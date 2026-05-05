# tg-reddit-aggregator

A self-hosted news aggregator that listens to Telegram channels and Reddit subreddits, deduplicates similar posts via the Claude API, and delivers a single curated stream to a Telegram group of your choice.

- **Telegram side**: a userbot session listens to channels you're a member of and forwards posts (preserving the "Forwarded from X" header) to your destination group.
- **Reddit side**: a poller hits Reddit's public JSON endpoint (no auth, no script app) for the subreddits you list and posts them to the same destination group as standalone messages with a permalink.
- **Dedup + filter**: every candidate post is judged by Claude against a rolling 24-hour history of already-delivered items, and against a free-form `filters.md` you maintain.
- **Single process**: one Python `asyncio` daemon, durable SQLite work queue, supervised by [OpenClaw](https://openclaw.ai) or systemd.

## Quick start

```bash
# 1. Install uv (one-liner) if you don't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install deps
uv sync

# 3. Copy and edit config files
cp .env.example .env
cp config.example.yaml config.yaml
cp filters.example.md filters.md
# Edit .env (Telegram + Reddit + Anthropic creds) and config.yaml (channels, subreddits, destination)

# 4. One-time Telegram login (creates a session file)
uv run aggregator login

# 5. Sanity-check the wiring
uv run aggregator doctor

# 6. Run the daemon
uv run aggregator run
```

See [docs/setup.md](docs/setup.md) for step-by-step credential setup, [docs/openclaw.md](docs/openclaw.md) for OpenClaw integration, and [docs/tuning-filters.md](docs/tuning-filters.md) for tips on writing `filters.md`.

## Architecture

- **`src/aggregator/telegram.py`** — Telethon listener (NewMessage + Album events) and forward/copy sender.
- **`src/aggregator/reddit.py`** — `asyncpraw` poller and Reddit-post formatter.
- **`src/aggregator/dedup.py`** — Claude prompt assembly + forced tool-use decision call.
- **`src/aggregator/store.py`** — `aiosqlite` storage layer; the SQLite database is the durable work queue.
- **`src/aggregator/pipeline.py`** — single dispatcher consuming queued items, calling Claude, routing to senders, applying retry/backoff.
- **`src/aggregator/filters.py`** — `filters.md` hot-reload by mtime polling.

State machine for every observed item:

```
queued → decided → delivered            (Claude said "deliver" + send succeeded)
       ↓         ↘
       retry      dropped               (duplicate / filtered / delivery failed permanently)
```

On crash, anything not yet `delivered` or `dropped` is re-evaluated on restart — never lost, never double-delivered.

## License

MIT.
