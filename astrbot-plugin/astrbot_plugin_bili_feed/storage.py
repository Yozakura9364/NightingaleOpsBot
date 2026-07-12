from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import sqlite3
import threading

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Subscription:
    id: int
    uid: str
    keyword_filter: str
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


@dataclass(frozen=True)
class BiliCredential:
    owner_user_id: str
    private_origin: str
    display_name: str
    cookie: str
    user_agent: str
    updated_at: str


class BiliFeedStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "bili_feed.sqlite3"
        self.key_path = self.data_dir / "secret.key"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_or_create_key())
        self._init_db()

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.write_bytes(key)
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass
        return key

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT NOT NULL,
                    keyword_filter TEXT NOT NULL DEFAULT '',
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
                    UNIQUE(uid, target_origin)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_items (
                    uid TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    link TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY(uid, item_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_bili_subscriptions_origin ON subscriptions(target_origin)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    credential_key TEXT PRIMARY KEY,
                    owner_user_id TEXT NOT NULL,
                    private_origin TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    cookie_enc TEXT NOT NULL,
                    user_agent_enc TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(str(value or "").encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def upsert_subscription(
        self,
        *,
        uid: str,
        keyword_filter: str,
        target_origin: str,
        target_kind: str,
        created_by: str,
        last_seen_id: str = "",
        last_seen_link: str = "",
    ) -> tuple[Subscription, bool]:
        now = self._now()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM subscriptions WHERE uid = ? AND target_origin = ?",
                (uid, target_origin),
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE subscriptions
                    SET keyword_filter = ?,
                        enabled = 1,
                        last_error = '',
                        failure_count = 0
                    WHERE uid = ?
                      AND target_origin = ?
                    """,
                    (keyword_filter, uid, target_origin),
                )
                row = connection.execute(
                    "SELECT * FROM subscriptions WHERE uid = ? AND target_origin = ?",
                    (uid, target_origin),
                ).fetchone()
                return self._row_to_subscription(row), False

            connection.execute(
                """
                INSERT INTO subscriptions (
                    uid, keyword_filter, target_origin, target_kind, created_by, created_at,
                    enabled, last_seen_id, last_seen_link
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (uid, keyword_filter, target_origin, target_kind, created_by, now, last_seen_id, last_seen_link),
            )
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE uid = ? AND target_origin = ?",
                (uid, target_origin),
            ).fetchone()
            return self._row_to_subscription(row), True

    def remove_subscription(self, *, uid: str, target_origin: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE uid = ? AND target_origin = ?",
                (uid, target_origin),
            )
            return cursor.rowcount > 0

    def list_enabled(self) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE enabled = 1
                ORDER BY uid, target_origin
                """
            ).fetchall()
        return [self._row_to_subscription(row) for row in rows]

    def list_for_origin(self, target_origin: str) -> list[Subscription]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM subscriptions
                WHERE target_origin = ?
                ORDER BY uid
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

    def record_seen(self, *, uid: str, item_id: str, link: str, published_at: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO seen_items (uid, item_id, link, published_at, pushed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (uid, item_id, link, published_at, now),
            )

    def bind_credential(
        self,
        *,
        owner_user_id: str,
        private_origin: str,
        display_name: str,
        cookie: str,
        user_agent: str,
    ) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO credentials (
                    credential_key,
                    owner_user_id,
                    private_origin,
                    display_name,
                    cookie_enc,
                    user_agent_enc,
                    updated_at
                )
                VALUES ('default', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(credential_key) DO UPDATE SET
                    owner_user_id = excluded.owner_user_id,
                    private_origin = excluded.private_origin,
                    display_name = excluded.display_name,
                    cookie_enc = excluded.cookie_enc,
                    user_agent_enc = excluded.user_agent_enc,
                    updated_at = excluded.updated_at
                """,
                (
                    owner_user_id,
                    private_origin,
                    display_name,
                    self._encrypt(cookie),
                    self._encrypt(user_agent),
                    now,
                ),
            )

    def get_credential(self) -> BiliCredential | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT owner_user_id, private_origin, display_name, cookie_enc, user_agent_enc, updated_at
                FROM credentials
                WHERE credential_key = 'default'
                """
            ).fetchone()
        if not row:
            return None
        return BiliCredential(
            owner_user_id=str(row[0]),
            private_origin=str(row[1]),
            display_name=str(row[2]),
            cookie=self._decrypt(row[3]),
            user_agent=self._decrypt(row[4]),
            updated_at=str(row[5]),
        )

    def clear_credential(self) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM credentials WHERE credential_key = 'default'"
            )
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_subscription(row) -> Subscription:
        return Subscription(
            id=int(row[0]),
            uid=str(row[1]),
            keyword_filter=str(row[2] or ""),
            target_origin=str(row[3]),
            target_kind=str(row[4]),
            created_by=str(row[5]),
            created_at=str(row[6]),
            enabled=bool(row[7]),
            last_seen_id=str(row[8] or ""),
            last_seen_link=str(row[9] or ""),
            last_success_at=str(row[10] or ""),
            failure_count=int(row[11] or 0),
            last_error=str(row[12] or ""),
        )
