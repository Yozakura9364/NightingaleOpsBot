from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import sqlite3
import threading

from cryptography.fernet import Fernet


@dataclass
class BoundAccount:
    user_id: str
    private_origin: str
    updated_at: str
    last_run_at: str | None
    last_auto_date: str | None
    last_ok: bool | None
    last_message: str | None


class CredentialStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "risingstone.sqlite3"
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
                    user_id TEXT PRIMARY KEY,
                    private_origin TEXT NOT NULL,
                    cookie_enc TEXT NOT NULL,
                    user_agent_enc TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_run_at TEXT,
                    last_auto_date TEXT,
                    last_ok INTEGER,
                    last_message TEXT
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")

    def bind(self, user_id: str, private_origin: str, cookie: str, user_agent: str) -> None:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, private_origin, cookie_enc, user_agent_enc,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    private_origin = excluded.private_origin,
                    cookie_enc = excluded.cookie_enc,
                    user_agent_enc = excluded.user_agent_enc,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    private_origin,
                    self._encrypt(cookie),
                    self._encrypt(user_agent),
                    now,
                    now,
                ),
            )

    def unbind(self, user_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
            return cursor.rowcount > 0

    def get_credentials(self, user_id: str) -> tuple[str, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT cookie_enc, user_agent_enc FROM accounts WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return self._decrypt(row[0]), self._decrypt(row[1])

    def get_account(self, user_id: str) -> BoundAccount | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, private_origin, updated_at, last_run_at,
                       last_auto_date, last_ok, last_message
                FROM accounts
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_account(row) if row else None

    def list_accounts(self) -> list[BoundAccount]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, private_origin, updated_at, last_run_at,
                       last_auto_date, last_ok, last_message
                FROM accounts
                ORDER BY user_id
                """
            ).fetchall()
        return [self._row_to_account(row) for row in rows]

    def update_result(
        self,
        user_id: str,
        *,
        ok: bool,
        message: str,
        auto_date: str | None = None,
    ) -> None:
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
                """,
                (now, auto_date, 1 if ok else 0, message[:2000], user_id),
            )

    @staticmethod
    def _row_to_account(row) -> BoundAccount:
        return BoundAccount(
            user_id=str(row[0]),
            private_origin=str(row[1]),
            updated_at=str(row[2]),
            last_run_at=row[3],
            last_auto_date=row[4],
            last_ok=None if row[5] is None else bool(row[5]),
            last_message=row[6],
        )
