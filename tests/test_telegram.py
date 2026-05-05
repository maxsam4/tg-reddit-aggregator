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
async def test_sender_falls_back_to_copy_uses_full_original_text(
    fake_telethon: FakeTelethonClient,
) -> None:
    """Copy fallback re-fetches originals and uses their FULL text — not the
    truncated dedup text on the StoredItem."""

    class ChatForwardsRestrictedError(Exception):
        pass

    # Original message has a long body that exceeds dedup truncation.
    full_body = "FULL ORIGINAL " + ("xxxx " * 500)
    original = _msg(message_id=42, text=full_body)
    fake_telethon.register_message(-1001, original)

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    item.text = "TRUNCATED dedup snippet"  # what was stored at enqueue time

    ok = await sender.deliver(item)
    assert ok is True
    # Text-only original (no media) → routes to send_message, not send_file.
    assert len(fake_telethon.sent_messages) == 1
    sent = fake_telethon.sent_messages[0]["message"]
    assert "📎 from ChannelA" in sent
    assert "FULL ORIGINAL" in sent  # full body preserved
    assert "TRUNCATED" not in sent  # truncated dedup text NOT used


@pytest.mark.asyncio
async def test_sender_falls_back_to_copy_with_media(
    fake_telethon: FakeTelethonClient,
) -> None:
    """Copy fallback for a message with media routes through send_file with caption."""

    class ChatForwardsRestrictedError(Exception):
        pass

    original = _msg(message_id=42, text="caption text", has_media=True)
    fake_telethon.register_message(-1001, original)

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    ok = await sender.deliver(item)
    assert ok is True
    assert len(fake_telethon.sent_files) == 1
    sent = fake_telethon.sent_files[0]
    assert sent["entity"] == "@dest"
    # Caption carries attribution + the original text.
    assert "📎 from ChannelA" in sent["kwargs"]["caption"]
    assert "caption text" in sent["kwargs"]["caption"]


@pytest.mark.asyncio
async def test_sender_copy_fallback_degrades_to_text_when_media_too_large(
    fake_telethon: FakeTelethonClient,
) -> None:
    """If send_file fails (e.g. media size limit), we send text-only with a
    `(media omitted)` note so the user still gets attribution + body."""

    class ChatForwardsRestrictedError(Exception):
        pass

    class FileTooBigError(Exception):
        pass

    original = _msg(message_id=42, text="body text", has_media=True)
    fake_telethon.register_message(-1001, original)

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    fake_telethon.send_file_should_raise = FileTooBigError("oversized")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    ok = await sender.deliver(item)
    assert ok is True
    # Routed through send_message after send_file failed.
    assert len(fake_telethon.sent_messages) == 1
    sent = fake_telethon.sent_messages[0]["message"]
    assert "📎 from ChannelA" in sent
    assert "body text" in sent
    assert "media omitted" in sent


@pytest.mark.asyncio
async def test_sender_copy_fallback_handles_missing_originals(
    fake_telethon: FakeTelethonClient,
) -> None:
    """If get_messages returns None for every id (deleted, inaccessible),
    we fall back to the truncated dedup text with attribution rather than crash."""

    class ChatForwardsRestrictedError(Exception):
        pass

    # No register_message → get_messages returns None for everything.
    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [42], "channel_title": "ChannelA"}
    )
    item.text = "best-available snippet"
    ok = await sender.deliver(item)
    assert ok is True
    assert len(fake_telethon.sent_messages) == 1
    sent = fake_telethon.sent_messages[0]["message"]
    assert "📎 from ChannelA" in sent
    assert "best-available snippet" in sent


@pytest.mark.asyncio
async def test_sender_copy_fallback_album_re_attribution_only_first(
    fake_telethon: FakeTelethonClient,
) -> None:
    """For an album, the attribution prefix only appears on the first item; the rest
    re-send their own captions/media without redundant prefixes."""

    class ChatForwardsRestrictedError(Exception):
        pass

    fake_telethon.register_message(
        -1001, _msg(message_id=10, text="first caption", has_media=True)
    )
    fake_telethon.register_message(
        -1001, _msg(message_id=11, text="", has_media=True)
    )
    fake_telethon.register_message(
        -1001, _msg(message_id=12, text="", has_media=True)
    )

    fake_telethon.forward_should_raise = ChatForwardsRestrictedError("nope")
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item(
        {"chat_id": -1001, "message_ids": [10, 11, 12], "channel_title": "ChannelA"}
    )
    ok = await sender.deliver(item)
    assert ok is True
    assert len(fake_telethon.sent_files) == 3
    # First file carries attribution and caption.
    first = fake_telethon.sent_files[0]
    assert "📎 from ChannelA" in first["kwargs"]["caption"]
    assert "first caption" in first["kwargs"]["caption"]
    # Subsequent files do NOT re-attribute.
    for f in fake_telethon.sent_files[1:]:
        cap = f["kwargs"].get("caption")
        assert cap is None or "📎 from" not in cap


@pytest.mark.asyncio
async def test_sender_returns_false_on_missing_payload(
    fake_telethon: FakeTelethonClient,
) -> None:
    sender = TelegramSender(fake_telethon, destination="@dest")
    item = _stored_tg_item({})
    ok = await sender.deliver(item)
    assert ok is False
