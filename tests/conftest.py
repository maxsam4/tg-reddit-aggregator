"""Shared fixtures and fakes for tg-reddit-aggregator tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from aggregator.models import Candidate, Decision, Source
from aggregator.store import Store

# ---------- shared helpers ----------


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "state.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()


def make_candidate(
    *,
    source: Source = Source.TELEGRAM,
    source_id: str | None = None,
    text: str = "Some news headline",
    media_group_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Candidate:
    return Candidate(
        source=source,
        source_id=source_id or f"{source.value}:{id(text)}",
        text=text,
        created_at=datetime.now(UTC),
        media_group_id=media_group_id,
        payload=payload or {},
    )


# ---------- fake Anthropic ----------


class _FakeUsage:
    def __init__(self, **kwargs: int) -> None:
        self.input_tokens = kwargs.get("input_tokens", 100)
        self.output_tokens = kwargs.get("output_tokens", 20)
        self.cache_creation_input_tokens = kwargs.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = kwargs.get("cache_read_input_tokens", 0)


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, input_data: dict[str, Any]) -> None:
        self.name = name
        self.input = input_data


class _FakeAnthropicResponse:
    def __init__(
        self,
        decision: Decision,
        reason: str = "ok",
        duplicate_of_id: int | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.content = [
            _FakeToolUseBlock(
                "record_decision",
                {
                    "decision": decision.value,
                    "reason": reason,
                    "duplicate_of_id": duplicate_of_id,
                },
            )
        ]
        self.usage = _FakeUsage(**(usage or {}))


@dataclass
class _FakeMessages:
    parent: FakeAnthropicClient

    async def create(self, **kwargs: Any) -> Any:
        self.parent.calls.append(kwargs)
        # Pop the next programmed response (or use the default).
        if self.parent.responses:
            r = self.parent.responses.popleft()
        else:
            r = self.parent.default_response
        if isinstance(r, Exception):
            raise r
        return r


class FakeAnthropicClient:
    """Programmable fake of anthropic.AsyncAnthropic."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: deque[Any] = deque()
        self.default_response: Any = _FakeAnthropicResponse(Decision.DELIVER, "default")
        self.messages = _FakeMessages(self)

    def queue(
        self,
        decision: Decision,
        reason: str = "ok",
        duplicate_of_id: int | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.responses.append(
            _FakeAnthropicResponse(decision, reason, duplicate_of_id, usage)
        )

    def queue_error(self, exc: Exception) -> None:
        self.responses.append(exc)


# ---------- fake Telethon client ----------


@dataclass
class FakeTelethonClient:
    """Tracks forward_messages and send_message calls; supports raising on demand."""

    forwarded: list[dict[str, Any]] = field(default_factory=list)
    sent_messages: list[dict[str, Any]] = field(default_factory=list)
    forward_should_raise: Exception | None = None
    send_should_raise: Exception | None = None

    async def forward_messages(self, entity: Any, messages: Any, from_peer: Any) -> Any:
        if self.forward_should_raise is not None:
            exc = self.forward_should_raise
            self.forward_should_raise = None  # one-shot by default
            raise exc
        self.forwarded.append(
            {"entity": entity, "messages": messages, "from_peer": from_peer}
        )
        return messages

    async def send_message(
        self, entity: Any, message: str, **kwargs: Any
    ) -> Any:
        if self.send_should_raise is not None:
            exc = self.send_should_raise
            self.send_should_raise = None
            raise exc
        self.sent_messages.append({"entity": entity, "message": message, "kwargs": kwargs})
        return None

    async def send_file(self, entity: Any, file: Any, **kwargs: Any) -> Any:
        return None

    async def get_entity(self, identifier: Any) -> Any:
        return _FakeChat(id=identifier, title=f"Title-{identifier}")

    def add_event_handler(self, callback: Any, event: Any) -> None:
        return None


@dataclass
class _FakeChat:
    id: Any
    title: str
    username: str | None = None


# ---------- fake asyncpraw ----------


@dataclass
class FakeSubmission:
    id: str
    title: str
    is_self: bool = True
    selftext: str = ""
    url: str | None = None
    permalink: str = ""
    fullname: str | None = None
    created_utc: float = 0.0
    subreddit_name: str = "test"
    author_name: str = "alice"

    @property
    def subreddit(self) -> Any:
        return type("Sub", (), {"display_name": self.subreddit_name})()

    @property
    def author(self) -> Any:
        return type("Auth", (), {"name": self.author_name})()


class _FakeAsyncIter:
    def __init__(self, items: list[FakeSubmission]) -> None:
        self._items = list(items)

    def __aiter__(self) -> _FakeAsyncIter:
        return self

    async def __anext__(self) -> FakeSubmission:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeSubreddit:
    def __init__(self, name: str, submissions: list[FakeSubmission]) -> None:
        self.display_name = name
        self._submissions = submissions

    def new(self, limit: int = 25) -> _FakeAsyncIter:
        return _FakeAsyncIter(self._submissions[:limit])


class FakeRedditClient:
    """Programmable fake of asyncpraw.Reddit."""

    def __init__(self) -> None:
        self.subreddit_data: dict[str, list[FakeSubmission]] = {}
        self.closed = False

    def add(self, sub_name: str, *submissions: FakeSubmission) -> None:
        self.subreddit_data.setdefault(sub_name, []).extend(submissions)

    async def subreddit(self, name: str) -> _FakeSubreddit:
        return _FakeSubreddit(name, list(self.subreddit_data.get(name, [])))

    async def close(self) -> None:
        self.closed = True


# ---------- pytest fixtures ----------


@pytest.fixture
def fake_anthropic() -> FakeAnthropicClient:
    return FakeAnthropicClient()


@pytest.fixture
def fake_telethon() -> FakeTelethonClient:
    return FakeTelethonClient()


@pytest.fixture
def fake_reddit() -> FakeRedditClient:
    return FakeRedditClient()
