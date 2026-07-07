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
    repo: str
    label: str
    branches: list[str]
    watch_push: bool
    watch_release: bool
    watch_tag: bool
    enabled: bool
    preset_id: str
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
class RepoState:
    state_key: str
    baseline_done: bool
    last_event_key: str
    last_checked_at: str
    last_success_at: str
    failure_count: int
    last_error: str


class GitHubWatchStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "github_watch.sqlite3"
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
                    repo TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    branches_json TEXT NOT NULL DEFAULT '["main"]',
                    watch_push INTEGER NOT NULL DEFAULT 1,
                    watch_release INTEGER NOT NULL DEFAULT 1,
                    watch_tag INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    preset_id TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(target_origin, repo)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repo_state (
                    state_key TEXT PRIMARY KEY,
                    baseline_done INTEGER NOT NULL DEFAULT 0,
                    last_event_key TEXT NOT NULL DEFAULT '',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_key TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
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
                SET enabled = ?, updated_at = ?
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

    def upsert_subscription(
        self,
        *,
        target_origin: str,
        target_kind: str,
        repo: str,
        label: str,
        branches: list[str],
        preset_id: str,
        created_by: str,
    ) -> bool:
        now = self._now()
        branches = [branch for branch in branches if branch] or ["main"]
        with self._lock, self._connect() as connection:
            self.ensure_target(target_origin=target_origin, target_kind=target_kind)
            existing = connection.execute(
                "SELECT 1 FROM subscriptions WHERE target_origin = ? AND repo = ?",
                (target_origin, repo),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO subscriptions (
                    target_origin, target_kind, repo, label, branches_json,
                    watch_push, watch_release, watch_tag, enabled, preset_id,
                    created_by, created_at
                )
                VALUES (?, ?, ?, ?, ?, 1, 1, 1, 1, ?, ?, ?)
                ON CONFLICT(target_origin, repo) DO UPDATE SET
                    target_kind = excluded.target_kind,
                    label = excluded.label,
                    branches_json = excluded.branches_json,
                    enabled = 1,
                    preset_id = excluded.preset_id
                """,
                (
                    target_origin,
                    target_kind,
                    repo,
                    label,
                    json.dumps(branches, ensure_ascii=False),
                    preset_id,
                    created_by,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO deliveries (event_key, target_origin, delivered_at)
                SELECT event_key, ?, ?
                FROM events
                WHERE repo = ?
                """,
                (target_origin, now, repo),
            )
        return existing is None

    def remove_subscription(self, *, target_origin: str, repo: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE subscriptions SET enabled = 0 WHERE target_origin = ? AND repo = ?",
                (target_origin, repo),
            )
            return cursor.rowcount > 0

    def set_event_enabled(self, *, target_origin: str, repo: str, event_type: str, enabled: bool) -> bool:
        column = {
            "push": "watch_push",
            "release": "watch_release",
            "tag": "watch_tag",
        }.get(event_type)
        if not column:
            return False
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE subscriptions SET {column} = ? WHERE target_origin = ? AND repo = ?",
                (1 if enabled else 0, target_origin, repo),
            )
            return cursor.rowcount > 0

    def list_subscriptions(self, target_origin: str) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM subscriptions
                WHERE target_origin = ?
                ORDER BY repo ASC
                """,
                (target_origin,),
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def list_enabled_subscriptions(self) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT s.*
                FROM subscriptions s
                JOIN targets t ON t.target_origin = s.target_origin
                WHERE s.enabled = 1
                  AND t.enabled = 1
                ORDER BY s.repo ASC, s.target_origin ASC
                """
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def get_state(self, state_key: str) -> RepoState:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM repo_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row:
            return self._row_to_state(row)
        return RepoState(
            state_key=state_key,
            baseline_done=False,
            last_event_key="",
            last_checked_at="",
            last_success_at="",
            failure_count=0,
            last_error="",
        )

    def record_state_success(self, *, state_key: str, event_key: str, baseline_done: bool = True) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO repo_state (
                    state_key, baseline_done, last_event_key, last_checked_at,
                    last_success_at, failure_count, last_error
                )
                VALUES (?, ?, ?, ?, ?, 0, '')
                ON CONFLICT(state_key) DO UPDATE SET
                    baseline_done = excluded.baseline_done,
                    last_event_key = excluded.last_event_key,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = excluded.last_success_at,
                    failure_count = 0,
                    last_error = ''
                """,
                (state_key, 1 if baseline_done else 0, event_key, now, now),
            )

    def record_state_failure(self, *, state_key: str, error: str) -> int:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO repo_state (
                    state_key, baseline_done, last_event_key, last_checked_at,
                    last_success_at, failure_count, last_error
                )
                VALUES (?, 0, '', ?, '', 1, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    last_checked_at = excluded.last_checked_at,
                    failure_count = repo_state.failure_count + 1,
                    last_error = excluded.last_error
                """,
                (state_key, now, str(error)[:1000]),
            )
            row = connection.execute(
                "SELECT failure_count FROM repo_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        return int(row["failure_count"]) if row else 1

    def upsert_event(
        self,
        *,
        event_key: str,
        repo: str,
        event_type: str,
        title: str,
        url: str,
        payload: dict,
    ) -> bool:
        now = self._now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_key, repo, event_type, title, url, payload_json, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_key, repo, event_type, title, url, json.dumps(payload, ensure_ascii=False), now),
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

    def count_enabled_targets(self) -> int:
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
        try:
            branches = json.loads(str(row["branches_json"] or "[]"))
        except json.JSONDecodeError:
            branches = []
        if not isinstance(branches, list):
            branches = []
        return Subscription(
            target_origin=str(row["target_origin"]),
            target_kind=str(row["target_kind"]),
            repo=str(row["repo"]),
            label=str(row["label"] or ""),
            branches=[str(item) for item in branches if str(item).strip()],
            watch_push=bool(row["watch_push"]),
            watch_release=bool(row["watch_release"]),
            watch_tag=bool(row["watch_tag"]),
            enabled=bool(row["enabled"]),
            preset_id=str(row["preset_id"] or ""),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _row_to_state(row) -> RepoState:
        return RepoState(
            state_key=str(row["state_key"]),
            baseline_done=bool(row["baseline_done"]),
            last_event_key=str(row["last_event_key"] or ""),
            last_checked_at=str(row["last_checked_at"] or ""),
            last_success_at=str(row["last_success_at"] or ""),
            failure_count=int(row["failure_count"] or 0),
            last_error=str(row["last_error"] or ""),
        )
