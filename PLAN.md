# tg-reddit-aggregator — Implementation Plan

## Context

Build a personal news-aggregator daemon that:
- Listens to user-configured **Telegram channels** via a userbot session and **forwards** new posts to a single destination Telegram group.
- Polls user-configured **Reddit subreddits** every N seconds (default 60) via PRAW and **posts** new submissions to the same destination group as standalone messages with a permalink back to Reddit.
- Uses the **Claude API** to deduplicate (across both sources, 24h rolling window) and to apply free-form filtering rules from a user-edited `filters.md`.
- Runs as a single long-lived Python process, supervised by **OpenClaw** on a server (or directly via systemd). Config-file driven; setup is documented end-to-end in the repo.
- Repo lives at `git@github.com:maxsam4/tg-reddit-aggregator.git` (currently empty; local working dir `/Users/mgupta/Development/news` is also empty).

**Why**: the user reads news across many Telegram channels and subreddits; the same story is often broken simultaneously by overlapping sources, drowning the signal. A Claude-based dedup + filter layer collapses redundant noise into a single curated feed without losing source attribution (Telegram forwards preserve origin).

## Decisions locked during brainstorming

- **Stack**: Python 3.11+, single asyncio process. Libraries: `Telethon` (userbot), **`asyncpraw`** (async-native Reddit client — required because sync PRAW blocks the event loop), `anthropic` (Claude API), `aiosqlite`, `PyYAML`, `python-dotenv`, `tenacity` (retries), `structlog` (logging).
- **Dedup approach**: per-item LLM check. For each candidate, send Claude the last 24h of delivered items (truncated to 1000 chars each) plus the new item. Claude returns structured JSON `{decision, reason}` via forced tool use. Items judged `duplicate` or `filtered` are NOT inserted into the recent-items history (so history stays clean).
- **Truncation**: every stored item text is capped at 1000 chars (configurable as `dedup.max_chars_per_item`).
- **Reddit auth**: `asyncpraw` with a user-created "script" app — read-only client (client_id, client_secret, user_agent only; no username/password). Unauth was ruled out — 10 req/min and increasingly blocked.
- **Identity**: the same Telethon userbot session reads source channels AND posts to the destination group. The user is already a member of the destination group, so no Telegram Bot API token is needed.
- **Telegram delivery**: native `forward_messages` (preserves "Forwarded from X" header). For albums, forward as a group via `events.Album` so a 3-photo post becomes one delivered item (and one Claude call). Fallback to copy-with-attribution prefix when a source channel has `noforwards: True`.
- **Reddit delivery**: standalone message — title, author, snippet of selftext (≤500 chars), permalink. Link-posts (no selftext) include the linked URL; the dedup text uses `title + " — " + url_host` so Claude has signal for similarity.
- **Filter file format**: `filters.md`, plaintext markdown, dropped verbatim into Claude's system prompt. Hot-reloaded on mtime change.
- **Config format**: `config.yaml` for non-secret settings; `.env` for secrets.
- **Storage**: single SQLite file at `data/state.db`. Two tables: `items` (state-machine work queue, also serves as recent-history source) and `decisions` (audit log with token usage and latency). SQLite is the durable work queue — no in-memory queue is the source of truth.
- **State machine**: every item observed by either producer is inserted as `status=queued`. The dispatcher transitions it to `decided` once Claude responds, then `delivered` (only for `decision=deliver` after destination send confirms) or `dropped` (for `duplicate` / `filtered`, or for `deliver` items whose delivery permanently fails after retries). On restart, anything not in `delivered`/`dropped` is re-evaluated. Recent-history queries select `status='delivered' AND delivered_at > now-24h`.
- **Default model**: `claude-haiku-4-5` (configurable). Prompt caching applied to the stable system prompt + filter instructions; the system block is padded with the dedup-instructions preamble to clear Haiku's 1024-token cache minimum.

## Architecture (single asyncio process)

Three producers, one durable queue (SQLite), one dispatcher consumer, one pruner:

