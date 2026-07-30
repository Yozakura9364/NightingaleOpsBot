from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
import threading


@dataclass(frozen=True)
class DeadlineItem:
    id: int
    target_origin: str
    target_kind: str
    category: str
    title: str
    due_at: str
    created_by: str
    created_at: str
    enabled: bool
    source_url: str
    source_id: str


@dataclass(frozen=True)
class ReminderTarget:
    target_origin: str
    target_kind: str
    enabled: bool
    broadcast_enabled: bool
    last_daily_date: str


class DeadlineStore:
    BROADCAST_ORIGIN = "__broadcast__"
    HERMES_ORIGIN = "__hermes__"

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "deadline_reminder.sqlite3"
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
                    broadcast_enabled INTEGER NOT NULL DEFAULT 0,
                    last_daily_date TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deadlines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_origin TEXT NOT NULL,
                    target_kind TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deadlines_target_due ON deadlines(target_origin, due_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deadlines_enabled_due ON deadlines(enabled, due_at)"
            )
            self._ensure_column(connection, "targets", "broadcast_enabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "deadlines", "source_url", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "deadlines", "source_id", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_deadlines_source_id ON deadlines(source_id) WHERE source_id != ''"
            )

    @staticmethod
    def _ensure_column(connection, table: str, column: str, definition: str) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

    def sync_deadlines(self, events: list[dict]) -> int:
        """全量替换 Hermes 同步的 deadlines。返回变更计数。"""
        now = self._now()
        changed = 0
        with self._lock, self._connect() as connection:
            incoming_ids = set()
            for event in events:
                event_id = str(event.get("id", "")).strip()
                if not event_id:
                    continue
                incoming_ids.add(event_id)
                title = str(event.get("title", "")).strip()
                url = str(event.get("url", "")).strip()
                starts = str(event.get("startsAt", "")).strip()
                ends = str(event.get("endsAt", "")).strip()
                region = str(event.get("region", "common")).strip()
                category = {"cn": "国服活动", "common": "共同活动"}.get(region, "国际服活动")

                # Use endsAt as due_at; if missing, use startsAt + 1 hour
                due_at = ends if ends else starts

                row = connection.execute(
                    "SELECT id, title, due_at, category, source_url, enabled FROM deadlines WHERE source_id = ?",
                    (event_id,),
                ).fetchone()

                if row:
                    existing = dict(row)
                    needs_update = (
                        existing["title"] != title
                        or existing["due_at"] != due_at
                        or existing["category"] != category
                        or existing["source_url"] != url
                        or not existing["enabled"]
                    )
                    if needs_update:
                        connection.execute(
                            "UPDATE deadlines SET title=?, due_at=?, category=?, source_url=?, enabled=1 WHERE source_id=?",
                            (title, due_at, category, url, event_id),
                        )
                        changed += 1
                else:
                    connection.execute(
                        """INSERT INTO deadlines (target_origin, target_kind, category, title, due_at, created_by, created_at, enabled, source_url, source_id)
                           VALUES (?, 'broadcast', ?, ?, ?, 'hermes', ?, 1, ?, ?)""",
                        (self.HERMES_ORIGIN, category, title, due_at, now, url, event_id),
                    )
                    changed += 1

            # Delete events no longer in Hermes
            if incoming_ids:
                placeholders = ",".join("?" * len(incoming_ids))
                connection.execute(
                    f"DELETE FROM deadlines WHERE source_id != '' AND source_id NOT IN ({placeholders})",
                    list(incoming_ids),
                )
            else:
                connection.execute("DELETE FROM deadlines WHERE source_id != ''")

        return changed

    def add_deadline(
        self,
        *,
        target_origin: str,
        target_kind: str,
        category: str,
        title: str,
        due_at: str,
        created_by: str,
        source_url: str = "",
    ) -> DeadlineItem:
        now = self._now()
        with self._lock, self._connect() as connection:
            if target_origin != self.BROADCAST_ORIGIN and target_kind != "broadcast":
                self.ensure_target(target_origin=target_origin, target_kind=target_kind)
            cursor = connection.execute(
                """
                INSERT INTO deadlines (
                    target_origin, target_kind, category, title, due_at, created_by, created_at, enabled, source_url
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (target_origin, target_kind, category, title, due_at, created_by, now, source_url),
            )
            row = connection.execute(
                "SELECT * FROM deadlines WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_deadline(row)

    def list_deadlines(self, *, target_origin: str, include_disabled: bool = False) -> list[DeadlineItem]:
        where = "target_origin IN (?, ?)"
        params: list[object] = [target_origin, self.HERMES_ORIGIN]
        if not include_disabled:
            where += " AND enabled = 1"
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM deadlines
                WHERE {where}
                ORDER BY due_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_deadline(row) for row in rows]

    def list_active_deadlines(self, *, target_origin: str, now_iso: str) -> list[DeadlineItem]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deadlines
                WHERE (target_origin = ? OR target_origin = ?)
                  AND enabled = 1
                  AND due_at >= ?
                ORDER BY due_at ASC, id ASC
                """,
                (target_origin, self.HERMES_ORIGIN, now_iso),
            ).fetchall()
        return [self._row_to_deadline(row) for row in rows]

    def delete_deadline(self, *, target_origin: str, deadline_id: int) -> bool:
        with self._lock, self._connect() as connection:
            # Only allow deleting manual entries (source_id empty)
            cursor = connection.execute(
                "DELETE FROM deadlines WHERE target_origin = ? AND id = ? AND (source_id = '' OR source_id IS NULL)",
                (target_origin, deadline_id),
            )
            return cursor.rowcount > 0

    def set_deadline_enabled(self, *, target_origin: str, deadline_id: int, enabled: bool) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE deadlines SET enabled = ? WHERE target_origin = ? AND id = ?",
                (1 if enabled else 0, target_origin, deadline_id),
            )
            return cursor.rowcount > 0

    def set_target_enabled(self, *, target_origin: str, target_kind: str, enabled: bool) -> None:
        self.ensure_target(target_origin=target_origin, target_kind=target_kind)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE targets
                SET enabled = ?,
                    updated_at = ?
                WHERE target_origin = ?
                """,
                (1 if enabled else 0, now, target_origin),
            )

    def get_target(self, target_origin: str) -> ReminderTarget | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM targets WHERE target_origin = ?",
                (target_origin,),
            ).fetchone()
        return self._row_to_target(row) if row else None

    def list_targets_for_daily(self, *, now_iso: str) -> list[ReminderTarget]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT t.*
                FROM targets t
                LEFT JOIN deadlines d
                  ON d.target_origin = t.target_origin
                 AND d.enabled = 1
                 AND d.due_at >= ?
                LEFT JOIN deadlines bd
                  ON bd.target_origin = ?
                 AND bd.enabled = 1
                 AND bd.due_at >= ?
                LEFT JOIN deadlines hd
                  ON hd.target_origin = ?
                 AND hd.enabled = 1
                 AND hd.due_at >= ?
                WHERE t.enabled = 1
                  AND t.target_origin <> ?
                  AND t.target_kind <> 'broadcast'
                  AND (
                    d.id IS NOT NULL
                    OR (
                      t.target_kind = 'group'
                      AND t.broadcast_enabled = 1
                      AND bd.id IS NOT NULL
                    )
                    OR (
                      t.target_kind = 'group'
                      AND t.broadcast_enabled = 1
                      AND hd.id IS NOT NULL
                    )
                  )
                ORDER BY t.target_origin ASC
                """,
                (now_iso, self.BROADCAST_ORIGIN, now_iso, self.HERMES_ORIGIN, now_iso, self.BROADCAST_ORIGIN),
            ).fetchall()
        return [self._row_to_target(row) for row in rows]

    def set_broadcast_enabled(self, *, target_origin: str, target_kind: str, enabled: bool) -> None:
        self.ensure_target(target_origin=target_origin, target_kind=target_kind)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE targets
                SET broadcast_enabled = ?,
                    updated_at = ?
                WHERE target_origin = ?
                """,
                (1 if enabled else 0, now, target_origin),
            )

    def list_broadcast_targets(self) -> list[ReminderTarget]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM targets
                WHERE target_kind = 'group'
                  AND enabled = 1
                  AND broadcast_enabled = 1
                ORDER BY target_origin ASC
                """
            ).fetchall()
        return [self._row_to_target(row) for row in rows]

    def count_broadcast_targets(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM targets
                WHERE target_kind = 'group'
                  AND enabled = 1
                  AND broadcast_enabled = 1
                """
            ).fetchone()
        return int(row[0] if row else 0)

    def update_last_daily_date(self, *, target_origin: str, date_value: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE targets
                SET last_daily_date = ?,
                    updated_at = ?
                WHERE target_origin = ?
                """,
                (date_value, now, target_origin),
            )

    @staticmethod
    def _row_to_deadline(row) -> DeadlineItem:
        return DeadlineItem(
            id=int(row["id"]),
            target_origin=str(row["target_origin"]),
            target_kind=str(row["target_kind"]),
            category=str(row["category"]),
            title=str(row["title"]),
            due_at=str(row["due_at"]),
            created_by=str(row["created_by"]),
            created_at=str(row["created_at"]),
            enabled=bool(row["enabled"]),
            source_url=str(row["source_url"] or ""),
            source_id=str(row["source_id"] or ""),
        )

    @staticmethod
    def _row_to_target(row) -> ReminderTarget:
        return ReminderTarget(
            target_origin=str(row["target_origin"]),
            target_kind=str(row["target_kind"]),
            enabled=bool(row["enabled"]),
            broadcast_enabled=bool(row["broadcast_enabled"]),
            last_daily_date=str(row["last_daily_date"] or ""),
        )
