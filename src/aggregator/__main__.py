"""CLI: `aggregator run | login | doctor`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import click

from .config import load_config, load_secrets
from .dedup import decide as dedup_decide
from .filters import FiltersFile
from .logging_setup import configure_logging
from .models import Source
from .pipeline import Dispatcher, Pruner
from .reddit import RedditPoller, RedditSender
from .store import Store
from .telegram import TelegramListener, TelegramSender

log = logging.getLogger(__name__)


@click.group()
def cli() -> None:
    """Telegram + Reddit news aggregator with Claude-powered dedup."""


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True,
              help="Path to config.yaml")
def login(config_path: str) -> None:
    """Interactive Telegram login. Creates the .session file used by `run`."""
    secrets = load_secrets()
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level, log_file=None)
    Path(secrets.telegram_session_path).parent.mkdir(parents=True, exist_ok=True)

    from telethon import TelegramClient  # type: ignore[import-not-found]

    client = TelegramClient(
        secrets.telegram_session_path,
        secrets.telegram_api_id,
        secrets.telegram_api_hash,
    )

    async def _go() -> None:
        await client.start()
        me = await client.get_me()
        click.echo(f"✓ Logged in as @{getattr(me, 'username', None) or me.first_name}")
        click.echo(f"  Session file: {secrets.telegram_session_path}")
        await client.disconnect()

    asyncio.run(_go())


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
def doctor(config_path: str) -> None:
    """Sanity-check the wiring: env, config, Reddit auth, Anthropic auth, SQLite."""
    exit_code = asyncio.run(_doctor(config_path))
    sys.exit(exit_code)


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option("--filters", "filters_path", default="filters.md", show_default=True)
def run(config_path: str, filters_path: str) -> None:
    """Start the aggregator daemon."""
    asyncio.run(_run(config_path, filters_path))


# --- internals ---


async def _doctor(config_path: str) -> int:
    def line(mark: str, msg: str) -> None:
        click.echo(f"  {mark} {msg}")

    failures = 0

    click.echo("== tg-reddit-aggregator: doctor ==")

    # 1. .env
    try:
        secrets = load_secrets()
        line("✓", ".env loaded; all required keys present")
    except Exception as e:
        line("✗", f".env: {e}")
        return 1  # Without secrets we can't proceed.

    # 2. config.yaml
    try:
        cfg = load_config(config_path)
        line("✓", f"{config_path} parsed and validated")
    except Exception as e:
        line("✗", f"{config_path}: {e}")
        return 1

    if not cfg.telegram.channels and not cfg.reddit.subreddits:
        line("⚠", "no Telegram channels AND no subreddits configured — nothing to do")
        failures += 1

    # 3. SQLite
    try:
        store = Store(cfg.storage.sqlite_path)
        await store.open()
        await store.close()
        line("✓", f"SQLite open/write at {cfg.storage.sqlite_path}")
    except Exception as e:
        line("✗", f"SQLite: {e}")
        failures += 1

    # 4. Anthropic
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=secrets.anthropic_api_key)
        await client.messages.create(
            model=cfg.dedup.model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        line("✓", f"Anthropic auth OK (model={cfg.dedup.model})")
    except Exception as e:
        line("✗", f"Anthropic: {e}")
        failures += 1

    # 5. Reddit (read-only public listing — works with script-app creds, no password)
    try:
        import asyncpraw  # type: ignore[import-not-found]
        reddit = asyncpraw.Reddit(
            client_id=secrets.reddit_client_id,
            client_secret=secrets.reddit_client_secret,
            user_agent=secrets.reddit_user_agent,
        )
        try:
            sub = await reddit.subreddit("test")
            async for _ in sub.new(limit=1):
                break
            line("✓", "Reddit auth OK (read-only listing)")
        finally:
            await reddit.close()
    except Exception as e:
        line("✗", f"Reddit: {e}")
        failures += 1

    # 6. Telegram (session file presence + connect)
    try:
        sess = Path(secrets.telegram_session_path)
        if not sess.exists():
            line("⚠", f"No session file at {sess}; run `aggregator login` first")
            failures += 1
        else:
            from telethon import TelegramClient  # type: ignore[import-not-found]
            client = TelegramClient(
                str(sess), secrets.telegram_api_id, secrets.telegram_api_hash
            )
            await client.connect()
            if not await client.is_user_authorized():
                line("✗", "Telegram session present but not authorized; run `aggregator login`")
                failures += 1
            else:
                me = await client.get_me()
                line(
                    "✓",
                    f"Telegram session OK (@{getattr(me, 'username', None) or me.first_name})",
                )
            await client.disconnect()
    except Exception as e:
        line("✗", f"Telegram: {e}")
        failures += 1

    # 7. filters.md (presence is informative, not required)
    fpath = Path("filters.md")
    if fpath.exists():
        line("✓", f"filters.md present ({fpath.stat().st_size} bytes)")
    else:
        line("⚠", "filters.md not found; running with no user filters")

    if failures:
        click.echo(f"\n{failures} check(s) failed.")
        return 2
    click.echo("\nAll checks passed.")
    return 0


async def _run(config_path: str, filters_path: str) -> None:
    secrets = load_secrets()
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level, cfg.logging.file)

    # Lazy imports so unit tests can import this module without telethon/asyncpraw.
    import asyncpraw  # type: ignore[import-not-found]
    from anthropic import AsyncAnthropic
    from telethon import TelegramClient  # type: ignore[import-not-found]

    store = Store(cfg.storage.sqlite_path)
    await store.open()

    filters_file = FiltersFile(filters_path)

    anthropic_client = AsyncAnthropic(api_key=secrets.anthropic_api_key)

    async def decide_fn(history, candidate):
        return await dedup_decide(
            anthropic_client,
            model=cfg.dedup.model,
            filter_text=filters_file.text,
            history=history,
            candidate=candidate,
            max_chars=cfg.dedup.max_chars_per_item,
        )

    telegram_client = TelegramClient(
        secrets.telegram_session_path,
        secrets.telegram_api_id,
        secrets.telegram_api_hash,
    )
    await telegram_client.connect()
    if not await telegram_client.is_user_authorized():
        log.error("Telegram session not authorized. Run `aggregator login` first.")
        await telegram_client.disconnect()
        await store.close()
        return

    senders = {
        Source.TELEGRAM: TelegramSender(telegram_client, cfg.destination.telegram_group),
        Source.REDDIT: RedditSender(telegram_client, cfg.destination.telegram_group),
    }

    dispatcher = Dispatcher(
        store=store,
        filters_file=filters_file,
        decide_fn=decide_fn,
        senders=senders,
        window_hours=cfg.dedup.window_hours,
        max_chars_per_item=cfg.dedup.max_chars_per_item,
    )
    pruner = Pruner(store, window_hours=cfg.dedup.window_hours)

    listener = TelegramListener(
        telegram_client, cfg.telegram.channels, dispatcher.enqueue
    )
    if cfg.telegram.channels:
        await listener.start()

    reddit_client = asyncpraw.Reddit(
        client_id=secrets.reddit_client_id,
        client_secret=secrets.reddit_client_secret,
        user_agent=secrets.reddit_user_agent,
    )

    poller = RedditPoller(
        client=reddit_client,
        subreddits=cfg.reddit.subreddits,
        enqueue=dispatcher.enqueue,
        already_seen=lambda sid: store.has_source_id(Source.REDDIT, sid),
        poll_interval_seconds=cfg.reddit.poll_interval_seconds,
        posts_per_poll=cfg.reddit.posts_per_poll,
    )

    log.info(
        "Aggregator running: %d Telegram channel(s), %d subreddit(s), destination=%s",
        len(cfg.telegram.channels),
        len(cfg.reddit.subreddits),
        cfg.destination.telegram_group,
    )

    try:
        await asyncio.gather(
            dispatcher.run(),
            poller.run() if cfg.reddit.subreddits else _idle(),
            pruner.run(),
            telegram_client.run_until_disconnected(),
        )
    finally:
        await reddit_client.close()
        await telegram_client.disconnect()
        await store.close()


async def _idle() -> None:
    await asyncio.Event().wait()


if __name__ == "__main__":
    cli()
