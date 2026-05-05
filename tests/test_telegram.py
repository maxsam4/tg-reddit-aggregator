"""Telegram producer + sender."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aggregator.models import Source, Status, StoredItem
from aggregator.telegram import (
    TelegramSender,
    candidate_from_album,
    candidate_from_message,
)
from tests.conftest import FakeTelethonClient


def _msg(
    *, message_id: int, text: str = "", grouped_id: int | None = None,
    chat_id: int = -1001, has_media: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        message=text,
        text=text,
        grouped_id=grouped_id,
        chat_id=chat_id,
        date=datetime(2026, 5, 5, 12, 0, tzinfo=UTC),
        media=object() if has_media else None,
    )


def test_candidate_from_message_basic() -> None:
    m = _msg(message_id=42, text="big news today")
    c = candidate_from_message(m, channel_title="ChannelA")
    assert c.source == Source.TELEGRAM
    assert c.source_id == "tg:-1001:42"
    assert "big news today" in c.text
    assert "ChannelA" in c.text
    assert c.media_group_id is None
    assert c.payload["message_ids"] == [42]


def test_candidate_from_message_marks_media() -> None:
    m = _msg(message_id=43, text="", has_media=True)
    c = candidate_from_message(m, channel_title="ChannelA")
    assert "[media]" in c.text
    assert c.payload["has_media"] is True


def test_candidate_from_album_groups_messages() -> None:
    msgs = [
        _msg(message_id=10, text="album caption", grouped_id=999, has_media=True),
        _msg(message_id=11, grouped_id=999, has_media=True),
        _msg(message_id=12, grouped_id=999, has_media=True),
    ]
    c = candidate_from_album(msgs, channel_title="ChannelA")
    assert c.source == Source.TELEGRAM
    # Stable id keyed on smallest message_id.
    assert c.source_id == "tg:-1001:album:10"
    assert c.media_group_id == "999"
    assert c.payload["message_ids"] == [10, 11, 12]
    assert c.payload["is_album"] is True
    # Caption is preserved (not "[media]").
    assert "album caption" in c.text


def test_candidate_from_album_synthesizes_caption_if_none() -> None:
    msgs = [
        _msg(message_id=10, text="", grouped_id=999, has_media=True),
        _msg(message_id=11, text="", grouped_id=999, has_media=True),
    ]
    c = candidate_from_album(msgs, channel_title="ChannelA")
    assert "album of 2 items" in c.text


def _stored_tg_item(payload: dict) -> StoredItem:
    return StoredItem(
        id=1,
        source=Source.TELEGRAM,
        source_id="tg:-1001:42",
        text="hello",
        created_at=datetime.now(UTC),
        observed_at=datetime.now(UTC),
        status=Status.DECIDED,
        attempts=0,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_sender_native_forward(fake_telethon: FakeTelethonClient) -> None:
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    ok = await sender.deliver(item)
    assert ok is True
    assert fake_telethon.forwarded == [
        {"entity": "@dest", "messages": [42], "from_peer": -1001}
    ]
    assert fake_telethon.sent_messages == []


@pytest.mark.asyncio
async def test_sender_falls_back_to_copy_on_restriction(
    fake_telethon: FakeTelethonClient,
) -> None:
    # Build a fake exception class whose name contains "ChatForwardsRestricted".
    class ChatForwardsRestrictedError(Exception):
        pass

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    item.text = "important news"
    ok = await sender.deliver(item)
    assert ok is True
    assert len(fake_telethon.sent_messages) == 1
    sent = fake_telethon.sent_messages[0]
    assert sent["entity"] == "@dest"
    assert "📎 from ChannelA" in sent["message"]
    assert "important news" in sent["message"]


@pytest.mark.asyncio
async def test_sender_returns_false_on_missing_payload(
    fake_telethon: FakeTelethonClient,
) -> None:
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item({})
    ok = await sender.deliver(item)
    assert ok is False
