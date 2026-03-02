from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BindingState:
    user_id: str
    game_uid: str | None
    server: str | None
    screenshot_path: str | None
    status: str
    current_step: int
    created_at: str
    updated_at: str


class GameCSStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                  user_id TEXT PRIMARY KEY,
                  game_uid TEXT,
                  server TEXT,
                  screenshot_path TEXT,
                  status TEXT NOT NULL DEFAULT 'pending',
                  current_step INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL,
                  role TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )

    def get_or_create_binding(self, user_id: str) -> BindingState:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM bindings WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO bindings (user_id, game_uid, server, screenshot_path, status, current_step, created_at, updated_at)
                    VALUES (?, NULL, NULL, NULL, 'pending', 0, ?, ?)
                    """,
                    (user_id, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM bindings WHERE user_id = ?", (user_id,)).fetchone()
            return BindingState(**dict(row))

    def update_binding(
        self,
        user_id: str,
        *,
        game_uid: str | None = None,
        server: str | None = None,
        screenshot_path: str | None = None,
        status: str | None = None,
        current_step: int | None = None,
    ) -> BindingState:
        current = self.get_or_create_binding(user_id)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE bindings
                SET game_uid = ?,
                    server = ?,
                    screenshot_path = ?,
                    status = ?,
                    current_step = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    game_uid if game_uid is not None else current.game_uid,
                    server if server is not None else current.server,
                    screenshot_path if screenshot_path is not None else current.screenshot_path,
                    status if status is not None else current.status,
                    current_step if current_step is not None else current.current_step,
                    now,
                    user_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM bindings WHERE user_id = ?", (user_id,)).fetchone()
            return BindingState(**dict(row))

    def append_message(self, user_id: str, role: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, content, _now_iso()),
            )
            conn.commit()
