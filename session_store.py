"""SQLite storage for Telegram chat to Codex session mappings."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self, *, initialized: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        if initialized:
            self._init_schema(conn)
            conn.commit()
        return conn

    def _init_db(self) -> None:
        with self._connect(initialized=True):
            pass

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_sessions (
                chat_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                thread_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_history (
                chat_id INTEGER NOT NULL,
                thread_id TEXT NOT NULL,
                user_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, thread_id)
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO session_history (
                chat_id, thread_id, user_id, created_at, updated_at
            )
            SELECT chat_id, thread_id, user_id, created_at, updated_at
            FROM chat_sessions
            """
        )

    def get_thread_id(self, chat_id: int) -> str | None:
        with self._connect(initialized=True) as conn:
            row = conn.execute(
                "SELECT thread_id FROM chat_sessions WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def set_thread_id(self, chat_id: int, user_id: int | None, thread_id: str) -> None:
        now = int(time.time())
        with self._connect(initialized=True) as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, user_id, thread_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    thread_id = excluded.thread_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, user_id, thread_id, now, now),
            )
            conn.execute(
                """
                INSERT INTO session_history (chat_id, thread_id, user_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, thread_id, user_id, now, now),
            )

    def clear(self, chat_id: int) -> None:
        with self._connect(initialized=True) as conn:
            conn.execute("DELETE FROM chat_sessions WHERE chat_id = ?", (chat_id,))

    def list_history(self, chat_id: int, limit: int = 10) -> list[tuple[str, int, int]]:
        with self._connect(initialized=True) as conn:
            rows = conn.execute(
                """
                SELECT thread_id, created_at, updated_at
                FROM session_history
                WHERE chat_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [(str(row[0]), int(row[1]), int(row[2])) for row in rows]