1. **Telegram listener** (`src/aggregator/telegram.py`) — Telethon registers BOTH `events.NewMessage` and `events.Album` for the configured channels. Single messages flow through `NewMessage`; grouped media (albums) flow through `Album` and are stored as one item with `media_group_id` so we forward the album as a unit and only call Claude once. On event, the listener does an idempotent `INSERT OR IGNORE` into `items` with `status='queued'` and signals the dispatcher via a small bounded `asyncio.Queue` (size 256, capacity-only — used as a wakeup channel, not state). `put_nowait` is wrapped in try/except for `QueueFull`; on overflow the dispatcher's polling fallback (below) catches up.
2. **Reddit poller** (`src/aggregator/reddit.py`) — `asyncio.create_task` loop running `await asyncpraw.Reddit(...).subreddit(name).new(limit=posts_per_poll)` for each configured subreddit every `poll_interval_seconds`. For each post: skip if `(source='reddit', source_id=submission.fullname)` already exists in `items`; otherwise INSERT with `status='queued'` and signal the dispatcher.
3. **Dispatcher** (`src/aggregator/pipeline.py`) — single consumer. Wakes on the in-memory signal queue OR every 5s as a fallback (covers the QueueFull / restart cases). Each tick, claims up to N rows where `status='queued' OR (status='retry' AND next_attempt_at <= now)`, processes each in order:
   1. Build dedup prompt (system = preamble + `filters.md`, cached; user = `delivered` items in last 24h **in chronological order, oldest first**, then `NEW CANDIDATE:` + truncated text).
   2. Call Claude (forced tool use). On 5xx/timeout: `tenacity` 3 retries (1s→4s→8s); after exhaustion mark `status='retry'`, `attempts++`, `next_attempt_at = now + min(60s * 2^attempts, 1h)`. Do NOT mark `decided`. Do NOT pollute history.
   3. On Claude success → INSERT row into `decisions` (audit) and set `items.status='decided'`, store the `decision` and `reason`.
   4. If `decision='deliver'`: invoke source-specific sender. On success: `status='delivered'`, set `delivered_at=now`. On send failure: same retry semantics as Claude failures (max 5 attempts; after that `status='dropped'` with `drop_reason='delivery_failed'`).
   5. If `decision in ('duplicate','filtered')`: `status='dropped'` (history is NOT touched — recent-history query filters by `status='delivered'`).
4. **Pruner** (`src/aggregator/store.py`) — periodic task every 1h; deletes `items` rows where `delivered_at < now - window_hours*2` AND `status='delivered'`, plus all `dropped` rows older than 7 days. Deletes `decisions` older than 30 days. Runs `PRAGMA optimize` weekly.

`filters.md` watcher polls mtime every 5s and reloads contents if changed (no restart needed); next Claude call picks up the new system prompt.

**Why SQLite-as-queue, not asyncio.Queue**: durability across crashes, explicit retry timing, and the same table powers the 24h history query — no extra writes. The in-memory channel exists only to wake the dispatcher quickly; if it's full or the process restarts, the polling fallback catches up within 5 seconds.

## Files to create

