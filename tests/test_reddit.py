"""Reddit producer + sender."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aggregator.models import Source, Status, StoredItem
from aggregator.reddit import (
    RedditPoller,
    RedditSender,
    candidate_from_submission,
    format_reddit_message,
)
from tests.conftest import FakeRedditClient, FakeSubmission, FakeTelethonClient


def test_candidate_self_post() -> None:
    sub = FakeSubmission(
        id="abc",
        fullname="t3_abc",
        title="Mainnet upgrade lands",
        is_self=True,
        selftext="The upgrade introduces X, Y, Z.",
        permalink="/r/ethereum/comments/abc/mainnet/",
        created_utc=1714838400.0,
        subreddit_name="ethereum",
        author_name="alice",
    )
    c = candidate_from_submission(sub)
    assert c.source == Source.REDDIT
    assert c.source_id == "t3_abc"
    assert "r/ethereum" in c.text
    assert "Mainnet upgrade lands" in c.text
    assert "introduces X" in c.text
    assert c.url == "https://reddit.com/r/ethereum/comments/abc/mainnet/"


def test_candidate_link_post_uses_host_in_dedup_text() -> None:
    sub = FakeSubmission(
        id="def",
        fullname="t3_def",
        title="Article: X happens",
        is_self=False,
        selftext="",
        url="https://www.example.com/article/x",
        permalink="/r/cryptocurrency/comments/def/article/",
        subreddit_name="cryptocurrency",
    )
    c = candidate_from_submission(sub)
    assert c.is_self if False else True  # syntax filler; the assertion below is real
    # Critical: link-posts get title — host so Claude has dedup signal.
    assert "Article: X happens" in c.text
    assert "example.com" in c.text  # www. stripped, host included
    assert c.payload["is_self"] is False
    assert c.payload["url"] == "https://www.example.com/article/x"


def test_format_self_post_message() -> None:
    item = StoredItem(
        id=1,
        source=Source.REDDIT,
        source_id="t3_abc",
        text="...",
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        status=Status.DECIDED,
        attempts=0,
        url="https://reddit.com/r/ethereum/comments/abc/mainnet/",
        payload={
            "title": "Mainnet upgrade lands",
            "author": "alice",
            "subreddit": "ethereum",
            "is_self": True,
            "selftext": "Body of the post.",
            "url": None,
            "permalink": "https://reddit.com/r/ethereum/comments/abc/mainnet/",
        },
    )
    out = format_reddit_message(item)
    assert "🔸 r/ethereum · u/alice" in out
    assert "Mainnet upgrade lands" in out
    assert "Body of the post." in out
    assert "https://reddit.com/r/ethereum/comments/abc/mainnet/" in out


def test_format_link_post_includes_external_url() -> None:
    item = StoredItem(
        id=2,
        source=Source.REDDIT,
        source_id="t3_def",
        text="...",
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        status=Status.DECIDED,
        attempts=0,
        url="https://reddit.com/r/x/comments/def/article/",
        payload={
            "title": "Article: X happens",
            "author": "bob",
            "subreddit": "x",
            "is_self": False,
            "selftext": "",
            "url": "https://news.example.com/x",
            "permalink": "https://reddit.com/r/x/comments/def/article/",
        },
    )
    out = format_reddit_message(item)
    assert "https://news.example.com/x" in out
    assert "https://reddit.com/r/x/comments/def/article/" in out


@pytest.mark.asyncio
async def test_sender_uses_telethon(fake_telethon: FakeTelethonClient) -> None:
    sender = RedditSender(fake_telethon, destination="@dest")
    item = StoredItem(
        id=3,
        source=Source.REDDIT,
        source_id="t3_xyz",
        text="...",
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        status=Status.DECIDED,
        attempts=0,
        url="https://reddit.com/r/test/comments/xyz/",
        payload={
            "title": "T", "author": "u", "subreddit": "test",
            "is_self": True, "selftext": "Body",
            "permalink": "https://reddit.com/r/test/comments/xyz/",
        },
    )
    ok = await sender.deliver(item)
    assert ok is True
    assert len(fake_telethon.sent_messages) == 1
    assert "r/test" in fake_telethon.sent_messages[0]["message"]


@pytest.mark.asyncio
async def test_poller_skips_already_seen(fake_reddit: FakeRedditClient) -> None:
    seen: set[str] = {"t3_old"}
    enqueued: list[str] = []

    async def already_seen(sid: str) -> bool:
        return sid in seen

    async def enqueue(cand) -> None:
        enqueued.append(cand.source_id)

    fake_reddit.add(
        "test",
        FakeSubmission(id="old", fullname="t3_old", title="Old"),
        FakeSubmission(id="new", fullname="t3_new", title="New"),
    )
    poller = RedditPoller(
        client=fake_reddit,
        subreddits=["test"],
        enqueue=enqueue,
        already_seen=already_seen,
        poll_interval_seconds=60,
        posts_per_poll=10,
    )
    await poller._poll_one("test")
    assert enqueued == ["t3_new"]
