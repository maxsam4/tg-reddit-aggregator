# Tuning `filters.md`

`filters.md` is dropped verbatim into Claude's system prompt for every dedup decision. There's no special syntax — it's plain markdown, written for Claude to read.

## How it gets used

For every candidate post, the prompt looks roughly like:

```
<dedup task instructions, fixed>
BEGIN_USER_FILTERS
<contents of filters.md>
END_USER_FILTERS

RECENT DELIVERED ITEMS (oldest first):
  [id=14] [reddit] [12m ago] r/cryptocurrency | ...
  [id=15] [telegram] [3m ago] [SomeChannel] ...

NEW CANDIDATE:
  [reddit] r/ethereum | Some new post — example.com

Call record_decision exactly once.
```

Claude returns one of three decisions: **deliver**, **duplicate**, or **filtered**. The recent items are used only to detect duplicates — your `filters.md` is what drives "filtered" decisions.

## Patterns that work well

### 1. Plain English allow/block lists

```
## Always suppress
- Pure price speculation ("X to $1000", moonshot pumps)
- Promotional content for paid trading groups or referral codes
- Crypto airdrop announcements with no underlying utility

## Always deliver
- Mainnet upgrades, governance votes, exploits, post-mortems
- On-chain data with concrete numbers
- Primary-source links (official blogs, GitHub releases)
```

### 2. Per-source rules

```
## Source-specific
- Posts from r/cryptomemes: filter out unless they contain breaking news.
- Posts from @ChannelXYZ: this channel reposts old news; suppress anything older than 12 hours unless the post itself is novel commentary.
```

### 3. Dedup hints

The dedup task instructions already explain what counts as a duplicate, but you can sharpen the rules for your domain:

```
## Dedup guidance
- Treat "X token listed on Y" and "Y exchange adds X" as the same news.
- A 24-hour follow-up with substantively new info (revised numbers,
  attribution, post-mortem) is NOT a duplicate — deliver it.
```

## Patterns that don't work

- **Regex or programmatic matching.** Claude reads filters.md as natural language; it won't follow `^.*pump.*$`. Just write what you mean: "Filter out posts about pump-and-dump schemes."
- **Hyper-specific keyword blocklists.** A 200-word blocklist of project names is brittle and burns tokens on every call. Prefer principle-based rules ("filter out promotional content") over enumeration.
- **Conflicting rules.** If you say "always deliver protocol news" AND "filter posts mentioning ProtocolX", Claude has to pick one — the result is unpredictable. State the override explicitly: "filter posts mentioning ProtocolX even if they're protocol news".

## Iterating

The aggregator hot-reloads `filters.md` within ~5 seconds of saving. Workflow:

1. Watch your destination group.
2. When you spot noise you don't want, open `filters.md`, add a one-line rule, save.
3. The next decision will use the new rule. No restart.
4. If a rule is too aggressive (you stop seeing things you wanted), check the `decisions` table:

```bash
sqlite3 data/state.db "SELECT timestamp, decision, reason FROM decisions ORDER BY id DESC LIMIT 30"
```

Each row has Claude's stated reason. If you see "Filtered per rule about X" applied to a post you actually wanted, soften the rule.

## Cost note

Each decision call consumes input tokens for the system prompt (filters + preamble) and the recent-history block. We mark the system block as cacheable (5-minute TTL) — if your traffic is at least one decision per ~5 minutes, you'll mostly pay cache-read prices for the system prompt. Long `filters.md` files (>10KB) can dominate cost; keep it tight.
