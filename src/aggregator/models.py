"""Domain types: enums for source/decision/status, and the in-memory item dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Source(StrEnum):
    TELEGRAM = "telegram"
    REDDIT = "reddit"


class Decision(StrEnum):
    DELIVER = "deliver"
    DUPLICATE = "duplicate"
    FILTERED = "filtered"


class Status(StrEnum):
    QUEUED = "queued"
    RETRY = "retry"
    DECIDED = "decided"
    DELIVERED = "delivered"
    DROPPED = "dropped"


class DropReason(StrEnum):
    DUPLICATE = "duplicate"
    FILTERED = "filtered"
    DELIVERY_FAILED = "delivery_failed"
    CLAUDE_FAILED = "claude_failed"


@dataclass(frozen=True)
class Candidate:
    """A new item observed by a producer, before insertion into the store."""

    source: Source
    source_id: str
    text: str
    created_at: datetime
    media_group_id: str | None = None
    url: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoredItem:
    """An item as stored in the items table."""

    id: int
    source: Source
    source_id: str
    text: str
    created_at: datetime
    observed_at: datetime
    status: Status
    attempts: int
    media_group_id: str | None = None
    url: str | None = None
    next_attempt_at: datetime | None = None
    decision: Decision | None = None
    decision_reason: str | None = None
    delivered_at: datetime | None = None
    drop_reason: DropReason | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionResult:
    """Outcome of a Claude dedup call."""

    decision: Decision
    reason: str
    duplicate_of_id: int | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    latency_ms: int
