"""Dispatcher that drives items from queued → decided → delivered/dropped.

SQLite is the durable work queue. The asyncio.Queue here is purely a wakeup signal so
the dispatcher reacts immediately to new producer pushes; if it's full or the process
restarts, the polling fallback picks up within `idle_poll_seconds` seconds.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .filters import FiltersFile
from .models import (
    Candidate,
    Decision,
    DecisionResult,
    DropReason,
    Source,
    StoredItem,
)
from .store import Store

log = logging.getLogger(__name__)

# Cap on per-stage retries.
MAX_CLAUDE_ATTEMPTS = 6   # Claude transient failures
MAX_DELIVERY_ATTEMPTS = 5 # destination send failures

# Backoff bounds.
MIN_BACKOFF = timedelta(seconds=60)
MAX_BACKOFF = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def _backoff(attempts: int) -> datetime:
    """Exponential: 60s, 2m, 4m, 8m, ..., capped at 1h."""
    secs = min(MIN_BACKOFF.total_seconds() * (2 ** max(attempts - 1, 0)),
               MAX_BACKOFF.total_seconds())
    return _now() + timedelta(seconds=secs)


class SenderLike(Protocol):
    async def deliver(self, item: StoredItem) -> bool: ...


DecideFn = Callable[[list[StoredItem], Candidate], Awaitable[DecisionResult]]
"""(history, candidate-shaped-from-item) → DecisionResult."""


def candidate_view_of_item(item: StoredItem) -> Candidate:
    """A StoredItem at the dispatcher boundary still needs to be passed to decide()
    as a Candidate so the prompt builder treats it uniformly."""
    return Candidate(
        source=item.source,
        source_id=item.source_id,
        text=item.text,
        created_at=item.created_at,
        media_group_id=item.media_group_id,
        url=item.url,
        payload=item.payload,
    )


class Dispatcher:
    """Single-consumer dispatcher. Pull queued items, call Claude, route to senders."""

    def __init__(
        self,
        store: Store,
        filters_file: FiltersFile,
        decide_fn: DecideFn,
        senders: dict[Source, SenderLike],
        *,
        window_hours: int = 24,
        max_chars_per_item: int = 1000,
        idle_poll_seconds: float = 5.0,
        wakeup_queue_size: int = 256,
    ) -> None:
        self.store = store
        self.filters_file = filters_file
        self.decide_fn = decide_fn
        self.senders = senders
        self.window_hours = window_hours
        self.max_chars_per_item = max_chars_per_item
        self.idle_poll_seconds = idle_poll_seconds
        self.wakeup: asyncio.Queue[None] = asyncio.Queue(maxsize=wakeup_queue_size)
        self._stop = asyncio.Event()

    def signal(self) -> None:
        """Producer-side: nudge the dispatcher to look at the queue immediately."""
        try:
            self.wakeup.put_nowait(None)
        except asyncio.QueueFull:
            # Polling fallback (idle_poll_seconds) will catch up. Not a problem.
            pass

    def stop(self) -> None:
        self._stop.set()
        # Unblock the wakeup wait so the loop can exit promptly.
        try:
            self.wakeup.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def enqueue(self, candidate: Candidate) -> None:
        """Producer-side helper: persist + signal."""
        item_id = await self.store.enqueue(candidate, self.max_chars_per_item)
        if item_id is not None:
            self.signal()

    async def run(self) -> None:
        log.info("Dispatcher running (window=%dh)", self.window_hours)
        while not self._stop.is_set():
            self.filters_file.reload_if_changed()
            items = await self.store.claim_pending(limit=10)
            for item in items:
                if self._stop.is_set():
                    break
                await self._process_one(item)
            try:
                await asyncio.wait_for(self.wakeup.get(), timeout=self.idle_poll_seconds)
                # Drain any extra signals that arrived together.
                while not self.wakeup.empty():
                    self.wakeup.get_nowait()
            except TimeoutError:
                pass

    async def _process_one(self, item: StoredItem) -> None:
        # Pick up any filters.md edits before this decision. Cheap mtime stat.
        self.filters_file.reload_if_changed()
        log.debug("Dispatching item id=%s source=%s", item.id, item.source.value)

        # --- Claude decision (skip if this is a delivery-only retry) ---
        if item.decision is None:
            history = await self.store.recent_history(self.window_hours)
            try:
                result = await self.decide_fn(history, candidate_view_of_item(item))
            except Exception as e:
                log.warning(
                    "Claude decision failed for item %s (attempt %s): %s",
                    item.id, item.attempts + 1, e,
                )
                if item.attempts + 1 >= MAX_CLAUDE_ATTEMPTS:
                    log.error("Item %s exhausted Claude retries; dropping", item.id)
                    await self.store.mark_dropped(item.id, DropReason.CLAUDE_FAILED)
                    return
                await self.store.mark_retry(item.id, _backoff(item.attempts + 1))
                return

            await self.store.record_decision(item.id, result)
            await self.store.mark_decided(item.id, result.decision, result.reason)
            decision = result.decision
            log.info(
                "Decision for item %s: %s (%s)",
                item.id, decision.value, result.reason,
            )
        else:
            # Already decided in a previous tick; this is a delivery retry.
            decision = item.decision

        # --- Routing ---
        if decision == Decision.DUPLICATE:
            await self.store.mark_dropped(item.id, DropReason.DUPLICATE)
            return
        if decision == Decision.FILTERED:
            await self.store.mark_dropped(item.id, DropReason.FILTERED)
            return

        # decision == DELIVER
        sender = self.senders.get(item.source)
        if sender is None:
            log.error("No sender registered for source %s; dropping item %s",
                      item.source.value, item.id)
            await self.store.mark_dropped(item.id, DropReason.DELIVERY_FAILED)
            return

        # Refetch the item so attempts reflects the current row (mark_decided reset
        # attempts to 0, so the delivery-retry counter starts fresh on first delivery).
        current = await self.store.get_item(item.id)
        delivery_attempts = current.attempts if current else 0
        try:
            ok = await sender.deliver(item)
        except Exception as e:
            log.warning("Delivery raised for item %s (attempt %s): %s",
                        item.id, delivery_attempts + 1, e)
            ok = False

        if ok:
            await self.store.mark_delivered(item.id)
            return

        if delivery_attempts + 1 >= MAX_DELIVERY_ATTEMPTS:
            log.error("Item %s exhausted delivery retries; dropping", item.id)
            await self.store.mark_dropped(item.id, DropReason.DELIVERY_FAILED)
            return
        await self.store.mark_retry(item.id, _backoff(delivery_attempts + 1))


class Pruner:
    """Periodic pruning task."""

    def __init__(
        self,
        store: Store,
        window_hours: int = 24,
        interval_seconds: int = 3600,
    ) -> None:
        self.store = store
        self.window_hours = window_hours
        self.interval_seconds = interval_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                deleted = await self.store.prune(self.window_hours)
                if any(deleted.values()):
                    log.info("Pruner removed %s", deleted)
            except Exception:
                log.exception("Pruner cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
