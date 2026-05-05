# Filtering instructions

This file is dropped verbatim into Claude's system prompt for every dedup decision.
Edit it freely and save — the running aggregator picks up changes within ~5 seconds.
No restart required.

## Always suppress

- Pure price-action speculation ("X to $1000", "moonshot pumps")
- Reposted news from >2 days ago framed as "BREAKING"
- Promotional content for paid trading groups, signal services, or referral codes
- Cryptocurrency airdrop announcements with no underlying utility

## Always prefer

- Protocol-level news (mainnet upgrades, exploits, governance, fund flows)
- Primary-source links (official blogs, GitHub releases, on-chain data)
- Substantive analysis with concrete data, not pure opinion

## Dedup guidance

- Treat two posts about the same event as duplicates even if their wording differs significantly.
- A short tweet-style summary and a long-form article about the same news are duplicates — keep whichever arrived first.
- Different angles on the same event (e.g. "X token listed on Y" vs "Y exchange adds X token") are duplicates.
