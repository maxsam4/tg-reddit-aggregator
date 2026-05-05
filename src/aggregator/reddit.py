"""Reddit poller (unauthenticated public JSON endpoint) and standalone-message sender.

Hits ``https://old.reddit.com/r/{name}/new.json`` with a custom User-Agent. No script
app, OAuth, or username/password is required. Trade-off: Reddit's unauthenticated
rate limit is ~10 requests/minute per IP, which is plenty for a handful of subreddits
polled every 60s but will start dropping requests if you scale much higher.

The Telethon client (passed in) is used to post Reddit items to the Telegram
destination, since the userbot owns the destination identity for both sources.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from .models import Candidate, Source, StoredItem

log = logging.getLogger(__name__)

EnqueueFn = Callable[[Candidate], Awaitable[None]]

# old.reddit.com is more stable than www. for unauthenticated JSON access.
REDDIT_BASE_URL = "https://old.reddit.com"

# Reddit blocks generic / library default User-Agents. Operators should override
# this with something identifiable; the default is descriptive enough to avoid
# the worst auto-blocking but you should set REDDIT_USER_AGENT in .env to be safe.
DEFAULT_USER_AGENT = (
    "tg-reddit-aggregator/0.1 "
    "(+https://github.com/maxsam4/tg-reddit-aggregator)"
)


@dataclass(frozen=True)
class RedditSubmission:
    """Normalized view of a Reddit submission. Mirrors the fields PRAW exposed
    so candidate_from_submission stays agnostic of the fetch mechanism."""

    id: str
    fullname: str
    title: str
    is_self: bool
    selftext: str
    url: str | None
    permalink: str
    subreddit_name: str
    author_name: str
    created_utc: float

    @classmethod
    def from_listing_child(cls, child: dict[str, Any]) -> RedditSubmission:
        """Parse a single 'child' object out of Reddit's listing JSON shape."""
        d = child.get("data", {}) or {}
        sid = d.get("id") or ""
        return cls(
            id=sid,
            fullname=d.get("name") or (f"t3_{sid}" if sid else ""),
            title=d.get("title") or "",
            is_self=bool(d.get("is_self")),
            selftext=d.get("selftext") or "",
            url=d.get("url"),
            permalink=d.get("permalink") or "",
            subreddit_name=d.get("subreddit") or "",
            author_name=d.get("author") or "[deleted]",
            created_utc=float(d.get("created_utc") or 0.0),
        )


class TelethonSenderLike(Protocol):
    async def send_message(self, entity: Any, message: str, **kwargs: Any) -> Any: ...


# Fetcher callable contract: given (subreddit_name, limit), return submissions.
# Production uses make_httpx_fetcher(); tests inject a programmable in-memory fake.
RedditFetcher = Callable[[str, int], Awaitable[list[RedditSubmission]]]


def make_httpx_fetcher(
    user_agent: str = DEFAULT_USER_AGENT,
    base_url: str = REDDIT_BASE_URL,
    timeout: float = 20.0,
) -> tuple[RedditFetcher, Callable[[], Awaitable[None]]]:
    """Build a default httpx-backed fetcher. Returns (fetch_fn, close_fn).

    The caller is responsible for awaiting close_fn() at shutdown.
    """
    client = httpx.AsyncClient(
        headers={"User-Agent": user_agent},
        timeout=timeout,
        follow_redirects=True,
    )

    async def fetch(sub_name: str, limit: int) -> list[RedditSubmission]:
        url = f"{base_url}/r/{sub_name}/new.json?limit={limit}"
        try:
            resp = await client.get(url)
        except httpx.TimeoutException as e:
            log.warning("Reddit GET timeout for r/%s: %s", sub_name, e)
            return []
        if resp.status_code == 429:
            log.warning("Reddit 429 (rate limit) for r/%s; backing off", sub_name)
            return []
        if resp.status_code in (403, 451):
            log.warning(
                "Reddit %d for r/%s; auth-required or geo-blocked endpoint, skipping",
                resp.status_code, sub_name,
            )
            return []
        resp.raise_for_status()
        body = resp.json()
        children = (body.get("data") or {}).get("children") or []
        return [RedditSubmission.from_listing_child(c) for c in children]

    async def close() -> None:
        await client.aclose()

    return fetch, close


def _url_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
        return host.removeprefix("www.")
    except Exception:
        return ""


def candidate_from_submission(submission: Any) -> Candidate:
    """Normalize a Reddit submission into a Candidate.

    Self-posts use title + selftext for dedup. Link-posts have no body, so we
    synthesize "title — host" so Claude has signal to compare against other items.
    Accepts both the new RedditSubmission dataclass and any duck-typed object
    exposing the same field names (used by the test suite).
    """
    title = getattr(submission, "title", "") or ""
    is_self = bool(getattr(submission, "is_self", False))
    selftext = getattr(submission, "selftext", "") or ""
    url = getattr(submission, "url", None)
    permalink = getattr(submission, "permalink", "") or ""
    full_permalink = f"https://reddit.com{permalink}" if permalink else None
    fullname = (
        getattr(submission, "fullname", None)
        or f"t3_{getattr(submission, 'id', '')}"
    )

    # Subreddit/author names: prefer flat attributes (RedditSubmission dataclass /
    # FakeSubmission), then fall back to the PRAW-style nested form.
    subreddit_name = getattr(submission, "subreddit_name", None)
    if not subreddit_name:
        subreddit_name = (
            getattr(getattr(submission, "subreddit", None), "display_name", None)
            or "unknown"
        )
    author = getattr(submission, "author_name", None)
    if not author:
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
    """Periodically calls the configured fetcher and enqueues new submissions."""

    def __init__(
        self,
        fetcher: RedditFetcher,
        subreddits: list[str],
        enqueue: EnqueueFn,
        already_seen: Callable[[str], Awaitable[bool]],
        poll_interval_seconds: int = 60,
        posts_per_poll: int = 25,
    ) -> None:
        self.fetcher = fetcher
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
                    log.exception(
                        "Polling r/%s failed; will retry next cycle", sub_name
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _poll_one(self, sub_name: str) -> None:
        submissions = await self.fetcher(sub_name, self.posts_per_poll)
        for submission in submissions:
            if not submission.fullname:
                continue
            if await self.already_seen(submission.fullname):
                continue
            cand = candidate_from_submission(submission)
            await self.enqueue(cand)
