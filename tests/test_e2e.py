"""End-to-end tests: producers → SQLite queue → dispatcher → Claude → senders.

These tests wire the real Dispatcher, Store, and FiltersFile to the fake Anthropic,
fake Telethon, and fake Reddit clients. They cover the full scenarios listed in the
plan's verification section, with the exception of items requiring real network
(Telegram channel resolution, Reddit live API).

Each test is fully deterministic — no real time-based waits beyond brief asyncio yields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aggregator.dedup import decide as dedup_decide
from aggregator.filters import FiltersFile
from aggregator.models import Decision, DropReason, Source, Status
from aggregator.pipeline import Dispatcher
from aggregator.reddit import (
    RedditPoller,
    RedditSender,
    candidate_from_submission,
)
from aggregator.store import Store
from aggregator.telegram import (
    TelegramSender,
    candidate_from_album,
    candidate_from_message,
)
from tests.conftest import (
    FakeAnthropicClient,
    FakeRedditClient,
    FakeSubmission,
    FakeTelethonClient,
)

# ------------------------- helpers -------------------------


async def _drive_dispatcher_until_idle(d: Dispatcher, store: Store, max_iters: int = 20) -> None:
    """Process every claimable item by directly invoking the inner step.

    Avoids racing the run() loop and its 5s polling fallback in tests.
    """
    for _ in range(max_iters):
        items = await store.claim_pending()
        if not items:
            return
        for item in items:
            await d._process_one(item)


def _make_dispatcher(
    *,
    store: Store,
    filters_path: Path,
    anthropic: FakeAnthropicClient,
    senders: dict[Source, Any],
    max_chars: int = 1000,
) -> Dispatcher:
    filters = FiltersFile(filters_path)

    async def decide_fn(history, candidate):
        return await dedup_decide(
            anthropic,
            model="claude-haiku-4-5",
            filter_text=filters.text,
            history=history,
            candidate=candidate,
            max_chars=max_chars,
        )

    return Dispatcher(
        store=store,
        filters_file=filters,
        decide_fn=decide_fn,
        senders=senders,
        max_chars_per_item=max_chars,
    )


def _telegram_message(
    *, chat_id: int, message_id: int, text: str, grouped_id: int | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        message=text,
        text=text,
        chat_id=chat_id,
        grouped_id=grouped_id,
        date=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
        media=None,
    )


# ------------------------- end-to-end scenarios -------------------------


@pytest.mark.asyncio
async def test_e2e_telegram_deliver_path(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """A new Telegram message: enqueue → deliver decision → forwarded to destination."""
    fake_anthropic.queue(Decision.DELIVER, "novel breaking news")
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    # Producer side: a Telegram message arrives.
    msg = _telegram_message(chat_id=-100, message_id=1, text="Big news today")
    cand = candidate_from_message(msg, channel_title="ChannelA")
    await d.enqueue(cand)

    await _drive_dispatcher_until_idle(d, store)

    # Forwarded with native semantics.
    assert fake_telethon.forwarded == [
        {"entity": "@dest", "messages": [1], "from_peer": -100}
    ]
    # State is correct.
    item = (await store.recent_history(window_hours=24))[0]
    assert item.status == Status.DELIVERED
    assert item.delivered_at is not None
    # Decisions audit log written.
    cur = await store.db.execute("SELECT COUNT(*) AS n FROM decisions")
    row = await cur.fetchone()
    assert row["n"] == 1


@pytest.mark.asyncio
async def test_e2e_telegram_duplicate_path_does_not_pollute_history(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """Same story from a second channel is judged duplicate; the destination only sees it once."""
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    # First arrival: deliver.
    fake_anthropic.queue(Decision.DELIVER, "novel")
    cand1 = candidate_from_message(
        _telegram_message(chat_id=-100, message_id=1, text="X token listed on Y"),
        channel_title="ChannelA",
    )
    await d.enqueue(cand1)
    await _drive_dispatcher_until_idle(d, store)

    # Second arrival (different channel, same story): duplicate.
    fake_anthropic.queue(Decision.DUPLICATE, "same listing as id=1", duplicate_of_id=1)
    cand2 = candidate_from_message(
        _telegram_message(chat_id=-200, message_id=99, text="Y exchange adds X token"),
        channel_title="ChannelB",
    )
    await d.enqueue(cand2)
    await _drive_dispatcher_until_idle(d, store)

    # Only one forward.
    assert len(fake_telethon.forwarded) == 1
    # Recent history holds only the delivered one.
    history = await store.recent_history(window_hours=24)
    assert len(history) == 1
    assert history[0].source_id == "tg:-100:1"
    # The dropped item exists with the right reason and no delivered_at.
    cur = await store.db.execute(
        "SELECT status, drop_reason, delivered_at FROM items WHERE source_id = ?",
        ("tg:-200:99",),
    )
    dropped = await cur.fetchone()
    assert dropped["status"] == Status.DROPPED.value
    assert dropped["drop_reason"] == DropReason.DUPLICATE.value
    assert dropped["delivered_at"] is None


@pytest.mark.asyncio
async def test_e2e_filter_hot_reload(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """Editing filters.md changes Claude's system prompt without a restart."""
    filters_path = tmp_path / "f.md"
    filters_path.write_text("rule v1\n", encoding="utf-8")
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=filters_path,
        anthropic=fake_anthropic, senders=senders,
    )

    fake_anthropic.queue(Decision.DELIVER, "ok")
    await d.enqueue(
        candidate_from_message(
            _telegram_message(chat_id=-100, message_id=1, text="A"),
            channel_title="C",
        )
    )
    await _drive_dispatcher_until_idle(d, store)
    first_call_system = fake_anthropic.calls[0]["system"][0]["text"]
    assert "rule v1" in first_call_system

    # Edit filters.md and bump mtime so reload detects the change.
    import os
    import time as _t
    filters_path.write_text("rule v2\nfilter pump posts\n", encoding="utf-8")
    new_time = _t.time() + 5
    os.utime(filters_path, (new_time, new_time))

    fake_anthropic.queue(Decision.FILTERED, "matches pump rule")
    await d.enqueue(
        candidate_from_message(
            _telegram_message(chat_id=-100, message_id=2, text="PUMP signal"),
            channel_title="C",
        )
    )
    await _drive_dispatcher_until_idle(d, store)

    second_call_system = fake_anthropic.calls[1]["system"][0]["text"]
    assert "rule v2" in second_call_system
    assert "filter pump posts" in second_call_system
    # Filtered → only one forward (the first).
    assert len(fake_telethon.forwarded) == 1


