"""User feedback (thumbs up/down + optional comment), linked to a request id. SQLite file."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from lexai.core.logging import mask_pii

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT
);
CREATE INDEX IF NOT EXISTS feedback_request ON feedback (request_id);
"""


class FeedbackStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def _add(self, request_id: str, rating: str, comment: str | None) -> int:
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO feedback (created_at, request_id, rating, comment) VALUES (?, ?, ?, ?)",
                # Comments are free text from users: mask personal data before storing.
                (datetime.now(UTC).isoformat(timespec="seconds"), request_id, rating,
                 mask_pii(comment) if comment else None),
            )  # fmt: skip
            return int(cur.lastrowid or 0)

    async def add(self, request_id: str, rating: str, comment: str | None) -> int:
        return await asyncio.to_thread(self._add, request_id, rating, comment)

    def _rows(self) -> list[tuple[int, str, str, str, str | None]]:
        with self._connect() as db:
            return list(
                db.execute("SELECT id, created_at, request_id, rating, comment FROM feedback ORDER BY id")
            )

    async def rows(self) -> list[tuple[int, str, str, str, str | None]]:
        return await asyncio.to_thread(self._rows)
