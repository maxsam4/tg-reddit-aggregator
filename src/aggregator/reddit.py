"""Reddit poller (asyncpraw) and standalone-message sender.

The Telethon client (passed in) is used to post Reddit items to the Telegram destination,
since the userbot owns the destination identity for both sources.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from .models import Candidate, Source, StoredItem

log = logging.getLogger(__name__)

EnqueueFn = Callable[[Candidate], Awaitable[None]]


class RedditClientLike(Protocol):
    async def subreddit(self, name: str) -> Any: ...
    async def close(self) -> None: ...


class TelethonSenderLike(Protocol):
    async def send_message(self, entity: Any, message: str, **kwargs: Any) -> Any: ...


def _url_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.")
    except Exception:
        return ""


def candidate_from_submission(submission: Any) -> Candidate:
    """Normalize a PRAW Submission into a Candidate.

    Self-posts use title + selftext for dedup. Link posts have no body, so we synthesize
    "title — host" so Claude has signal to compare against other items.
    """
    title = getattr(submission, "title", "") or ""
    is_self = bool(getattr(submission, "is_self", False))
    selftext = getattr(submission, "selftext", "") or ""
    url = getattr(submission, "url", None)
    permalink = getattr(submission, "permalink", "") or ""
    full_permalink = f"https://reddit.com{permalink}" if permalink else None
    fullname = getattr(submission, "fullname", None) or f"t3_{submission.id}"
    subreddit_name = (
        getattr(getattr(submission, "subreddit", None), "display_name", None)
        or "unknown"
    )
    author = (
        getattr(getattr(submission, "author", None), "name", None) or "[deleted]"
    )
    created = getattr(submission, "created_utc", None)
    if isinstance(created, (int, float)):
        created_dt = datetime.fromtimestamp(created, tz=UTC)
    else:
        created_dt = datetime.now(UTC)

    if is_self:
        text = f"r/{subreddit_name} | {title}\n\n{selftext}".strip()
    else:
        host = _url_host(url)
        text = f"r/{subreddit_name} | {title} — {host}".strip(" —")

    return Candidate(
        source=Source.REDDIT,
        source_id=fullname,
        text=text,
        created_at=created_dt,
        url=full_permalink,
        payload={
            "title": title,
            "author": author,
            "subreddit": subreddit_name,
            "is_self": is_self,
            "selftext": selftext,
            "url": url,
            "permalink": full_permalink,
        },
    )


def format_reddit_message(item: StoredItem, snippet_chars: int = 500) -> str:
    """Render a delivered Reddit item as a standalone Telegram message."""
    p = item.payload or {}
    sub = p.get("subreddit", "?")
    author = p.get("author", "?")
    title = p.get("title", "")
    permalink = p.get("permalink") or item.url or ""
    is_self = bool(p.get("is_self"))
    selftext = (p.get("selftext") or "").strip()
    link_url = p.get("url") or ""

    header = f"🔸 r/{sub} · u/{author}"
    body_lines: list[str] = [header, title]
    if is_self and selftext:
        snippet = selftext[:snippet_chars]
        if len(selftext) > snippet_chars:
            snippet += "…"
        body_lines.extend(["", snippet])
    elif not is_self and link_url:
        body_lines.extend(["", link_url])
    if permalink:
        body_lines.extend(["", permalink])
    return "\n".join(body_lines).strip()


class RedditSender:
    """Posts Reddit items to the Telegram destination via the userbot."""

    def __init__(
        self,
        telegram_client: TelethonSenderLike,
        destination: str | int,
    ) -> None:
        self.telegram_client = telegram_client
        self.destination = destination

    async def deliver(self, item: StoredItem) -> bool:
        text = format_reddit_message(item)
        try:
            await self.telegram_client.send_message(
                self.destination, text, link_preview=True
            )
            return True
        except TypeError:
            # Some Telethon versions use different kwarg names; retry without kwargs.
            await self.telegram_client.send_message(self.destination, text)
            return True
        except Exception as e:
            log.exception("Reddit→Telegram delivery failed for item %s: %s", item.id, e)
            raise


class RedditPoller:
    """Periodically fetches new submissions from each configured subreddit."""

    def __init__(
        self,
        client: RedditClientLike,
        subreddits: list[str],
        enqueue: EnqueueFn,
        already_seen: Callable[[str], Awaitable[bool]],
        poll_interval_seconds: int = 60,
        posts_per_poll: int = 25,
    ) -> None:
        self.client = client
        self.subreddits = subreddits
        self.enqueue = enqueue
        self.already_seen = already_seen
        self.poll_interval_seconds = poll_interval_seconds
        self.posts_per_poll = posts_per_poll
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            for sub_name in self.subreddits:
                try:
                    await self._poll_one(sub_name)
                except Exception:
                    log.exception("Polling r/%s failed; will retry next cycle", sub_name)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _poll_one(self, sub_name: str) -> None:
        subreddit = await self.client.subreddit(sub_name)
        async for submission in subreddit.new(limit=self.posts_per_poll):
            cand = candidate_from_submission(submission)
            if await self.already_seen(cand.source_id):
                continue
            await self.enqueue(cand)
