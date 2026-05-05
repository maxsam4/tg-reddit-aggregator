# Setup

Step-by-step first-run instructions. Plan on ~10–15 minutes total, mostly waiting for forms.

## 0. Prerequisites

- A Linux/macOS host with Python 3.11–3.13 available (3.12 recommended). [`uv`](https://github.com/astral-sh/uv) will fetch a suitable interpreter automatically.
- A regular Telegram account (with phone + password if 2FA is enabled).
- A Reddit account.
- An Anthropic console account with an API key.

## 1. Clone and install

```bash
git clone git@github.com:maxsam4/tg-reddit-aggregator.git
cd tg-reddit-aggregator
uv sync
```

## 2. Get Telegram API credentials

1. Open <https://my.telegram.org> and log in with your phone number.
2. Click **API development tools**.
3. Fill out the form (any app name will do; "platform" can be Desktop). On submit you get an **api_id** (integer) and **api_hash** (long hex string).
4. Copy them into `.env`:

```
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef0123456789...
```

> Treat these like a password. Anyone with both can impersonate your account.

## 3. Create a Reddit "script" app

1. Open <https://www.reddit.com/prefs/apps>.
2. Scroll to **create another app...** at the bottom.
3. Pick type **script**.
4. Set redirect URI to `http://localhost:8080` (unused but required).
5. Submit. The page now shows the **client_id** (under your app name) and a **secret**.
6. Copy them into `.env`:

```
REDDIT_CLIENT_ID=abc123xyz
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=tg-reddit-aggregator/0.1 by u/your-reddit-username
```

> The user agent must include your Reddit username — Reddit blocks generic UAs.

## 4. Get an Anthropic API key

1. Open <https://console.anthropic.com>.
2. **Settings → API Keys → Create Key**.
3. Copy into `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## 5. Configure channels, subreddits, and destination

Copy and edit:

```bash
cp config.example.yaml config.yaml
cp filters.example.md filters.md
$EDITOR config.yaml filters.md
```

For each Telegram source channel, **the userbot account must be a member**. Public channels can be referenced by `@username`; private channels by their numeric id (negative for supergroups, e.g. `-1001234567890`). Same for the destination group.

Tip for getting a numeric chat_id: open the chat in <https://web.telegram.org>, the URL shows the id.

## 6. One-time Telegram login

```bash
uv run aggregator login
```

You'll be prompted for your phone number, then an OTP, then your 2FA password (if set). On success a `data/userbot.session` file is written. **This file is a credential** — `.gitignore` already excludes it, but don't share it.

## 7. Run the doctor

```bash
uv run aggregator doctor
```

Expected output: ✓ for every check. Exit code 0. If anything fails, fix it before running the daemon — the doctor catches most setup mistakes (wrong api_hash, expired Reddit secret, missing session, etc.).

## 8. Start the aggregator

```bash
uv run aggregator run
```

Or via the helper script (used by systemd / OpenClaw):

```bash
./start.sh
```

You should see structured log lines per event. Newly arriving Telegram posts appear in your destination group within a couple of seconds; Reddit posts within `poll_interval_seconds`.

## 9. Tune `filters.md` over time

`filters.md` is hot-reloaded on save (~5s lag). When you spot noise in the destination group you don't want, add a rule to `filters.md`. The next decision picks it up. See [tuning-filters.md](tuning-filters.md) for patterns that work well.
