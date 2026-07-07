from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class Subscription:
    id: int
    handle: str
    target_origin: str
    target_kind: str
    created_by: str
    created_at: str
    enabled: bool
    last_seen_id: str
    last_seen_link: str
    last_success_at: str
    failure_count: int
    last_error: str


class XFeedStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "x_feed.sqlite3"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handle TEXT NOT NULL,
                    target_origin TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_seen_id TEXT NOT NULL DEFAULT '',
                    last_seen_link TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(handle, target_origin)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
                    handle TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    link TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY(handle, item_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_xfeed_subscriptions_origin ON subscriptions(target_origin)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def upsert_subscription(
        self,
        *,
        handle: str,
        target_origin: str,
        target_kind: str,
        created_by: str,
        last_seen_id: str = "",
        last_seen_link: str = "",
    ) -> tuple[Subscription, bool]:
        now = self._now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM subscriptions WHERE handle = ? AND target_origin = ?",
                (handle, target_origin),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE subscriptions
                    SET enabled = 1,
                        last_error = '',
                        failure_count = 0
                    WHERE handle = ?
                      AND target_origin = ?
                    """,
                    (handle, target_origin),
                )
                row = connection.execute(
                    "SELECT * FROM subscriptions WHERE handle = ? AND target_origin = ?",
                    (handle, target_origin),
                ).fetchone()
                return self._row_to_subscription(row), False

            connection.execute(
                """
                INSERT INTO subscriptions (
                    handle, target_origin, target_kind, created_by, created_at,
                    enabled, last_seen_id, last_seen_link
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (handle, target_origin, target_kind, created_by, now, last_seen_id, last_seen_link),
            )
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE handle = ? AND target_origin = ?",
                (handle, target_origin),
            ).fetchone()
            return self._row_to_subscription(row), True

    def remove_subscription(self, *, handle: str, target_origin: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE handle = ? AND target_origin = ?",
                (handle, target_origin),
            )
            return cursor.rowcount > 0

    def list_enabled(self) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = 1
                ORDER BY handle, target_origin
                """
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def list_for_origin(self, target_origin: str) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE target_origin = ?
                ORDER BY handle
                """,
                (target_origin,),
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def set_origin_enabled(self, target_origin: str, enabled: bool) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE subscriptions SET enabled = ? WHERE target_origin = ?",
                (1 if enabled else 0, target_origin),
            )
            return cursor.rowcount

    def update_success(self, subscription_id: int, *, last_seen_id: str, last_seen_link: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE subscriptions
                SET last_seen_id = ?,
                    last_seen_link = ?,
                    last_success_at = ?,
                    failure_count = 0,
                    last_error = ''
                WHERE id = ?
                """,
                (last_seen_id, last_seen_link, now, subscription_id),
            )

    def update_failure(self, subscription_id: int, error: str) -> int:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE subscriptions
                SET failure_count = failure_count + 1,
                    last_error = ?
                WHERE id = ?
                """,
                (str(error)[:1000], subscription_id),
            )
            row = connection.execute(
                "SELECT failure_count FROM subscriptions WHERE id = ?",
                (subscription_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_seen(self, *, handle: str, item_id: str, link: str, published_at: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO seen_items (handle, item_id, link, published_at, pushed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (handle, item_id, link, published_at, now),
            )

    @staticmethod
    def _row_to_subscription(row) -> Subscription:
        return Subscription(
            id=int(row[0]),
            handle=str(row[1]),
            target_origin=str(row[2]),
            target_kind=str(row[3]),
            created_by=str(row[4]),
            created_at=str(row[5]),
            enabled=bool(row[6]),
            last_seen_id=str(row[7] or ""),
            last_seen_link=str(row[8] or ""),
            last_success_at=str(row[9] or ""),
            failure_count=int(row[10] or 0),
            last_error=str(row[11] or ""),
        )