@pytest.mark.asyncio
async def test_e2e_telegram_album_collapses_to_one_decision(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """A 3-photo album becomes ONE item, ONE Claude call, ONE forward."""
    fake_anthropic.queue(Decision.DELIVER, "ok")
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    msgs = [
        _telegram_message(chat_id=-100, message_id=10, text="album caption", grouped_id=999),
        _telegram_message(chat_id=-100, message_id=11, text="", grouped_id=999),
        _telegram_message(chat_id=-100, message_id=12, text="", grouped_id=999),
    ]
    # Inject `media=object()` so the album isn't entirely text-less.
    for m in msgs:
        m.media = object()
    cand = candidate_from_album(msgs, channel_title="ChannelA")
    await d.enqueue(cand)
    await _drive_dispatcher_until_idle(d, store)

    assert len(fake_anthropic.calls) == 1
    assert len(fake_telethon.forwarded) == 1
    # Forward includes all three message_ids.
    assert sorted(fake_telethon.forwarded[0]["messages"]) == [10, 11, 12]


@pytest.mark.asyncio
async def test_e2e_reddit_link_post_dedup_text_uses_host(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient, fake_reddit: FakeRedditClient,
) -> None:
    """Reddit link-posts (no selftext) are stored with `title — host` dedup text and the
    Telegram message includes the external URL plus the permalink."""
    fake_anthropic.queue(Decision.DELIVER, "ok")
    senders = {Source.REDDIT: RedditSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    sub = FakeSubmission(
        id="def", fullname="t3_def",
        title="Article: X happens",
        is_self=False, selftext="",
        url="https://news.example.com/x",
        permalink="/r/cryptocurrency/comments/def/article/",
        subreddit_name="cryptocurrency",
        author_name="bob",
        created_utc=1714838400.0,
    )
    cand = candidate_from_submission(sub)
    assert "example.com" in cand.text  # producer-side guarantee

    await d.enqueue(cand)
    await _drive_dispatcher_until_idle(d, store)

    assert len(fake_telethon.sent_messages) == 1
    sent = fake_telethon.sent_messages[0]["message"]
    assert "Article: X happens" in sent
    assert "https://news.example.com/x" in sent
    assert "https://reddit.com/r/cryptocurrency/comments/def/article/" in sent


@pytest.mark.asyncio
async def test_e2e_reddit_poller_skips_already_seen_across_polls(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient, fake_reddit: FakeRedditClient,
) -> None:
    """Two consecutive polls return the same submission; the second poll must skip it."""
    senders = {Source.REDDIT: RedditSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    fake_reddit.add(
        "test",
        FakeSubmission(
            id="r1", fullname="t3_r1", title="Reddit story",
            is_self=True, selftext="body", subreddit_name="test",
        ),
    )
    fake_anthropic.queue(Decision.DELIVER, "ok")

    poller = RedditPoller(
        client=fake_reddit,
        subreddits=["test"],
        enqueue=d.enqueue,
        already_seen=lambda sid: store.has_source_id(Source.REDDIT, sid),
        poll_interval_seconds=60,
    )

    # First poll: enqueues t3_r1, dispatcher delivers.
    await poller._poll_one("test")
    await _drive_dispatcher_until_idle(d, store)
    assert len(fake_telethon.sent_messages) == 1

    # Second poll returns the SAME submission (rotated back into the deque).
    fake_reddit.add(
        "test",
        FakeSubmission(
            id="r1", fullname="t3_r1", title="Reddit story",
            is_self=True, selftext="body", subreddit_name="test",
        ),
    )
    await poller._poll_one("test")
    await _drive_dispatcher_until_idle(d, store)
    # No second delivery, and Anthropic was not consulted again.
    assert len(fake_telethon.sent_messages) == 1
    assert len(fake_anthropic.calls) == 1


@pytest.mark.asyncio
async def test_e2e_cross_source_dedup_telegram_then_reddit(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """A Telegram post and then a Reddit submission about the same news: only the
    Telegram one is delivered."""
    senders = {
        Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest"),
        Source.REDDIT: RedditSender(fake_telethon, destination="@dest"),
    }
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )

    # 1. Telegram first: deliver.
    fake_anthropic.queue(Decision.DELIVER, "first to break the news")
    tg_cand = candidate_from_message(
        _telegram_message(chat_id=-100, message_id=1, text="Protocol Z hacked for $5M"),
        channel_title="CryptoNews",
    )
    await d.enqueue(tg_cand)
    await _drive_dispatcher_until_idle(d, store)
    assert len(fake_telethon.forwarded) == 1
    assert len(fake_telethon.sent_messages) == 0

    # 2. Reddit follows up about the same event: duplicate.
    fake_anthropic.queue(Decision.DUPLICATE, "same hack as id=1", duplicate_of_id=1)
    sub = FakeSubmission(
        id="abc", fullname="t3_abc",
        title="Z exploit drains $5M",
        is_self=False, selftext="",
        url="https://example.com/post",
        permalink="/r/cryptocurrency/comments/abc/z/",
        subreddit_name="cryptocurrency",
    )
    rd_cand = candidate_from_submission(sub)
    await d.enqueue(rd_cand)
    await _drive_dispatcher_until_idle(d, store)
    # Reddit was NOT sent.
    assert len(fake_telethon.sent_messages) == 0
    # Reddit item is dropped, not delivered.
    cur = await store.db.execute(
        "SELECT status, drop_reason, delivered_at FROM items WHERE source_id = ?",
        ("t3_abc",),
    )
    row = await cur.fetchone()
    assert row["status"] == Status.DROPPED.value
    assert row["drop_reason"] == DropReason.DUPLICATE.value
    assert row["delivered_at"] is None


@pytest.mark.asyncio
async def test_e2e_restart_safety_no_double_delivery(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """An item delivered before a restart must not be redelivered when a new dispatcher
    starts and a fresh poll re-encounters the same source_id."""
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d1 = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )
    fake_anthropic.queue(Decision.DELIVER, "ok")
    cand = candidate_from_message(
        _telegram_message(chat_id=-100, message_id=1, text="news"),
        channel_title="C",
    )
    await d1.enqueue(cand)
    await _drive_dispatcher_until_idle(d1, store)
    assert len(fake_telethon.forwarded) == 1

    # Simulate process restart: brand new dispatcher and new producer attempt.
    d2 = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )
    # Re-enqueueing the same source_id is idempotent — store returns None and
    # nothing new gets queued.
    await d2.enqueue(cand)
    await _drive_dispatcher_until_idle(d2, store)
    # No second forward, no second Claude call.
    assert len(fake_telethon.forwarded) == 1
    assert len(fake_anthropic.calls) == 1


@pytest.mark.asyncio
async def test_e2e_restart_safety_inflight_item_recovers(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """An item enqueued but never decided (process killed pre-Claude) is picked up by a
    new dispatcher on restart and processed exactly once."""
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}

    # Producer enqueues; no dispatcher runs (simulates crash before processing).
    cand = candidate_from_message(
        _telegram_message(chat_id=-100, message_id=1, text="news"),
        channel_title="C",
    )
    await store.enqueue(cand, max_chars=1000)
    pending_before = await store.claim_pending()
    assert len(pending_before) == 1
    assert pending_before[0].status == Status.QUEUED

    # New dispatcher comes up, finds the orphaned item, processes it.
    d2 = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )
    fake_anthropic.queue(Decision.DELIVER, "ok")
    await _drive_dispatcher_until_idle(d2, store)

    assert len(fake_telethon.forwarded) == 1
    history = await store.recent_history(window_hours=24)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_e2e_forward_restricted_falls_back_to_copy(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    """Source channel has noforwards=True: the sender copy-fallbacks instead of failing."""

    class ChatForwardsRestrictedError(Exception):
        pass

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")

    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )
    fake_anthropic.queue(Decision.DELIVER, "ok")
    cand = candidate_from_message(
        _telegram_message(chat_id=-100, message_id=1, text="protected news"),
        channel_title="LockedChannel",
    )
    await d.enqueue(cand)
    await _drive_dispatcher_until_idle(d, store)

    # No native forward succeeded; one copy-fallback message went out.
    assert fake_telethon.forwarded == []
    assert len(fake_telethon.sent_messages) == 1
    assert "📎 from LockedChannel" in fake_telethon.sent_messages[0]["message"]
    assert "protected news" in fake_telethon.sent_messages[0]["message"]
    # Item was marked delivered.
    history = await store.recent_history(window_hours=24)
    assert len(history) == 1


@pytest.mark.asyncio
async def test_e2e_filtered_decision_does_not_send(
    store: Store, tmp_path: Path, fake_anthropic: FakeAnthropicClient,
    fake_telethon: FakeTelethonClient,
) -> None:
    fake_anthropic.queue(Decision.FILTERED, "blocked by user filter rule")
    senders = {Source.TELEGRAM: TelegramSender(fake_telethon, destination="@dest")}
    d = _make_dispatcher(
        store=store, filters_path=tmp_path / "f.md",
        anthropic=fake_anthropic, senders=senders,
    )
    await d.enqueue(
        candidate_from_message(
            _telegram_message(chat_id=-100, message_id=1, text="PUMP signal"),
            channel_title="C",
        )
    )
    await _drive_dispatcher_until_idle(d, store)

    assert fake_telethon.forwarded == []
    assert fake_telethon.sent_messages == []
    # No history pollution.
    assert (await store.recent_history(window_hours=24)) == []
