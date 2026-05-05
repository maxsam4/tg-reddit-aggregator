"""Pipeline / Dispatcher: decision routing + retry semantics."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aggregator.filters import FiltersFile
from aggregator.models import (
    Decision,
    DecisionResult,
    DropReason,
    Source,
    Status,
)
from aggregator.pipeline import (
    MAX_CLAUDE_ATTEMPTS,
    MAX_DELIVERY_ATTEMPTS,
    Dispatcher,
)
from aggregator.store import Store
from tests.conftest import make_candidate


def _decision(decision: Decision, reason: str = "ok") -> DecisionResult:
    return DecisionResult(
        decision=decision,
        reason=reason,
        duplicate_of_id=None,
        model="fake",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        latency_ms=0,
    )


class _CountingSender:
    def __init__(self, *, succeed: bool = True, raise_exc: Exception | None = None) -> None:
        self.calls = 0
        self.succeed = succeed
        self.raise_exc = raise_exc

    async def deliver(self, item) -> bool:
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return self.succeed


class _ProgrammableDecide:
    def __init__(self, results: list[DecisionResult]) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, history, candidate):
        self.calls += 1
        if not self.results:
            raise AssertionError(f"decide_fn called {self.calls}x without programmed result")
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.mark.asyncio
async def test_deliver_path_marks_delivered_and_history(store: Store, tmp_path) -> None:
    cand = make_candidate(source_id="tg:1:100", text="news A")
    item_id = await store.enqueue(cand, max_chars=1000)

    decide = _ProgrammableDecide([_decision(Decision.DELIVER, "novel")])
    sender = _CountingSender(succeed=True)
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide,
        senders={Source.TELEGRAM: sender},
    )
    item = (await store.claim_pending())[0]
    await d._process_one(item)

    saved = await store.get_item(item_id)
    assert saved.status == Status.DELIVERED
    assert saved.delivered_at is not None
    history = await store.recent_history(window_hours=24)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_duplicate_path_drops_without_history_pollution(
    store: Store, tmp_path
) -> None:
    cand = make_candidate(source_id="tg:1:101")
    item_id = await store.enqueue(cand, max_chars=1000)

    decide = _ProgrammableDecide([_decision(Decision.DUPLICATE, "same as id=14")])
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide,
        senders={Source.TELEGRAM: _CountingSender()},
    )
    item = (await store.claim_pending())[0]
    await d._process_one(item)

    saved = await store.get_item(item_id)
    assert saved.status == Status.DROPPED
    assert saved.drop_reason == DropReason.DUPLICATE
    assert saved.delivered_at is None
    assert (await store.recent_history(window_hours=24)) == []


@pytest.mark.asyncio
async def test_filtered_path_drops(store: Store, tmp_path) -> None:
    cand = make_candidate(source_id="tg:1:102")
    item_id = await store.enqueue(cand, max_chars=1000)
    decide = _ProgrammableDecide([_decision(Decision.FILTERED, "blocked")])
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide,
        senders={Source.TELEGRAM: _CountingSender()},
    )
    item = (await store.claim_pending())[0]
    await d._process_one(item)

    saved = await store.get_item(item_id)
    assert saved.status == Status.DROPPED
    assert saved.drop_reason == DropReason.FILTERED


@pytest.mark.asyncio
async def test_claude_failure_marks_retry_then_eventually_dropped(
    store: Store, tmp_path
) -> None:
    cand = make_candidate(source_id="tg:1:103")
    item_id = await store.enqueue(cand, max_chars=1000)

    # Always fail.
    decide = _ProgrammableDecide([RuntimeError("anthropic 500")] * (MAX_CLAUDE_ATTEMPTS + 2))
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide,
        senders={Source.TELEGRAM: _CountingSender()},
    )

    # First attempt: marked retry.
    item = (await store.claim_pending())[0]
    await d._process_one(item)
    saved = await store.get_item(item_id)
    assert saved.status == Status.RETRY
    assert saved.attempts == 1

    # Force eligibility immediately (avoid waiting for backoff in tests).
    for attempt in range(2, MAX_CLAUDE_ATTEMPTS + 1):
        await store.db.execute(
            "UPDATE items SET next_attempt_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), item_id),
        )
        await store.db.commit()
        items = await store.claim_pending()
        assert items, f"item not eligible at attempt {attempt}"
        await d._process_one(items[0])
        saved = await store.get_item(item_id)
        if saved.status == Status.DROPPED:
            break

    assert saved.status == Status.DROPPED
    assert saved.drop_reason == DropReason.CLAUDE_FAILED


@pytest.mark.asyncio
async def test_delivery_failure_retries_then_drops(store: Store, tmp_path) -> None:
    cand = make_candidate(source_id="tg:1:104")
    item_id = await store.enqueue(cand, max_chars=1000)

    # Decide returns DELIVER every time it's called.
    decide = _ProgrammableDecide([_decision(Decision.DELIVER, "novel")] * (MAX_DELIVERY_ATTEMPTS + 2))
    sender = _CountingSender(succeed=False)
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide,
        senders={Source.TELEGRAM: sender},
    )

    for _ in range(MAX_DELIVERY_ATTEMPTS + 1):
        items = await store.claim_pending()
        if not items:
            # bump next_attempt_at to now
            await store.db.execute(
                "UPDATE items SET next_attempt_at = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), item_id),
            )
            await store.db.commit()
            items = await store.claim_pending()
        await d._process_one(items[0])
        saved = await store.get_item(item_id)
        if saved.status == Status.DROPPED:
            break

    assert saved.status == Status.DROPPED
    assert saved.drop_reason == DropReason.DELIVERY_FAILED


@pytest.mark.asyncio
async def test_signal_overflow_does_not_crash(store: Store, tmp_path) -> None:
    """Producer-side `signal()` swallows QueueFull silently — polling fallback covers."""
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=_ProgrammableDecide([]),
        senders={},
        wakeup_queue_size=2,
    )
    d.signal()
    d.signal()
    d.signal()  # third would overflow — should be silently dropped


@pytest.mark.asyncio
async def test_enqueue_via_dispatcher_signals(store: Store, tmp_path) -> None:
    filters = FiltersFile(tmp_path / "f.md")
    d = Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=_ProgrammableDecide([]),
        senders={},
    )
    cand = make_candidate(source_id="tg:1:777")
    await d.enqueue(cand)
    # Wakeup queue should have a token waiting.
    assert d.wakeup.qsize() == 1
