"""Store: state machine, idempotency, recent-history query, pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aggregator.models import Decision, DecisionResult, DropReason, Source, Status
from aggregator.store import Store
from tests.conftest import make_candidate


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(store: Store) -> None:
    cand = make_candidate(source=Source.TELEGRAM, source_id="tg:1:42", text="hello")
    id1 = await store.enqueue(cand, max_chars=1000)
    id2 = await store.enqueue(cand, max_chars=1000)
    assert id1 is not None
    assert id2 is None  # already present


@pytest.mark.asyncio
async def test_enqueue_truncates(store: Store) -> None:
    long_text = "x" * 5000
    cand = make_candidate(text=long_text)
    item_id = await store.enqueue(cand, max_chars=1000)
    item = await store.get_item(item_id)
    assert item is not None
    assert len(item.text) == 1000


@pytest.mark.asyncio
async def test_state_machine_deliver_path(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:1")
    item_id = await store.enqueue(cand, max_chars=1000)
    pending = await store.claim_pending()
    assert len(pending) == 1
    assert pending[0].status == Status.QUEUED

    await store.mark_decided(item_id, Decision.DELIVER, "novel")
    item = await store.get_item(item_id)
    assert item.status == Status.DECIDED

    await store.mark_delivered(item_id)
    item = await store.get_item(item_id)
    assert item.status == Status.DELIVERED
    assert item.delivered_at is not None
    # Delivered items show up in recent history.
    history = await store.recent_history(window_hours=24)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_dropped_duplicate_not_in_history(store: Store) -> None:
    """Duplicate decisions must NOT pollute recent history."""
    cand = make_candidate(source_id="tg:1:2")
    item_id = await store.enqueue(cand, max_chars=1000)
    await store.mark_decided(item_id, Decision.DUPLICATE, "same as id=14")
    await store.mark_dropped(item_id, DropReason.DUPLICATE)
    item = await store.get_item(item_id)
    assert item.status == Status.DROPPED
    assert item.delivered_at is None
    history = await store.recent_history(window_hours=24)
    assert history == []


@pytest.mark.asyncio
async def test_dropped_filtered_not_in_history(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:3")
    item_id = await store.enqueue(cand, max_chars=1000)
    await store.mark_decided(item_id, Decision.FILTERED, "blocked by user filter")
    await store.mark_dropped(item_id, DropReason.FILTERED)
    history = await store.recent_history(window_hours=24)
    assert history == []


@pytest.mark.asyncio
async def test_retry_path_reclaim(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:4")
    item_id = await store.enqueue(cand, max_chars=1000)
    # Move to retry with a past next_attempt_at; should be claimable again.
    await store.mark_retry(item_id, datetime.now(UTC) - timedelta(seconds=1))
    pending = await store.claim_pending()
    assert len(pending) == 1
    assert pending[0].status == Status.RETRY
    assert pending[0].attempts == 1


@pytest.mark.asyncio
async def test_retry_path_future_not_claimable(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:5")
    item_id = await store.enqueue(cand, max_chars=1000)
    await store.mark_retry(item_id, datetime.now(UTC) + timedelta(minutes=10))
    pending = await store.claim_pending()
    assert pending == []


@pytest.mark.asyncio
async def test_recent_history_chronological_order(store: Store) -> None:
    """Returned oldest-first so the prompt has the candidate at the end."""
    ids = []
    for i in range(3):
        cand = make_candidate(source_id=f"tg:1:{i}", text=f"text-{i}")
        ids.append(await store.enqueue(cand, max_chars=1000))
    # Mark them delivered with staggered timestamps via direct SQL.
    base = datetime.now(UTC) - timedelta(hours=2)
    for i, item_id in enumerate(ids):
        ts = (base + timedelta(minutes=i * 5)).isoformat()
        await store.db.execute(
            "UPDATE items SET status = ?, delivered_at = ? WHERE id = ?",
            (Status.DELIVERED.value, ts, item_id),
        )
    await store.db.commit()

    history = await store.recent_history(window_hours=24)
    assert [h.text for h in history] == ["text-0", "text-1", "text-2"]


@pytest.mark.asyncio
async def test_record_decision_audit(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:6")
    item_id = await store.enqueue(cand, max_chars=1000)
    result = DecisionResult(
        decision=Decision.DELIVER,
        reason="novel",
        duplicate_of_id=None,
        model="claude-haiku-4-5",
        input_tokens=120,
        output_tokens=14,
        cache_creation_input_tokens=80,
        cache_read_input_tokens=0,
        latency_ms=350,
    )
    await store.record_decision(item_id, result)
    cur = await store.db.execute("SELECT * FROM decisions WHERE item_id = ?", (item_id,))
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["model"] == "claude-haiku-4-5"
    assert rows[0]["cache_creation_input_tokens"] == 80


@pytest.mark.asyncio
async def test_prune_deletes_old_delivered(store: Store) -> None:
    cand = make_candidate(source_id="tg:1:7")
    item_id = await store.enqueue(cand, max_chars=1000)
    very_old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    await store.db.execute(
        "UPDATE items SET status = ?, delivered_at = ? WHERE id = ?",
        (Status.DELIVERED.value, very_old, item_id),
    )
    await store.db.commit()
    deleted = await store.prune(window_hours=24)
    assert deleted["items_delivered"] == 1
    assert await store.get_item(item_id) is None


@pytest.mark.asyncio
async def test_has_source_id(store: Store) -> None:
    cand = make_candidate(source=Source.REDDIT, source_id="t3_abc")
    await store.enqueue(cand, max_chars=1000)
    assert await store.has_source_id(Source.REDDIT, "t3_abc") is True
    assert await store.has_source_id(Source.REDDIT, "t3_other") is False
