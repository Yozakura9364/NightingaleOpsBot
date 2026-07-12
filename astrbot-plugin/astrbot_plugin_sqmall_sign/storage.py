from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import sqlite3
import threading

from cryptography.fernet import Fernet


DEFAULT_SLOT = "默认"


@dataclass
class BoundSqmallAccount:
    user_id: str
    slot: str
    private_origin: str
    show_username: str
    credential_kind: str
    member_id: str
    updated_at: str
    last_run_at: str | None
    last_auto_date: str | None
    last_ok: bool | None
    last_message: str | None


@dataclass
class SqmallCredential:
    kind: str
    secret: str
    show_username: str
    member_id: str


class SqmallCredentialStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "sqmall.sqlite3"
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
                CREATE TABLE IF NOT EXISTS accounts (
                    user_id TEXT NOT NULL,
                    slot TEXT NOT NULL DEFAULT '默认',
                    private_origin TEXT NOT NULL,
                    daoyu_key_enc TEXT NOT NULL,
                    show_username TEXT NOT NULL,
                    credential_kind TEXT NOT NULL DEFAULT 'daoyu',
                    member_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_auto_date TEXT,
                    last_ok INTEGER,
                    last_message TEXT,
                    PRIMARY KEY (user_id, slot)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sqmall_accounts_user_id ON accounts(user_id)"
            )
            self._ensure_column(connection, "credential_kind", "TEXT NOT NULL DEFAULT 'daoyu'")
            self._ensure_column(connection, "member_id", "TEXT NOT NULL DEFAULT ''")

    def _ensure_column(self, connection: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(accounts)").fetchall()
        }
        if name not in columns:
            connection.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")

    @staticmethod
    def normalize_slot(slot: str | None) -> str:
        value = str(slot or "").strip()
        return value or DEFAULT_SLOT

    @staticmethod
    def validate_slot(slot: str | None) -> str:
        value = SqmallCredentialStore.normalize_slot(slot)
        if value == "全部":
            raise ValueError("槽位名不能叫“全部”，这是保留命令。")
        if any(char in value for char in "\r\n\t"):
            raise ValueError("槽位名不能包含换行或制表符。")
        if len(value) > 24:
            raise ValueError("槽位名过长，请控制在 24 个字符以内。")
        return value

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def bind(
        self,
        user_id: str,
        private_origin: str,
        daoyu_key: str,
        show_username: str,
        member_id: str = "",
        slot: str | None = DEFAULT_SLOT,
    ) -> None:
        normalized_slot = self.validate_slot(slot)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, slot, private_origin, daoyu_key_enc, show_username,
                    credential_kind, member_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'daoyu', ?, ?, ?)
                ON CONFLICT(user_id, slot) DO UPDATE SET
                    private_origin = excluded.private_origin,
                    daoyu_key_enc = excluded.daoyu_key_enc,
                    show_username = excluded.show_username,
                    credential_kind = excluded.credential_kind,
                    member_id = excluded.member_id,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    normalized_slot,
                    private_origin,
                    self._encrypt(daoyu_key),
                    show_username,
                    member_id,
                    now,
                    now,
                ),
            )

    def bind_session(
        self,
        user_id: str,
        private_origin: str,
        session_id: str,
        member_id: str,
        display_name: str,
        slot: str | None = DEFAULT_SLOT,
    ) -> None:
        normalized_slot = self.validate_slot(slot)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, slot, private_origin, daoyu_key_enc, show_username,
                    credential_kind, member_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'sqmall-session', ?, ?, ?)
                ON CONFLICT(user_id, slot) DO UPDATE SET
                    private_origin = excluded.private_origin,
                    daoyu_key_enc = excluded.daoyu_key_enc,
                    show_username = excluded.show_username,
                    credential_kind = excluded.credential_kind,
                    member_id = excluded.member_id,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    normalized_slot,
                    private_origin,
                    self._encrypt(session_id),
                    display_name,
                    member_id,
                    now,
                    now,
                ),
            )

    def bind_browser_state(
        self,
        user_id: str,
        private_origin: str,
        browser_state: str,
        member_id: str,
        display_name: str,
        slot: str | None = DEFAULT_SLOT,
    ) -> None:
        normalized_slot = self.validate_slot(slot)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, slot, private_origin, daoyu_key_enc, show_username,
                    credential_kind, member_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'sqmall-browser-state', ?, ?, ?)
                ON CONFLICT(user_id, slot) DO UPDATE SET
                    private_origin = excluded.private_origin,
                    daoyu_key_enc = excluded.daoyu_key_enc,
                    show_username = excluded.show_username,
                    credential_kind = excluded.credential_kind,
                    member_id = excluded.member_id,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    normalized_slot,
                    private_origin,
                    self._encrypt(browser_state),
                    display_name,
                    member_id,
                    now,
                    now,
                ),
            )

    def unbind(self, user_id: str, slot: str | None = DEFAULT_SLOT) -> bool:
        normalized_slot = self.normalize_slot(slot)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM accounts WHERE user_id = ? AND slot = ?",
                (user_id, normalized_slot),
            )
            return cursor.rowcount > 0

    def rename_slot(self, user_id: str, old_slot: str | None, new_slot: str | None) -> str:
        normalized_old_slot = self.normalize_slot(old_slot)
        normalized_new_slot = self.validate_slot(new_slot)
        if normalized_old_slot == normalized_new_slot:
            return "same"

        now = self._now()
        with self._lock, self._connect() as connection:
            old_exists = connection.execute(
                "SELECT 1 FROM accounts WHERE user_id = ? AND slot = ?",
                (user_id, normalized_old_slot),
            ).fetchone()
            if not old_exists:
                return "missing"

            new_exists = connection.execute(
                "SELECT 1 FROM accounts WHERE user_id = ? AND slot = ?",
                (user_id, normalized_new_slot),
            ).fetchone()
            if new_exists:
                return "exists"

            connection.execute(
                """
                UPDATE accounts
                SET slot = ?,
                    updated_at = ?
                WHERE user_id = ?
                  AND slot = ?
                """,
                (normalized_new_slot, now, user_id, normalized_old_slot),
            )
        return "renamed"

    def get_credentials(self, user_id: str, slot: str | None = DEFAULT_SLOT) -> tuple[str, str] | None:
        credential = self.get_credential(user_id, slot)
        if not credential:
            return None
        return credential.secret, credential.show_username

    def get_credential(self, user_id: str, slot: str | None = DEFAULT_SLOT) -> SqmallCredential | None:
        normalized_slot = self.normalize_slot(slot)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT daoyu_key_enc, show_username, credential_kind, member_id
                FROM accounts
                WHERE user_id = ? AND slot = ?
                """,
                (user_id, normalized_slot),
            ).fetchone()
        if not row:
            return None
        return SqmallCredential(
            kind=str(row[2] or "daoyu"),
            secret=self._decrypt(row[0]),
            show_username=str(row[1] or ""),
            member_id=str(row[3] or ""),
        )

    def get_account(self, user_id: str, slot: str | None = DEFAULT_SLOT) -> BoundSqmallAccount | None:
        normalized_slot = self.normalize_slot(slot)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, slot, private_origin, show_username,
                       credential_kind, member_id, updated_at,
                       last_run_at, last_auto_date, last_ok, last_message
                FROM accounts
                WHERE user_id = ? AND slot = ?
                """,
                (user_id, normalized_slot),
            ).fetchone()
        return self._row_to_account(row) if row else None

    def list_user_accounts(self, user_id: str) -> list[BoundSqmallAccount]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, slot, private_origin, show_username,
                       credential_kind, member_id, updated_at,
                       last_run_at, last_auto_date, last_ok, last_message
                FROM accounts
                WHERE user_id = ?
                ORDER BY slot
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def list_accounts(self) -> list[BoundSqmallAccount]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, slot, private_origin, show_username,
                       credential_kind, member_id, updated_at,
                       last_run_at, last_auto_date, last_ok, last_message
                FROM accounts
                ORDER BY user_id, slot
                """
            ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def update_result(
        self,
        user_id: str,
        *,
        slot: str | None = DEFAULT_SLOT,
        ok: bool,
        message: str,
        auto_date: str | None = None,
    ) -> None:
        normalized_slot = self.normalize_slot(slot)
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE accounts
                SET last_run_at = ?,
                    last_auto_date = COALESCE(?, last_auto_date),
                    last_ok = ?,
                    last_message = ?
                WHERE user_id = ?
                  AND slot = ?
                """,
                (now, auto_date, 1 if ok else 0, message[:2000], user_id, normalized_slot),
            )

    @staticmethod
    def _row_to_account(row) -> BoundSqmallAccount:
        return BoundSqmallAccount(
            user_id=str(row[0]),
            slot=str(row[1]),
            private_origin=str(row[2]),
            show_username=str(row[3]),
            credential_kind=str(row[4] or "daoyu"),
            member_id=str(row[5] or ""),
            updated_at=str(row[6]),
            last_run_at=row[7],
            last_auto_date=row[8],
            last_ok=None if row[9] is None else bool(row[9]),
            last_message=row[10],
        )
