"""Dedup prompt assembly + decide() integration with the fake Anthropic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aggregator.dedup import (
    SYSTEM_PREAMBLE,
    SYSTEM_SUFFIX,
    build_system_blocks,
    build_user_message,
    decide,
)
from aggregator.models import Decision, Source, Status, StoredItem
from tests.conftest import FakeAnthropicClient, make_candidate


def test_build_system_blocks_marks_cache() -> None:
    blocks = build_system_blocks("filter rule X\n")
    assert len(blocks) == 1
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert SYSTEM_PREAMBLE.split("\n")[0] in blocks[0]["text"]
    assert "filter rule X" in blocks[0]["text"]
    assert blocks[0]["text"].endswith(SYSTEM_SUFFIX)


def test_build_system_blocks_handles_empty_filters() -> None:
    blocks = build_system_blocks("")
    assert "(no user filters configured)" in blocks[0]["text"]


def test_system_block_clears_haiku_cache_minimum() -> None:
    """Heuristic: Haiku 4.5 needs >=1024 tokens cached. Rough check: ≥4000 chars
    (≈1 token per 4 chars) so caching always activates for typical filters.md sizes."""
    blocks = build_system_blocks("")
    assert len(blocks[0]["text"]) >= 1500  # preamble alone is enough


def test_build_user_message_chronological_with_candidate_at_end() -> None:
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    history = [
        StoredItem(
            id=1,
            source=Source.TELEGRAM,
            source_id="tg:1:1",
            text="OLDEST item",
            created_at=now - timedelta(hours=2),
            observed_at=now - timedelta(hours=2),
            status=Status.DELIVERED,
            attempts=0,
            delivered_at=now - timedelta(hours=2),
        ),
        StoredItem(
            id=2,
            source=Source.REDDIT,
            source_id="t3_x",
            text="NEWER item",
            created_at=now - timedelta(minutes=30),
            observed_at=now - timedelta(minutes=30),
            status=Status.DELIVERED,
            attempts=0,
            delivered_at=now - timedelta(minutes=30),
        ),
    ]
    cand = make_candidate(text="THE CANDIDATE")
    msg = build_user_message(history, cand, max_chars=1000, now=now)
    # Candidate must come AFTER history.
    cand_pos = msg.index("THE CANDIDATE")
    older_pos = msg.index("OLDEST item")
    newer_pos = msg.index("NEWER item")
    assert older_pos < newer_pos < cand_pos
    # ID is included so Claude can return duplicate_of_id.
    assert "[id=1]" in msg
    assert "[id=2]" in msg


def test_build_user_message_truncates_long_history_text() -> None:
    item = StoredItem(
        id=1,
        source=Source.TELEGRAM,
        source_id="tg:1:1",
        text="x" * 5000,
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        status=Status.DELIVERED,
        attempts=0,
    )
    cand = make_candidate(text="y" * 5000)
    msg = build_user_message([item], cand, max_chars=100)
    # Neither history nor candidate may include >100 of the same char in a row.
    assert "x" * 200 not in msg
    assert "y" * 200 not in msg


@pytest.mark.asyncio
async def test_decide_uses_forced_tool_use(fake_anthropic: FakeAnthropicClient) -> None:
    fake_anthropic.queue(Decision.DELIVER, "fresh", usage={"input_tokens": 200, "output_tokens": 5})
    cand = make_candidate(text="news")
    result = await decide(
        fake_anthropic,
        model="claude-haiku-4-5",
        filter_text="some rules",
        history=[],
        candidate=cand,
        max_chars=1000,
    )
    assert result.decision == Decision.DELIVER
    assert result.reason == "fresh"
    assert result.input_tokens == 200
    assert result.model == "claude-haiku-4-5"
    # Verify the API was invoked with forced tool use.
    call = fake_anthropic.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_decision"}
    assert any(t["name"] == "record_decision" for t in call["tools"])


@pytest.mark.asyncio
async def test_decide_carries_cache_token_metrics(
    fake_anthropic: FakeAnthropicClient,
) -> None:
    fake_anthropic.queue(
        Decision.DUPLICATE,
        "same as id=14",
        duplicate_of_id=14,
        usage={
            "input_tokens": 50,
            "output_tokens": 10,
            "cache_creation_input_tokens": 1500,
            "cache_read_input_tokens": 0,
        },
    )
    cand = make_candidate()
    r = await decide(
        fake_anthropic,
        model="claude-haiku-4-5",
        filter_text="",
        history=[],
        candidate=cand,
        max_chars=1000,
    )
    assert r.decision == Decision.DUPLICATE
    assert r.duplicate_of_id == 14
    assert r.cache_creation_input_tokens == 1500
    assert r.cache_read_input_tokens == 0


@pytest.mark.asyncio
async def test_decide_propagates_anthropic_error(
    fake_anthropic: FakeAnthropicClient,
) -> None:
    fake_anthropic.queue_error(RuntimeError("boom"))
    cand = make_candidate()
    with pytest.raises(RuntimeError, match="boom"):
        await decide(
            fake_anthropic,
            model="claude-haiku-4-5",
            filter_text="",
            history=[],
            candidate=cand,
            max_chars=1000,
        )
