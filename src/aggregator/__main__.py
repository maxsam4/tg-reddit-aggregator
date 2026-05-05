"""CLI: `aggregator run | login | doctor`."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import click

from .config import load_config, load_secrets
from .dedup import decide as dedup_decide
from .filters import FiltersFile
from .logging_setup import configure_logging
from .models import Source
from .pipeline import Dispatcher, Pruner
from .reddit import RedditPoller, RedditSender, make_httpx_fetcher
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


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--delay-seconds",
    default=1.5,
    show_default=True,
    type=float,
    help="Delay between successive joins to stay clear of Telegram rate limits.",
)
def join(config_path: str, delay_seconds: float) -> None:
    """Join all public Telegram channels listed in config.yaml using the userbot session.

    Run this once after `login` so the userbot is a member of every source channel
    before starting the daemon. Private channels (numeric IDs or invite links) must
    be joined manually since auto-join requires the public username form.
    """
    sys.exit(asyncio.run(_join_channels(config_path, delay_seconds)))


@cli.command()
@click.option("--config", "config_path", default="config.yaml", show_default=True)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    type=int,
    help="Number of most-recent dialogs to list.",
)
@click.option(
    "--query",
    default=None,
    help="If set, filter dialogs whose title contains this substring (case-insensitive).",
)
def destinations(config_path: str, limit: int, query: str | None) -> None:
    """List recent Telegram chats with numeric chat_id, so you can pick a destination
    without opening the Telegram app.

    Each row prints: title, type (group/channel/user/etc), and the chat_id you can
    paste into config.yaml under destination.telegram_group.
    """
    sys.exit(asyncio.run(_list_destinations(config_path, limit, query)))


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

    # 5. Reddit (unauthenticated public JSON endpoint — only checks the UA gets a 200)
    try:
        fetch, close_fetcher = make_httpx_fetcher(secrets.reddit_user_agent)
        try:
            submissions = await fetch("test", 1)
        finally:
            await close_fetcher()
        # Empty list is acceptable (rate limit / 403 returns []); we surface the
        # underlying log line if it happened. The check passes if the call did not
        # raise — meaning the UA is valid and the endpoint is reachable.
        line(
            "✓",
            f"Reddit public endpoint OK ({len(submissions)} sample posts from r/test)",
        )
    except Exception as e:
        line("✗", f"Reddit: {e}")
        failures += 1

    # 6. Telegram (session, channels resolvable, destination resolvable)
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
                # 6a. Each configured source channel must resolve (i.e. userbot is a member
                # OR the channel is public). Unresolvable means we won't receive events.
                for ch in cfg.telegram.channels:
                    try:
                        ent = await client.get_entity(ch)
                        title = (
                            getattr(ent, "title", None)
                            or getattr(ent, "username", None)
                            or str(ch)
                        )
                        line("✓", f"channel {ch} resolves → {title}")
                    except Exception as e:
                        line(
                            "✗",
                            f"channel {ch} unresolvable ({type(e).__name__}); "
                            "run `aggregator join` or subscribe manually",
                        )
                        failures += 1
                # 6b. Destination must resolve.
                try:
                    dest_ent = await client.get_entity(cfg.destination.telegram_group)
                    dest_title = (
                        getattr(dest_ent, "title", None)
                        or getattr(dest_ent, "username", None)
                        or str(cfg.destination.telegram_group)
                    )
                    line("✓", f"destination resolves → {dest_title}")
                except Exception as e:
                    line(
                        "✗",
                        f"destination {cfg.destination.telegram_group!r} "
                        f"unresolvable ({type(e).__name__}); the userbot must be a "
                        "member of the destination group",
                    )
                    failures += 1
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

    # Lazy imports so unit tests can import this module without telethon installed.
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

    reddit_fetch, reddit_close = make_httpx_fetcher(secrets.reddit_user_agent)
    poller = RedditPoller(
        fetcher=reddit_fetch,
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
        await reddit_close()
        await telegram_client.disconnect()
        await store.close()


async def _idle() -> None:
    await asyncio.Event().wait()


async def _open_authorized_telegram_client(secrets) -> Any:
    """Open + connect a Telethon client using the saved session, or exit non-zero
    with a clear message if it isn't authorized."""
    from telethon import TelegramClient  # type: ignore[import-not-found]

    sess = Path(secrets.telegram_session_path)
    if not sess.exists():
        click.echo(f"✗ No Telegram session at {sess}. Run `aggregator login` first.")
        sys.exit(1)
    client = TelegramClient(
        str(sess), secrets.telegram_api_id, secrets.telegram_api_hash
    )
    await client.connect()
    if not await client.is_user_authorized():
        click.echo("✗ Telegram session not authorized. Run `aggregator login` first.")
        await client.disconnect()
        sys.exit(1)
    return client


