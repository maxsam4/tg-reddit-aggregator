"""Telegram userbot listener (NewMessage + Album) and forward/copy sender.

A single Telethon client is used for BOTH reading source channels and posting to the
destination group. The user account must be a member of every source channel and of
the destination group.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from .models import Candidate, Source, StoredItem

log = logging.getLogger(__name__)

# Telethon's free-account upload limit; we degrade to text-only above this.
MAX_REUPLOAD_BYTES = 50 * 1024 * 1024

EnqueueFn = Callable[[Candidate], Awaitable[None]]


class TelethonClientLike(Protocol):
    """Subset of Telethon's TelegramClient that we exercise (for testability)."""

    async def get_entity(self, identifier: Any) -> Any: ...
    def add_event_handler(self, callback: Any, event: Any) -> None: ...
    async def forward_messages(
        self, entity: Any, messages: Any, from_peer: Any
    ) -> Any: ...
    async def send_message(self, entity: Any, message: str, **kwargs: Any) -> Any: ...
    async def send_file(self, entity: Any, file: Any, **kwargs: Any) -> Any: ...
    async def get_messages(self, entity: Any, ids: Any) -> Any: ...


def normalize_telegram_text(message: Any) -> str:
    """Get a textual representation of a Telegram message for dedup."""
    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    if not text and getattr(message, "media", None):
        text = "[media]"
    return text


def candidate_from_message(message: Any, channel_title: str) -> Candidate:
    """Build a Candidate from a single Telethon Message."""
    chat_id = getattr(message, "chat_id", None) or getattr(message, "peer_id", None)
    msg_id = message.id
    text = normalize_telegram_text(message)
    grouped_id = getattr(message, "grouped_id", None)
    created = getattr(message, "date", None) or datetime.now(UTC)
    if isinstance(created, datetime) and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return Candidate(
        source=Source.TELEGRAM,
        source_id=f"tg:{chat_id}:{msg_id}",
        text=f"[{channel_title}] {text}".strip(),
        created_at=created,
        media_group_id=str(grouped_id) if grouped_id else None,
        url=None,  # Telethon doesn't always expose t.me link; not needed for forward
        payload={
            "chat_id": chat_id,
            "message_ids": [msg_id],
            "channel_title": channel_title,
            "has_media": bool(getattr(message, "media", None)),
        },
    )


def candidate_from_album(messages: list[Any], channel_title: str) -> Candidate:
    """Build a single Candidate from a Telegram album (group of media messages)."""
    if not messages:
        raise ValueError("candidate_from_album: messages must be non-empty")
    chat_id = getattr(messages[0], "chat_id", None)
    grouped_id = getattr(messages[0], "grouped_id", None)
    # Caption is on whichever message has one (usually the first).
    caption = ""
    for m in messages:
        text = normalize_telegram_text(m)
        if text and text != "[media]":
            caption = text
            break
    if not caption:
        caption = f"[album of {len(messages)} items]"
    msg_ids = [m.id for m in messages]
    earliest = min(
        (m.date for m in messages if getattr(m, "date", None)),
        default=datetime.now(UTC),
    )
    if isinstance(earliest, datetime) and earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=UTC)
    return Candidate(
        source=Source.TELEGRAM,
        # Use the smallest msg_id as the canonical id so we have a stable key per album.
        source_id=f"tg:{chat_id}:album:{min(msg_ids)}",
        text=f"[{channel_title}] {caption}".strip(),
        created_at=earliest,
        media_group_id=str(grouped_id) if grouped_id else None,
        url=None,
        payload={
            "chat_id": chat_id,
            "message_ids": msg_ids,
            "channel_title": channel_title,
            "has_media": True,
            "is_album": True,
        },
    )