```
tg-reddit-aggregator/
├── README.md
├── config.example.yaml
├── filters.example.md
├── .env.example
├── .gitignore                       # ignores .env, data/, .session, *.log
├── pyproject.toml                   # project metadata + deps; uv-managed
├── uv.lock
├── start.sh                         # `uv run python -m aggregator run`
├── systemd/aggregator.service       # example unit
├── docs/
│   ├── setup.md                     # step-by-step first run
│   ├── openclaw.md                  # OpenClaw integration
│   └── tuning-filters.md            # how to write filters.md
├── src/aggregator/
│   ├── __init__.py
│   ├── __main__.py                  # CLI: run | login | doctor
│   ├── config.py                    # YAML + .env loader; pydantic validation
│   ├── telegram.py                  # Telethon listener + forward/copy sender
│   ├── reddit.py                    # PRAW poller + standalone formatter
│   ├── dedup.py                     # Claude prompt + tool-use schema + caching
│   ├── store.py                     # aiosqlite layer + schema migrations
│   ├── pipeline.py                  # asyncio queue + dispatcher + pruner
│   ├── filters.py                   # filters.md hot-reload
│   ├── models.py                    # Item dataclass, Decision enum
│   └── logging_setup.py             # structlog config
└── tests/
    ├── conftest.py                  # fake Anthropic, fake Telethon, fake PRAW
    ├── test_config.py
    ├── test_store.py                # schema, prune, idempotency
    ├── test_dedup_prompt.py         # prompt shape, truncation, caching
    ├── test_pipeline.py             # all three decisions, history non-pollution, retry/restart durability
    ├── test_telegram.py             # forward vs copy fallback, album grouping
    └── test_reddit.py               # poller skip-seen, formatting, link-post dedup text
```

## Critical configuration shapes

`config.yaml`:
```yaml
destination:
  telegram_group: "@my_news_dump"     # or numeric chat_id
telegram:
  channels:
    - "@somecryptochannel"
    - -1001234567890
reddit:
  poll_interval_seconds: 60
  posts_per_poll: 25
  subreddits: [cryptocurrency, ethereum]
dedup:
  window_hours: 24
  max_chars_per_item: 1000
  model: "claude-haiku-4-5"
storage:
  sqlite_path: "./data/state.db"
logging:
  level: INFO
  file: "./data/aggregator.log"
```

`.env`:
```
TELEGRAM_API_ID=...
TELEGRAM_API_HASH=...
TELEGRAM_SESSION_PATH=./data/userbot.session
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=tg-reddit-aggregator/0.1 by u/<your-name>
ANTHROPIC_API_KEY=...
```

`filters.md` is free-form markdown, dropped verbatim into Claude's system prompt.

## SQLite schema (`src/aggregator/store.py`)

Two tables. `items` is both the durable work queue and the recent-history source — eliminates duplicated writes and avoids the early-mark problem.

```sql
CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,                -- 'telegram' | 'reddit'
  source_id TEXT NOT NULL,             -- e.g. 'tg:<channel_id>:<msg_id>' or 'reddit:t3_xxx'
  media_group_id TEXT,                 -- Telegram album grouping; NULL for singles/reddit
  url TEXT,                            -- canonical URL (Reddit permalink, t.me link for tg)
  text TEXT NOT NULL,                  -- truncated to dedup.max_chars_per_item; for Reddit link-posts: title + ' — ' + url_host
  created_at TIMESTAMP NOT NULL,       -- when the source created the post
  observed_at TIMESTAMP NOT NULL,      -- when we ingested it
  status TEXT NOT NULL,                -- queued | retry | decided | delivered | dropped
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMP,           -- set when status='retry'
  decision TEXT,                       -- deliver | duplicate | filtered (after Claude responds)
  decision_reason TEXT,
  delivered_at TIMESTAMP,              -- set only when status transitions to 'delivered'
  drop_reason TEXT,                    -- 'duplicate' | 'filtered' | 'delivery_failed' | 'claude_failed'
  UNIQUE(source, source_id)
);
CREATE INDEX idx_items_status ON items(status, next_attempt_at);
CREATE INDEX idx_items_history ON items(status, delivered_at);  -- for the 24h recent-history query

CREATE TABLE decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  decision TEXT NOT NULL,                            -- deliver | duplicate | filtered
  reason TEXT,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_creation_input_tokens INTEGER,
  cache_read_input_tokens INTEGER,
  latency_ms INTEGER,
  timestamp TIMESTAMP NOT NULL
);
CREATE INDEX idx_decisions_timestamp ON decisions(timestamp);
```

Recent-history query for the prompt: `SELECT id, source, text, delivered_at FROM items WHERE status='delivered' AND delivered_at >= now - window_hours ORDER BY delivered_at ASC` (chronological, oldest first).