async def _join_channels(config_path: str, delay_seconds: float) -> int:
    secrets = load_secrets()
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level, log_file=None)

    if not cfg.telegram.channels:
        click.echo("No telegram.channels configured in config.yaml; nothing to join.")
        return 0

    from telethon.errors import (  # type: ignore[import-not-found]
        ChannelPrivateError,
        FloodWaitError,
        UsernameInvalidError,
        UsernameNotOccupiedError,
    )
    from telethon.tl.functions.channels import (  # type: ignore[import-not-found]
        JoinChannelRequest,
    )

    client = await _open_authorized_telegram_client(secrets)
    click.echo(f"== Joining {len(cfg.telegram.channels)} channel(s) ==")
    failures = 0

    try:
        for ch in cfg.telegram.channels:
            # Numeric IDs / private channels can't be auto-joined via username flow.
            if isinstance(ch, int):
                click.echo(
                    f"  ⚠ {ch}: numeric (private) channel id — auto-join unsupported. "
                    "Join manually via invite link."
                )
                failures += 1
                continue
            try:
                entity = await client.get_entity(ch)
            except (UsernameNotOccupiedError, UsernameInvalidError):
                click.echo(f"  ✗ {ch}: username does not exist or is invalid")
                failures += 1
                continue
            except Exception as e:
                click.echo(f"  ✗ {ch}: resolve failed ({type(e).__name__}: {e})")
                failures += 1
                continue

            try:
                await client(JoinChannelRequest(entity))
                click.echo(f"  ✓ {ch}: joined (or already a member)")
            except FloodWaitError as e:
                click.echo(
                    f"  ⚠ {ch}: Telegram FloodWait {e.seconds}s; sleeping then retrying"
                )
                await asyncio.sleep(e.seconds + 1)
                try:
                    await client(JoinChannelRequest(entity))
                    click.echo(f"  ✓ {ch}: joined after wait")
                except Exception as e2:
                    click.echo(f"  ✗ {ch}: still failed: {e2}")
                    failures += 1
            except ChannelPrivateError:
                click.echo(
                    f"  ✗ {ch}: private channel; you must be invited or use an invite link"
                )
                failures += 1
            except Exception as e:
                err_name = type(e).__name__
                # Telethon raises UserAlreadyParticipantError as a subclass of RPCError
                # in some versions; treat any "AlreadyParticipant" as success.
                if "AlreadyParticipant" in err_name:
                    click.echo(f"  ✓ {ch}: already a member")
                else:
                    click.echo(f"  ✗ {ch}: join failed ({err_name}: {e})")
                    failures += 1

            await asyncio.sleep(delay_seconds)
    finally:
        await client.disconnect()

    if failures:
        click.echo(f"\n{failures} channel(s) failed.")
        return 2
    click.echo("\nAll channels joined.")
    return 0


async def _list_destinations(config_path: str, limit: int, query: str | None) -> int:
    secrets = load_secrets()
    cfg = load_config(config_path)
    configure_logging(cfg.logging.level, log_file=None)

    client = await _open_authorized_telegram_client(secrets)
    needle = query.lower() if query else None
    click.echo(f"{'CHAT_ID':>16}  {'TYPE':<10}  TITLE")
    click.echo("-" * 60)
    try:
        async for dialog in client.iter_dialogs(limit=limit):
            title = getattr(dialog, "title", None) or "?"
            if needle and needle not in title.lower():
                continue
            kind = "channel" if dialog.is_channel else (
                "group" if dialog.is_group else (
                    "user" if dialog.is_user else "other"
                )
            )
            entity = dialog.entity
            chat_id = getattr(entity, "id", None)
            # Show the marked form (-100... for supergroups/channels) since that's
            # what config.yaml expects.
            try:
                from telethon.utils import get_peer_id  # type: ignore[import-not-found]

                marked = get_peer_id(entity)
            except Exception:
                marked = chat_id
            click.echo(f"{marked:>16}  {kind:<10}  {title}")
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    cli()
