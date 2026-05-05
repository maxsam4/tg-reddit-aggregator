"""aiosqlite storage layer. SQLite is the durable work queue and recent-history source."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from .models import (
    Candidate,
    Decision,
    DecisionResult,
    DropReason,
    Source,
    Status,
    StoredItem,
)

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        media_group_id TEXT,
        url TEXT,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        decision TEXT,
        decision_reason TEXT,
        delivered_at TEXT,
        drop_reason TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        UNIQUE(source, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_items_history ON items(status, delivered_at)",
    """
    CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id),
        decision TEXT NOT NULL,
        reason TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_creation_input_tokens INTEGER,
        cache_read_input_tokens INTEGER,
        latency_ms INTEGER,
        timestamp TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp)",
]


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _row_to_item(row: aiosqlite.Row) -> StoredItem:
    return StoredItem(
        id=row["id"],
        source=Source(row["source"]),
        source_id=row["source_id"],
        media_group_id=row["media_group_id"],
        url=row["url"],
        text=row["text"],
        created_at=_parse_dt(row["created_at"]),  # type: ignore[arg-type]
        observed_at=_parse_dt(row["observed_at"]),  # type: ignore[arg-type]
        status=Status(row["status"]),
        attempts=row["attempts"],
        next_attempt_at=_parse_dt(row["next_attempt_at"]),
        decision=Decision(row["decision"]) if row["decision"] else None,
        decision_reason=row["decision_reason"],
        delivered_at=_parse_dt(row["delivered_at"]),
        drop_reason=DropReason(row["drop_reason"]) if row["drop_reason"] else None,
        payload=json.loads(row["payload"] or "{}"),
    )