## Claude prompt design (`src/aggregator/dedup.py`)

- **System block (cached)**: fixed preamble describing the dedup task + behavior rules + `filters.md` contents. `cache_control: {"type": "ephemeral"}` for the 5-minute TTL. The preamble is sized so the cached block reliably exceeds Haiku 4.5's **1024-token cache minimum** even when `filters.md` is small; otherwise caching silently no-ops.
- **User message**: chronological list (oldest → newest) of last-24h `delivered` items rendered as `[id={items.id}] [{source}] [{ago}] {text}` followed by `NEW CANDIDATE:` and the candidate's normalized text. Chronological order keeps Claude's attention near the candidate at the end of the prompt.
- **Tool-use schema**: a single tool `record_decision(decision: "deliver"|"duplicate"|"filtered", reason: string, duplicate_of_id?: integer)`. Force structured output via `tool_choice: {type: "tool", name: "record_decision"}` — no regex parsing.
- **Audit logging**: every Claude response writes a `decisions` row capturing `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` (so cache effectiveness is measurable), and `latency_ms`.

## Failure handling

All transient failures route through the `items` state machine — `status='retry'` with `attempts++` and a backoff-computed `next_attempt_at`. Nothing is "left in memory"; every retry is durable.

- **Anthropic 5xx / timeout** → `tenacity` 3 immediate retries (1s→4s→8s). Exhaustion: row → `status='retry'`, exponential `next_attempt_at` (1m→2m→…→1h cap). Item is re-claimed on a future dispatcher tick. Never marked `decided` until Claude responds.
- **Telethon `FloodWaitError`** → sleep the requested duration before retrying delivery; the dispatcher serializes sends so a flood-wait holds the whole pipeline (acceptable — it's per-account).
- **`asyncpraw` 429 / `prawcore.RequestException`** → asyncpraw handles retry-after internally; on top of that, the poller catches and logs, skipping the current poll cycle for the offending subreddit.
- **Destination send failure** → up to 5 attempts via the same `status='retry'` path. After exhaustion: `status='dropped'`, `drop_reason='delivery_failed'` (logged loudly).
- **Forward-restricted source channels** → catch Telethon `ChatForwardsRestrictedError`, fall back to copy with `📎 from <channel name>` prefix. If media is too large to re-upload (e.g. video > 50MB, document over the bot/userbot upload limit), degrade further to text-only with a `(media omitted — open in source channel)` line and the `t.me` link. Always preserve attribution.
- **Album partial failure** → if forwarding an album as a group fails, copy the textual caption and links with attribution; do not fall back to per-item forwards (that defeats the album dedup).

## Existing utilities to reuse

This is a greenfield repo, so nothing to reuse from the local codebase. External libraries (Telethon, PRAW, anthropic SDK, tenacity, structlog) are pulled in fresh.

## Verification plan

End-to-end manual test (documented in `docs/setup.md` so the user can replay):

1. **Local install**: `uv sync` in repo root.
2. **First-run login**: `uv run python -m aggregator login` → enter Telegram phone, OTP, 2FA. Confirms `.session` file is written.
3. **Doctor command**: `uv run python -m aggregator doctor` validates `.env` (all keys present), `config.yaml` (parses, channels resolvable, destination group joinable), **Reddit auth via a public read** (`await reddit.subreddit("test").new(limit=1)` — works with read-only script-app credentials; `reddit.user.me()` would require password auth which we deliberately don't use), Anthropic auth (1-token sanity ping using `claude-haiku-4-5`), SQLite schema and write-permission. Prints a clean ✓/✗ table and exits non-zero on any failure (so OpenClaw / systemd ExecStartPre can gate startup).
4. **Smoke run with one channel + one sub**: configure 1 Telegram channel and 1 small subreddit (e.g. `r/test`), point destination at a private test group. `uv run python -m aggregator run`. Watch logs.
5. **Verify deliver path**: post a brand-new test message to the source Telegram channel → confirm it appears forwarded in destination group within seconds. Confirm an `items` row with `status='delivered'`, `delivered_at` set, and a corresponding `decisions` row.
6. **Verify duplicate path**: post a near-duplicate of an existing recent item to a different configured channel → confirm it does NOT appear in destination, confirm a `decisions` row with `decision='duplicate'`, and confirm the `items` row is `status='dropped'` with `drop_reason='duplicate'` AND `delivered_at IS NULL` (proves the recent-history query won't see it).
7. **Verify filtered path**: add a rule to `filters.md` (e.g. "skip messages containing the word 'PUMP'") → without restarting, post a message containing PUMP → confirm `decision='filtered'`, no delivery, `items.delivered_at IS NULL`. Validates hot-reload.
8. **Verify Reddit standalone**: a new submission appears in the destination group as a non-forwarded message with title, author, snippet, permalink. For a Reddit link-post, confirm dedup text in `items.text` is `title — url_host`.
9. **Verify Telegram album**: post a 3-photo album to a source channel → confirm exactly ONE `decisions` row, ONE `items` row, and the destination group receives a single album (not 3 separate forwards).
10. **Verify forward-restricted fallback**: target a forward-protected channel → confirm copy fallback fires with `📎 from` prefix; for an oversized media item, confirm the text-only fallback fires with `(media omitted)` line.
11. **Restart safety**: kill the process mid-flight (e.g. while Claude call is in flight) → restart → confirm the in-flight item is re-evaluated and either delivered or dropped exactly once, never twice. Confirm a `delivered` item from before the crash is NOT redelivered.

Unit tests cover the deterministic pieces (schema, prune, prompt shape, decision routing, formatters, fallback logic) using fakes for Anthropic / Telethon / PRAW.

## Out of scope (call out, do not build)

- Multiple destination groups / routing rules — one destination only for v1.
- Embedding-based pre-filter — `filters.md` + LLM check is sufficient at expected volume; revisit if cost/latency becomes a problem.
- Web UI for editing filters — `filters.md` + hot-reload is the interface.
- Cross-server replication / HA — single process is intentional.
- Historical backfill — only items arriving after process start are processed.

## Pushing to GitHub

The local repo is already initialized on `main` and the `origin` remote points at `git@github.com:maxsam4/tg-reddit-aggregator.git` (done before planning handoff).

After implementation and local verification:
1. Stage and commit the work.
2. Confirm with the user before the first push, then `git push -u origin main`.

---

## Implementation status (as of 2026-05-05)

- Plan approved. Implementation paused before any code was written.
- Local repo initialized at `/Users/mgupta/Development/news` on `main` with `origin` → `git@github.com:maxsam4/tg-reddit-aggregator.git`.
- `uv` 0.11.8 installed at `~/.local/bin/uv` (user-scoped). Add `~/.local/bin` to PATH for tomorrow's session: `export PATH="$HOME/.local/bin:$PATH"`.
- System Python is 3.14; uv will fetch a pinned 3.12 once `pyproject.toml` is created (recommend pinning to 3.12 for library compat — Telethon support on 3.14 is unverified).

## Resume checklist for tomorrow

1. Scaffold (`uv init` with Python 3.12 pin, `pyproject.toml`, `.gitignore`, `config.example.yaml`, `filters.example.md`, `.env.example`, README skeleton).
2. Core: `models.py`, `config.py`, `store.py` + tests.
3. Dedup + filters hot-reload + tests.
4. Telegram producer/sender (NewMessage + Album) + tests.
5. Reddit producer/sender (asyncpraw, link-post dedup text) + tests.
6. Pipeline + dispatcher + pruner + tests.
7. CLI (`__main__.py` with `run`/`login`/`doctor`), `start.sh`, `systemd/aggregator.service`.
8. Documentation (`README.md`, `docs/setup.md`, `docs/openclaw.md`, `docs/tuning-filters.md`).
9. Run `pytest`, run `doctor` with stub creds, commit.
10. Confirm with user before `git push -u origin main`.