class TelegramSender:
    """Forwards Telegram items to the destination group, with copy fallback."""

    def __init__(self, client: TelethonClientLike, destination: str | int) -> None:
        self.client = client
        self.destination = destination

    async def deliver(self, item: StoredItem) -> bool:
        """Forward (or copy-fallback) an item. Returns True on success."""
        chat_id = item.payload.get("chat_id")
        message_ids = item.payload.get("message_ids", [])
        channel_title = item.payload.get("channel_title", "channel")
        if not chat_id or not message_ids:
            log.error("Telegram item %s missing chat_id/message_ids in payload", item.id)
            return False
        try:
            await self.client.forward_messages(
                self.destination, message_ids, from_peer=chat_id
            )
            return True
        except Exception as e:
            # Heuristic: if forwarding is restricted, fall back to copy.
            err_name = type(e).__name__
            if "ChatForwardsRestricted" in err_name or "ForbiddenError" in err_name:
                return await self._copy_fallback(item, channel_title, chat_id, message_ids)
            log.exception("Telegram forward failed for item %s: %s", item.id, e)
            raise

    async def _copy_fallback(
        self,
        item: StoredItem,
        channel_title: str,
        chat_id: Any,
        message_ids: list[int],
    ) -> bool:
        """Copy a forward-restricted message by re-fetching originals.

        For each original message we try send_file (when it has media) or send_message
        (text only), preserving the original full body — NOT the truncated dedup text.
        Attribution is prepended to the first item only. Per-item failures (oversized
        media, etc.) degrade to text-only with a "(media omitted)" note.
        """
        prefix = f"📎 from {channel_title}"

        # Re-fetch the original messages so we have full text and media handles.
        try:
            fetched = await self.client.get_messages(chat_id, ids=message_ids)
        except Exception as e:
            log.warning(
                "get_messages failed for item %s; degrading to truncated text: %s",
                item.id, e,
            )
            fetched = None

        if fetched is None:
            messages: list[Any] = []
        elif isinstance(fetched, list):
            messages = [m for m in fetched if m is not None]
        else:
            messages = [fetched]

        if not messages:
            # Last-ditch: no originals available, send the (truncated) dedup text.
            text_body = item.text or "(message unavailable)"
            try:
                await self.client.send_message(self.destination, f"{prefix}\n\n{text_body}")
                log.warning(
                    "Item %s: copy fallback used truncated dedup text "
                    "(originals could not be fetched)",
                    item.id,
                )
                return True
            except Exception as e:
                log.exception("Telegram copy fallback failed for item %s: %s", item.id, e)
                return False

        # Send the first message with attribution prefix.
        first = messages[0]
        first_text = getattr(first, "message", "") or getattr(first, "text", "") or ""
        first_caption = f"{prefix}\n\n{first_text}".strip() if first_text else prefix

        if await self._send_one(first, caption=first_caption, item_id=item.id) is False:
            return False

        # Send the rest of the album (or extra messages) without re-attribution.
        for m in messages[1:]:
            m_text = getattr(m, "message", "") or getattr(m, "text", "") or ""
            await self._send_one(m, caption=m_text or None, item_id=item.id)

        log.warning(
            "Item %s delivered via copy fallback (forward-restricted source)", item.id
        )
        return True

    async def _send_one(
        self, message: Any, *, caption: str | None, item_id: int
    ) -> bool:
        """Send one message via send_file (if it has media) or send_message.
        Returns False only on a hard failure of the text-only fallback path."""
        has_media = bool(getattr(message, "media", None))
        if has_media:
            try:
                kwargs: dict[str, Any] = {}
                if caption is not None:
                    kwargs["caption"] = caption
                await self.client.send_file(self.destination, message, **kwargs)
                return True
            except Exception as e:
                log.warning(
                    "Item %s: media re-upload failed (%s); degrading to text-only",
                    item_id, e,
                )
                # Fall through to send_message with a (media omitted) note.
                degraded = (caption or "").rstrip()
                degraded = (degraded + "\n\n(media omitted — see source channel)").strip()
                try:
                    await self.client.send_message(self.destination, degraded)
                    return True
                except Exception as e2:
                    log.exception("Item %s: degraded text send also failed: %s", item_id, e2)
                    return False

        # Text-only message
        if not caption:
            return True  # nothing to send
        try:
            await self.client.send_message(self.destination, caption)
            return True
        except Exception as e:
            log.exception("Item %s: text send failed: %s", item_id, e)
            return False


class TelegramListener:
    """Wires Telethon NewMessage + Album events to an enqueue callback."""

    def __init__(
        self,
        client: TelethonClientLike,
        channels: list[str | int],
        enqueue: EnqueueFn,
    ) -> None:
        self.client = client
        self.channels = channels
        self.enqueue = enqueue
        self._channel_titles: dict[Any, str] = {}

    async def start(self) -> None:
        """Resolve channel entities and register event handlers."""
        # Local imports so this module can be imported without telethon at test-time.
        from telethon import events  # type: ignore[import-not-found]
        from telethon.utils import get_peer_id  # type: ignore[import-not-found]

        resolved = []
        for ch in self.channels:
            entity = await self.client.get_entity(ch)
            resolved.append(entity)
            title = (
                getattr(entity, "title", None)
                or getattr(entity, "username", None)
                or str(ch)
            )
            # event.chat_id returns the *marked* peer id form (e.g. -100... for
            # channels/supergroups), so we must key the title map by the same
            # canonicalization to look it up reliably from the event handlers.
            try:
                key = get_peer_id(entity)
            except Exception:
                key = getattr(entity, "id", ch)
            self._channel_titles[key] = title

        async def on_new_message(event: Any) -> None:
            try:
                if getattr(event.message, "grouped_id", None):
                    # Album path will handle it as a unit.
                    return
                title = self._channel_titles.get(event.chat_id, "channel")
                cand = candidate_from_message(event.message, title)
                await self.enqueue(cand)
            except Exception:
                log.exception("on_new_message handler failed")

        async def on_album(event: Any) -> None:
            try:
                title = self._channel_titles.get(event.chat_id, "channel")
                cand = candidate_from_album(list(event.messages), title)
                await self.enqueue(cand)
            except Exception:
                log.exception("on_album handler failed")

        self.client.add_event_handler(
            on_new_message, events.NewMessage(chats=resolved)
        )
        self.client.add_event_handler(on_album, events.Album(chats=resolved))