class Store:
    """Async SQLite store. Single-writer assumed (one dispatcher process)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        for stmt in _SCHEMA:
            await self._db.execute(stmt)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not open. Call await store.open() first.")
        return self._db

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # --- write side ---

    async def enqueue(self, candidate: Candidate, max_chars: int) -> int | None:
        """Insert a new candidate as status='queued'. Returns the item id, or None if it
        was already present (idempotent on (source, source_id))."""
        text = candidate.text[:max_chars]
        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO items
                (source, source_id, media_group_id, url, text,
                 created_at, observed_at, status, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.source.value,
                candidate.source_id,
                candidate.media_group_id,
                candidate.url,
                text,
                _iso(candidate.created_at),
                _iso(_now()),
                Status.QUEUED.value,
                json.dumps(candidate.payload),
            ),
        )
        await self.db.commit()
        return cur.lastrowid if cur.rowcount > 0 else None

    async def claim_pending(self, limit: int = 10) -> list[StoredItem]:
        """Return up to N items that are queued, or in retry with next_attempt_at <= now.

        Returns oldest-observed-first to preserve fairness.
        """
        now_iso = _iso(_now())
        cur = await self.db.execute(
            """
            SELECT * FROM items
            WHERE status = ?
               OR (status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
            ORDER BY observed_at ASC
            LIMIT ?
            """,
            (Status.QUEUED.value, Status.RETRY.value, now_iso, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_item(r) for r in rows]

    async def mark_decided(
        self, item_id: int, decision: Decision, reason: str
    ) -> None:
        await self.db.execute(
            """
            UPDATE items
               SET status = ?, decision = ?, decision_reason = ?, attempts = 0, next_attempt_at = NULL
             WHERE id = ?
            """,
            (Status.DECIDED.value, decision.value, reason, item_id),
        )
        await self.db.commit()

    async def mark_delivered(self, item_id: int) -> None:
        await self.db.execute(
            "UPDATE items SET status = ?, delivered_at = ?, attempts = 0, next_attempt_at = NULL WHERE id = ?",
            (Status.DELIVERED.value, _iso(_now()), item_id),
        )
        await self.db.commit()

    async def mark_dropped(self, item_id: int, drop_reason: DropReason) -> None:
        await self.db.execute(
            "UPDATE items SET status = ?, drop_reason = ? WHERE id = ?",
            (Status.DROPPED.value, drop_reason.value, item_id),
        )
        await self.db.commit()

    async def mark_retry(self, item_id: int, next_attempt_at: datetime) -> None:
        await self.db.execute(
            """
            UPDATE items
               SET status = ?, attempts = attempts + 1, next_attempt_at = ?
             WHERE id = ?
            """,
            (Status.RETRY.value, _iso(next_attempt_at), item_id),
        )
        await self.db.commit()

    async def record_decision(self, item_id: int, result: DecisionResult) -> None:
        await self.db.execute(
            """
            INSERT INTO decisions
                (item_id, decision, reason, model,
                 input_tokens, output_tokens,
                 cache_creation_input_tokens, cache_read_input_tokens,
                 latency_ms, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                result.decision.value,
                result.reason,
                result.model,
                result.input_tokens,
                result.output_tokens,
                result.cache_creation_input_tokens,
                result.cache_read_input_tokens,
                result.latency_ms,
                _iso(_now()),
            ),
        )
        await self.db.commit()

    # --- read side ---

    async def recent_history(self, window_hours: int) -> list[StoredItem]:
        """Delivered items in the last N hours, oldest first.

        Used to build the dedup prompt context.
        """
        cutoff = _iso(_now() - timedelta(hours=window_hours))
        cur = await self.db.execute(
            """
            SELECT * FROM items
             WHERE status = ? AND delivered_at >= ?
             ORDER BY delivered_at ASC
            """,
            (Status.DELIVERED.value, cutoff),
        )
        rows = await cur.fetchall()
        return [_row_to_item(r) for r in rows]

    async def has_source_id(self, source: Source, source_id: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM items WHERE source = ? AND source_id = ?",
            (source.value, source_id),
        )
        return await cur.fetchone() is not None

    async def get_item(self, item_id: int) -> StoredItem | None:
        cur = await self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,))
        row = await cur.fetchone()
        return _row_to_item(row) if row else None

    # --- maintenance ---

    async def prune(self, window_hours: int, keep_dropped_days: int = 7,
                    keep_decisions_days: int = 30) -> dict[str, int]:
        """Delete old delivered/dropped items and old decisions. Returns counts deleted."""
        items_cutoff = _iso(_now() - timedelta(hours=window_hours * 2))
        dropped_cutoff = _iso(_now() - timedelta(days=keep_dropped_days))
        decisions_cutoff = _iso(_now() - timedelta(days=keep_decisions_days))

        cur = await self.db.execute(
            "DELETE FROM items WHERE status = ? AND delivered_at < ?",
            (Status.DELIVERED.value, items_cutoff),
        )
        delivered_deleted = cur.rowcount

        cur = await self.db.execute(
            "DELETE FROM items WHERE status = ? AND observed_at < ?",
            (Status.DROPPED.value, dropped_cutoff),
        )
        dropped_deleted = cur.rowcount

        cur = await self.db.execute(
            "DELETE FROM decisions WHERE timestamp < ?", (decisions_cutoff,)
        )
        decisions_deleted = cur.rowcount

        await self.db.commit()
        return {
            "items_delivered": delivered_deleted,
            "items_dropped": dropped_deleted,
            "decisions": decisions_deleted,
        }


def items_summary(items: Iterable[StoredItem]) -> list[dict[str, Any]]:
    """Helper for tests: turn StoredItems into plain dicts for assertions."""
    return [
        {
            "id": i.id,
            "source": i.source.value,
            "source_id": i.source_id,
            "status": i.status.value,
            "decision": i.decision.value if i.decision else None,
            "delivered_at": i.delivered_at,
            "drop_reason": i.drop_reason.value if i.drop_reason else None,
            "attempts": i.attempts,
        }
        for i in items
    ]
