"""Claude prompt assembly + forced tool-use decision call.

The system prompt holds a stable preamble + filter instructions, marked for ephemeral
caching (5-minute TTL). The user message holds chronological history + the new candidate.
Output is forced through a single tool call so we always get structured JSON.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import Candidate, Decision, DecisionResult, StoredItem

# Force-padded preamble so the cached system block reliably exceeds Haiku's 1024-token
# cache minimum even when filters.md is small. If filters.md is larger, the padding
# becomes negligible and is still valid context.
SYSTEM_PREAMBLE = """\
You are the deduplication and filtering brain of a personal news-aggregator daemon.
Your job is to look at one new candidate news item and decide whether it should be
delivered to the user, suppressed as a duplicate of something already delivered in the
last 24 hours, or filtered out per the rules in this prompt.

You will be given a list of recently-delivered items (oldest first), then a single
NEW CANDIDATE. Use the recent items only to detect duplicates — do NOT use them as
filtering examples. The user's filtering rules are everything below this paragraph
and above the BEGIN_USER_FILTERS / END_USER_FILTERS markers.

Decision contract — you MUST call the record_decision tool exactly once with one of:
  - "deliver"   : the candidate is novel AND not blocked by user filter rules.
  - "duplicate" : the candidate covers the same underlying event/news as one of the
                  recently-delivered items. When choosing this, set duplicate_of_id to
                  the id of the prior item.
  - "filtered"  : the candidate violates one of the user's filter rules in this prompt.

Definition of "duplicate" for this task:
  Two posts are duplicates if a reasonable reader would consider them to be reporting
  the same event or news story, even if the wording, framing, source, or media differ.
  Examples that ARE duplicates:
    - A short tweet-style summary and a long-form article about the same news.
    - "X token listed on Y" and "Y exchange adds X token".
    - "Protocol Z hacked for $5M" and "Z exploit drains $5M".
  Examples that are NOT duplicates:
    - Two unrelated stories about the same project (a hack vs a partnership).
    - A new follow-up with substantively new information (revised numbers, post-mortem,
      attacker tracked) — those are deliver, not duplicate.

You MUST always provide a one-sentence reason explaining your decision in the tool call.

BEGIN_USER_FILTERS
"""

SYSTEM_SUFFIX = "\nEND_USER_FILTERS\n"


def build_system_blocks(filter_text: str) -> list[dict[str, Any]]:
    """The cacheable system block: preamble + filters + suffix.

    Returned as a single text block with cache_control set, ready to pass to
    `client.messages.create(system=...)`.
    """
    full = SYSTEM_PREAMBLE + (filter_text or "(no user filters configured)") + SYSTEM_SUFFIX
    return [
        {
            "type": "text",
            "text": full,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _ago(now: datetime, then: datetime) -> str:
    delta = now - then
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def build_user_message(
    history: list[StoredItem],
    candidate: Candidate,
    max_chars: int,
    now: datetime | None = None,
) -> str:
    """The fresh per-call user message. History oldest-first, candidate at the end."""
    now = now or datetime.now(UTC)
    lines: list[str] = ["RECENT DELIVERED ITEMS (oldest first):"]
    if not history:
        lines.append("  (none — last 24 hours had no delivered items)")
    else:
        for item in history:
            text = item.text[:max_chars]
            ts = item.delivered_at or item.observed_at
            lines.append(
                f"  [id={item.id}] [{item.source.value}] [{_ago(now, ts)}] {text}"
            )

    lines.append("")
    lines.append("NEW CANDIDATE:")
    lines.append(f"  [{candidate.source.value}] {candidate.text[:max_chars]}")
    lines.append("")
    lines.append("Call record_decision exactly once.")
    return "\n".join(lines)


RECORD_DECISION_TOOL: dict[str, Any] = {
    "name": "record_decision",
    "description": "Record the dedup/filter decision for the candidate item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["deliver", "duplicate", "filtered"],
                "description": (
                    "deliver = novel and unfiltered; "
                    "duplicate = same event as a recent item (set duplicate_of_id); "
                    "filtered = blocked by a user filter rule."
                ),
            },
            "reason": {
                "type": "string",
                "description": "One-sentence justification for the decision.",
            },
            "duplicate_of_id": {
                "type": ["integer", "null"],
                "description": "If decision=duplicate, the id of the matching prior item.",
            },
        },
        "required": ["decision", "reason"],
    },
}


class AnthropicLike(Protocol):
    """Minimal interface we need from the anthropic SDK (for testability)."""

    @property
    def messages(self) -> Any: ...  # pragma: no cover


def _extract_tool_use(response: Any) -> dict[str, Any]:
    """Pull the record_decision tool_use block out of an Anthropic response."""
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "record_decision":
            return dict(block.input)
    raise ValueError("Claude response did not contain a record_decision tool_use block")


async def decide(
    client: AnthropicLike,
    *,
    model: str,
    filter_text: str,
    history: list[StoredItem],
    candidate: Candidate,
    max_chars: int,
) -> DecisionResult:
    """Single dedup decision: build prompt, call Claude, return the structured result."""
    system_blocks = build_system_blocks(filter_text)
    user_text = build_user_message(history, candidate, max_chars)

    start = time.monotonic()
    response = await client.messages.create(
        model=model,
        max_tokens=512,
        system=system_blocks,
        tools=[RECORD_DECISION_TOOL],
        tool_choice={"type": "tool", "name": "record_decision"},
        messages=[{"role": "user", "content": user_text}],
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    parsed = _extract_tool_use(response)
    usage = response.usage
    return DecisionResult(
        decision=Decision(parsed["decision"]),
        reason=parsed.get("reason", ""),
        duplicate_of_id=parsed.get("duplicate_of_id"),
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        latency_ms=latency_ms,
    )
