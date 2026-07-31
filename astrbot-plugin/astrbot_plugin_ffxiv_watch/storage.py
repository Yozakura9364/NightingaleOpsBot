from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import json
import sqlite3
import threading


@dataclass(frozen=True)
class Subscription:
    target_origin: str
    target_kind: str
    category: str
    enabled: bool
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Target:
    target_origin: str
    target_kind: str
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SourceState:
    source_id: str
    baseline_done: bool
    baseline_version: int
    last_keys: list[str]
    last_checked_at: str
    last_success_at: str
    failure_count: int
    last_error: str


class FFXIVWatchStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "ffxiv_watch.sqlite3"
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS targets (
                    target_origin TEXT PRIMARY KEY,
                    target_kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    target_origin TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(target_origin, category)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS target_sources (
                    target_origin TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(target_origin, source_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_state (
                    source_id TEXT PRIMARY KEY,
                    baseline_done INTEGER NOT NULL DEFAULT 0,
                    baseline_version INTEGER NOT NULL DEFAULT 1,
                    last_keys_json TEXT NOT NULL DEFAULT '[]',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            source_state_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(source_state)").fetchall()
            }
            if "baseline_version" not in source_state_columns:
                connection.execute(
                    "ALTER TABLE source_state ADD COLUMN baseline_version INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    event_key TEXT NOT NULL,
                    target_origin TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY(event_key, target_origin)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ffxiv_watch_subscriptions_category ON subscriptions(category, enabled)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_ffxiv_watch_events_source ON events(source_id, first_seen_at)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def ensure_target(self, *, target_origin: str, target_kind: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO targets (target_origin, target_kind, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(target_origin) DO UPDATE SET
                    target_kind = excluded.target_kind,
                    updated_at = excluded.updated_at
                """,
                (target_origin, target_kind, now, now),
            )

    def set_target_enabled(self, *, target_origin: str, target_kind: str, enabled: bool) -> None:
        self.ensure_target(target_origin=target_origin, target_kind=target_kind)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE targets
                SET enabled = ?,
                    updated_at = ?
                WHERE target_origin = ?
                """,
                (1 if enabled else 0, self._now(), target_origin),
            )

    def get_target(self, target_origin: str) -> Target | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM targets WHERE target_origin = ?",
                (target_origin,),
            ).fetchone()
        return self._row_to_target(row) if row else None

    def subscribe(
        self,
        *,
        target_origin: str,
        target_kind: str,
        category: str,
        created_by: str,
        source_ids: list[str],
    ) -> bool:
        now = self._now()
        with self._lock, self._connect() as connection:
            self.ensure_target(target_origin=target_origin, target_kind=target_kind)
            existing = connection.execute(
                "SELECT 1 FROM subscriptions WHERE target_origin = ? AND category = ?",
                (target_origin, category),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO subscriptions (
                    target_origin, target_kind, category, enabled, created_by, created_at
                )
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(target_origin, category) DO UPDATE SET
                    target_kind = excluded.target_kind,
                    enabled = 1
                """,
                (target_origin, target_kind, category, created_by, now),
            )
            for source_id in source_ids:
                connection.execute(
                    """
                    INSERT INTO target_sources (target_origin, source_id, enabled, updated_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(target_origin, source_id) DO NOTHING
                    """,
                    (target_origin, source_id, now),
                )
            # Avoid pushing events discovered before this chat subscribed.
            connection.execute(
                """
                INSERT OR IGNORE INTO deliveries (event_key, target_origin, delivered_at)
                SELECT event_key, ?, ?
                FROM events
                WHERE kind = ?
                """,
                (target_origin, now, category),
            )
        return existing is None

    def unsubscribe(self, *, target_origin: str, category: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE subscriptions SET enabled = 0 WHERE target_origin = ? AND category = ?",
                (target_origin, category),
            )
            return cursor.rowcount > 0

    def list_subscriptions(self, target_origin: str) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE target_origin = ?
                ORDER BY category ASC
                """,
                (target_origin,),
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def list_all_enabled_subscriptions(self) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*
                FROM subscriptions s
                JOIN targets t ON t.target_origin = s.target_origin
                WHERE s.enabled = 1
                  AND t.enabled = 1
                ORDER BY s.category, s.target_origin
                """
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def set_source_enabled(self, *, target_origin: str, target_kind: str, source_id: str, enabled: bool) -> None:
        self.ensure_target(target_origin=target_origin, target_kind=target_kind)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO target_sources (target_origin, source_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_origin, source_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (target_origin, source_id, 1 if enabled else 0, self._now()),
            )

    def source_enabled_for_target(self, *, target_origin: str, source_id: str, default: bool = True) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT enabled
                FROM target_sources
                WHERE target_origin = ?
                  AND source_id = ?
                """,
                (target_origin, source_id),
            ).fetchone()
        return bool(row["enabled"]) if row else default

    def list_target_source_settings(self, target_origin: str) -> dict[str, bool]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT source_id, enabled FROM target_sources WHERE target_origin = ?",
                (target_origin,),
            ).fetchall()
        return {str(row["source_id"]): bool(row["enabled"]) for row in rows}

    def targets_for_source(self, *, source_id: str, category: str) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.target_origin
                FROM subscriptions s
                JOIN targets t ON t.target_origin = s.target_origin
                LEFT JOIN target_sources ts
                  ON ts.target_origin = s.target_origin
                 AND ts.source_id = ?
                WHERE s.category = ?
                  AND s.enabled = 1
                  AND t.enabled = 1
                  AND COALESCE(ts.enabled, 1) = 1
                ORDER BY s.target_origin
                """,
                (source_id, category),
            ).fetchall()
        return [str(row["target_origin"]) for row in rows]

    def get_source_state(self, source_id: str) -> SourceState:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_state WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if row:
            return self._row_to_source_state(row)
        return SourceState(
            source_id=source_id,
            baseline_done=False,
            baseline_version=1,
            last_keys=[],
            last_checked_at="",
            last_success_at="",
            failure_count=0,
            last_error="",
        )

    def record_source_success(
        self,
        *,
        source_id: str,
        keys: list[str],
        baseline_done: bool = True,
        baseline_version: int = 1,
    ) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (
                    source_id, baseline_done, baseline_version, last_keys_json, last_checked_at,
                    last_success_at, failure_count, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, '')
                ON CONFLICT(source_id) DO UPDATE SET
                    baseline_done = excluded.baseline_done,
                    baseline_version = excluded.baseline_version,
                    last_keys_json = excluded.last_keys_json,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    failure_count = 0,
                    last_error = ''
                """,
                (
                    source_id,
                    1 if baseline_done else 0,
                    max(1, int(baseline_version or 1)),
                    json.dumps(keys[:80]),
                    now,
                    now,
                ),
            )

    def record_source_failure(self, *, source_id: str, error: str) -> int:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_state (
                    source_id, baseline_done, last_keys_json, last_checked_at,
                    last_success_at, failure_count, last_error
                )
                VALUES (?, 0, '[]', ?, '', 1, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    failure_count = source_state.failure_count + 1,
                    last_error = excluded.last_error
                """,
                (source_id, now, str(error)[:1000]),
            )
            row = connection.execute(
                "SELECT failure_count FROM source_state WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return int(row["failure_count"]) if row else 1

    def upsert_event(
        self,
        *,
        event_key: str,
        source_id: str,
        kind: str,
        title: str,
        url: str,
        published_at: str,
        payload: dict,
    ) -> bool:
        now = self._now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_key, source_id, kind, title, url, published_at, payload_json, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    source_id,
                    kind,
                    title,
                    url,
                    published_at,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            return cursor.rowcount > 0

    def mark_delivered(self, *, event_key: str, target_origin: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO deliveries (event_key, target_origin, delivered_at)
                VALUES (?, ?, ?)
                """,
                (event_key, target_origin, self._now()),
            )
            return cursor.rowcount > 0

    def count_deliverable_targets(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT s.target_origin)
                FROM subscriptions s
                JOIN targets t ON t.target_origin = s.target_origin
                WHERE s.enabled = 1
                  AND t.enabled = 1
                """
            ).fetchone()
        return int(row[0] if row else 0)

    @staticmethod
    def _row_to_target(row) -> Target:
        return Target(
            target_origin=str(row["target_origin"]),
            target_kind=str(row["target_kind"]),
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_subscription(row) -> Subscription:
        return Subscription(
            target_origin=str(row["target_origin"]),
            target_kind=str(row["target_kind"]),
            category=str(row["category"]),
            enabled=bool(row["enabled"]),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_source_state(row) -> SourceState:
        try:
            last_keys = json.loads(str(row["last_keys_json"] or "[]"))
        except json.JSONDecodeError:
            last_keys = []
        if not isinstance(last_keys, list):
            last_keys = []
        return SourceState(
            source_id=str(row["source_id"]),
            baseline_done=bool(row["baseline_done"]),
            baseline_version=int(row["baseline_version"] or 1),
            last_keys=[str(item) for item in last_keys],
            last_checked_at=str(row["last_checked_at"] or ""),
            last_success_at=str(row["last_success_at"] or ""),
            failure_count=int(row["failure_count"] or 0),
            last_error=str(row["last_error"] or ""),
        )
