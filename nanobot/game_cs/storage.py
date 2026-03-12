from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover - exercised only when PyMySQL is missing
    pymysql = None  # type: ignore[assignment]
    DictCursor = None  # type: ignore[assignment]

from .config import GameCSConfig
from ..utils.time import now_datetime, now_iso, parse_datetime


def _now_iso() -> str:
    return now_iso()


def _parse_iso(value: str | None) -> datetime | None:
    return parse_datetime(value)


_UNSET = object()


@dataclass
class SOPSessionState:
    user_id: str
    sop_state: str
    game_name: str | None
    area_name: str | None
    role_name: str | None
    game_role_id: str | None
    channel_chat_id: str | None
    screenshot_path: str | None
    retry_count: int
    codes_sent_at: str | None
    follow_up_30m_sent: bool
    follow_up_1h_sent: bool
    next_day_visited: bool
    is_closed: bool
    closed_at: str | None
    created_at: str
    updated_at: str

    @property
    def is_bound(self) -> bool:
        from .models import SOPState

        return self.sop_state in {
            SOPState.SENDING_CODE,
            SOPState.FOLLOW_UP_PENDING,
            SOPState.FOLLOW_UP_30MIN,
            SOPState.FOLLOW_UP_1HOUR,
            SOPState.SILENT,
            SOPState.NEXT_DAY_VISIT,
            SOPState.REACTIVATION,
            SOPState.COMPLETED,
        }

    @property
    def has_full_info(self) -> bool:
        return bool(self.area_name) and bool(self.role_name)


class _BaseGameCSStore:
    def get_or_create_session(
        self,
        user_id: str,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        raise NotImplementedError

    def update_session(
        self,
        user_id: str,
        *,
        sop_state: str | object = _UNSET,
        game_name: str | None | object = _UNSET,
        area_name: str | None | object = _UNSET,
        role_name: str | None | object = _UNSET,
        game_role_id: str | None | object = _UNSET,
        channel_chat_id: str | None | object = _UNSET,
        screenshot_path: str | None | object = _UNSET,
        retry_count: int | object = _UNSET,
        codes_sent_at: str | None | object = _UNSET,
        follow_up_30m_sent: bool | object = _UNSET,
        follow_up_1h_sent: bool | object = _UNSET,
        next_day_visited: bool | object = _UNSET,
        is_closed: bool | object = _UNSET,
        closed_at: str | None | object = _UNSET,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        raise NotImplementedError


class _SqliteGameCSStore(_BaseGameCSStore):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sop_sessions (
                    user_id TEXT NOT NULL PRIMARY KEY,
                    sop_state TEXT NOT NULL DEFAULT 'greeting',
                    game_name TEXT,
                    area_name TEXT,
                    role_name TEXT,
                    game_role_id TEXT,
                    channel_chat_id TEXT,
                    screenshot_path TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    codes_sent_at TEXT,
                    follow_up_30m_sent INTEGER NOT NULL DEFAULT 0,
                    follow_up_1h_sent INTEGER NOT NULL DEFAULT 0,
                    next_day_visited INTEGER NOT NULL DEFAULT 0,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    closed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sop_state ON sop_sessions (sop_state);
                CREATE INDEX IF NOT EXISTS idx_codes_sent_at ON sop_sessions (codes_sent_at)
                WHERE codes_sent_at IS NOT NULL;
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_msg_user ON messages (user_id, id);
                CREATE TABLE IF NOT EXISTS pending_human_queries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    human_reply TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_phq_user_status
                    ON pending_human_queries (user_id, status);
                """
            )
            self._ensure_column(conn, "sop_sessions", "game_role_id", "game_role_id TEXT")
            self._ensure_column(conn, "sop_sessions", "channel_chat_id", "channel_chat_id TEXT")
            self._ensure_column(conn, "sop_sessions", "is_closed", "is_closed INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sop_sessions", "closed_at", "closed_at TEXT")
            conn.commit()

    def get_or_create_session(self, user_id: str, default_game_name: str = "顽石英雄之大楚复古") -> SOPSessionState:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sop_sessions (
                        user_id, sop_state, game_name, area_name, role_name, game_role_id,
                        channel_chat_id, screenshot_path, retry_count, codes_sent_at,
                        follow_up_30m_sent, follow_up_1h_sent, next_day_visited,
                        is_closed, closed_at, created_at, updated_at
                    ) VALUES (?, 'greeting', ?, NULL, NULL, NULL, NULL, NULL, 0, NULL, 0, 0, 0, 0, NULL, ?, ?)
                    """,
                    (user_id, default_game_name, now, now),
                )
                conn.commit()
                row = conn.execute("SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)).fetchone()
            return _row_to_state(row)

    def update_session(
        self,
        user_id: str,
        *,
        sop_state: str | object = _UNSET,
        game_name: str | None | object = _UNSET,
        area_name: str | None | object = _UNSET,
        role_name: str | None | object = _UNSET,
        game_role_id: str | None | object = _UNSET,
        channel_chat_id: str | None | object = _UNSET,
        screenshot_path: str | None | object = _UNSET,
        retry_count: int | object = _UNSET,
        codes_sent_at: str | None | object = _UNSET,
        follow_up_30m_sent: bool | object = _UNSET,
        follow_up_1h_sent: bool | object = _UNSET,
        next_day_visited: bool | object = _UNSET,
        is_closed: bool | object = _UNSET,
        closed_at: str | None | object = _UNSET,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        cur = self.get_or_create_session(user_id, default_game_name)
        now = _now_iso()

        def v(new_value: Any, current: Any) -> Any:
            return current if new_value is _UNSET else new_value

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sop_sessions
                SET sop_state = ?, game_name = ?, area_name = ?, role_name = ?, game_role_id = ?,
                    channel_chat_id = ?, screenshot_path = ?, retry_count = ?, codes_sent_at = ?,
                    follow_up_30m_sent = ?, follow_up_1h_sent = ?, next_day_visited = ?,
                    is_closed = ?, closed_at = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    v(sop_state, cur.sop_state),
                    v(game_name, cur.game_name),
                    v(area_name, cur.area_name),
                    v(role_name, cur.role_name),
                    v(game_role_id, cur.game_role_id),
                    v(channel_chat_id, cur.channel_chat_id),
                    v(screenshot_path, cur.screenshot_path),
                    v(retry_count, cur.retry_count),
                    v(codes_sent_at, cur.codes_sent_at),
                    int(v(follow_up_30m_sent, cur.follow_up_30m_sent)),
                    int(v(follow_up_1h_sent, cur.follow_up_1h_sent)),
                    int(v(next_day_visited, cur.next_day_visited)),
                    int(v(is_closed, cur.is_closed)),
                    v(closed_at, cur.closed_at),
                    now,
                    user_id,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)).fetchone()
            return _row_to_state(row)

    def reset_session(self, user_id: str, default_game_name: str = "顽石英雄之大楚复古") -> SOPSessionState:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sop_sessions
                SET sop_state='greeting', game_name=?, area_name=NULL, role_name=NULL, game_role_id=NULL,
                    channel_chat_id=NULL, screenshot_path=NULL, retry_count=0, codes_sent_at=NULL,
                    follow_up_30m_sent=0, follow_up_1h_sent=0, next_day_visited=0,
                    is_closed=0, closed_at=NULL, updated_at=?
                WHERE user_id=?
                """,
                (default_game_name, now, user_id),
            )
            conn.commit()
        return self.get_or_create_session(user_id, default_game_name)

    def close_session(self, user_id: str, default_game_name: str) -> SOPSessionState:
        return self.update_session(user_id, is_closed=True, closed_at=_now_iso(), default_game_name=default_game_name)

    def reopen_session(self, user_id: str, default_game_name: str) -> SOPSessionState:
        return self.update_session(user_id, is_closed=False, closed_at=None, default_game_name=default_game_name)

    def get_pending_30m_followups(self, now_iso: str) -> list[SOPSessionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sop_sessions
                WHERE codes_sent_at IS NOT NULL
                  AND follow_up_30m_sent = 0
                  AND sop_state = 'follow_up_pending'
                  AND datetime(codes_sent_at, '+30 minutes') <= datetime(?)
                """,
                (now_iso,),
            ).fetchall()
        return [_row_to_state(r) for r in rows]

    def get_pending_1h_followups(self, now_iso: str) -> list[SOPSessionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sop_sessions
                WHERE codes_sent_at IS NOT NULL
                  AND follow_up_1h_sent = 0
                  AND sop_state IN ('follow_up_pending', 'follow_up_30min')
                  AND datetime(codes_sent_at, '+60 minutes') <= datetime(?)
                """,
                (now_iso,),
            ).fetchall()
        return [_row_to_state(r) for r in rows]

    def get_pending_next_day_visits(self, now_iso: str) -> list[SOPSessionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sop_sessions
                WHERE next_day_visited = 0
                  AND sop_state = 'silent'
                  AND codes_sent_at IS NOT NULL
                  AND datetime(codes_sent_at, '+24 hours') <= datetime(?)
                """,
                (now_iso,),
            ).fetchall()
        return [_row_to_state(r) for r in rows]

    def append_message(self, user_id: str, role: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, content, _now_iso()),
            )
            conn.commit()

    def get_recent_messages(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, created_at
                FROM messages
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_session_messages(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.get_recent_messages(user_id, limit=limit)

    def create_human_query(self, user_id: str, question: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_human_queries (user_id, question, status, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (user_id, question, _now_iso()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def update_human_reply(self, query_id: int, reply: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pending_human_queries
                SET human_reply = ?, status = 'answered', answered_at = ?
                WHERE id = ?
                """,
                (reply, _now_iso(), query_id),
            )
            conn.commit()

    def get_pending_delivery_queries(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, question, status, human_reply, created_at, answered_at
                FROM pending_human_queries
                WHERE user_id = ? AND status = 'answered'
                ORDER BY id ASC
                """,
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_query_delivered(self, query_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE pending_human_queries SET status='delivered' WHERE id = ?", (query_id,))
            conn.commit()

    def get_pending_queries_all(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, question, status, human_reply, created_at, answered_at
                FROM pending_human_queries
                WHERE status IN ('pending', 'answered')
                ORDER BY id ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def list_human_queries(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = [
            "SELECT id, user_id, question, status, human_reply, created_at, answered_at FROM pending_human_queries"
        ]
        params: list[Any] = []
        if status:
            sql.append("WHERE status = ?")
            params.append(status)
        sql.append("ORDER BY created_at DESC LIMIT ?")
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(
        self,
        *,
        limit: int = 20,
        include_closed: bool = True,
        sop_state: str | None = None,
        query: str | None = None,
    ) -> list[SOPSessionState]:
        sql = ["SELECT * FROM sop_sessions WHERE 1=1"]
        params: list[Any] = []
        if not include_closed:
            sql.append("AND is_closed = 0")
        if sop_state:
            sql.append("AND sop_state = ?")
            params.append(sop_state)
        if query:
            like = f"%{query}%"
            sql.append("AND (user_id LIKE ? OR COALESCE(area_name, '') LIKE ? OR COALESCE(role_name, '') LIKE ?)")
            params.extend([like, like, like])
        sql.append("ORDER BY updated_at DESC LIMIT ?")
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [_row_to_state(r) for r in rows]

    def get_summary_counts(self) -> dict[str, Any]:
        with self._connect() as conn:
            total_customers = conn.execute("SELECT COUNT(*) FROM sop_sessions").fetchone()[0]
            open_customers = conn.execute("SELECT COUNT(*) FROM sop_sessions WHERE is_closed = 0").fetchone()[0]
            closed_customers = conn.execute("SELECT COUNT(*) FROM sop_sessions WHERE is_closed = 1").fetchone()[0]
            bound_customers = conn.execute(
                """
                SELECT COUNT(*) FROM sop_sessions
                WHERE sop_state IN (
                    'sending_code', 'follow_up_pending', 'follow_up_30min',
                    'follow_up_1hour', 'silent', 'next_day_visit',
                    'reactivation', 'completed'
                )
                """
            ).fetchone()[0]
            active_24h = conn.execute(
                "SELECT COUNT(*) FROM sop_sessions WHERE datetime(updated_at) >= datetime('now', '-1 day')"
            ).fetchone()[0]
            pending_human_queries = conn.execute(
                "SELECT COUNT(*) FROM pending_human_queries WHERE status = 'pending'"
            ).fetchone()[0]
            answered_human_queries = conn.execute(
                "SELECT COUNT(*) FROM pending_human_queries WHERE status = 'answered'"
            ).fetchone()[0]
            delivered_human_queries = conn.execute(
                "SELECT COUNT(*) FROM pending_human_queries WHERE status = 'delivered'"
            ).fetchone()[0]
            state_rows = conn.execute(
                "SELECT sop_state, COUNT(*) AS count FROM sop_sessions GROUP BY sop_state ORDER BY count DESC, sop_state ASC"
            ).fetchall()
        return {
            "total_customers": int(total_customers),
            "open_customers": int(open_customers),
            "closed_customers": int(closed_customers),
            "ai_auto_reply_enabled_customers": int(open_customers),
            "ai_auto_reply_disabled_customers": int(closed_customers),
            "bound_customers": int(bound_customers),
            "active_24h": int(active_24h),
            "pending_human_queries": int(pending_human_queries),
            "answered_human_queries": int(answered_human_queries),
            "delivered_human_queries": int(delivered_human_queries),
            "sop_state_counts": {str(row["sop_state"]): int(row["count"]) for row in state_rows},
        }


class _MySQLGameCSStore(_BaseGameCSStore):
    def __init__(self, config: GameCSConfig) -> None:
        if pymysql is None or DictCursor is None:
            raise RuntimeError("PyMySQL is required for GAME_CS_DB_DRIVER=mysql")
        self.config = config
        self._create_database_if_needed()
        self._init_db()

    def _server_connect(self, *, database: str | None) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.config.db_host,
            port=self.config.db_port,
            user=self.config.db_user,
            password=self.config.db_password,
            database=database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
        )

    def _connect(self) -> pymysql.connections.Connection:
        return self._server_connect(database=self.config.db_name)

    def _create_database_if_needed(self) -> None:
        conn = self._server_connect(database=None)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.config.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            conn.commit()
        finally:
            conn.close()

    def _column_exists(self, conn: pymysql.connections.Connection, table: str, column: str) -> bool:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s AND column_name = %s
                LIMIT 1
                """,
                (self.config.db_name, table, column),
            )
            return cur.fetchone() is not None

    def _ensure_column(self, conn: pymysql.connections.Connection, table: str, column: str, ddl: str) -> None:
        if not self._column_exists(conn, table, column):
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sop_sessions (
                        user_id VARCHAR(64) NOT NULL PRIMARY KEY,
                        sop_state VARCHAR(64) NOT NULL DEFAULT 'greeting',
                        game_name VARCHAR(255) NULL,
                        area_name VARCHAR(255) NULL,
                        role_name VARCHAR(255) NULL,
                        game_role_id VARCHAR(255) NULL,
                        channel_chat_id VARCHAR(255) NULL,
                        screenshot_path TEXT NULL,
                        retry_count INT NOT NULL DEFAULT 0,
                        codes_sent_at VARCHAR(32) NULL,
                        follow_up_30m_sent TINYINT(1) NOT NULL DEFAULT 0,
                        follow_up_1h_sent TINYINT(1) NOT NULL DEFAULT 0,
                        next_day_visited TINYINT(1) NOT NULL DEFAULT 0,
                        is_closed TINYINT(1) NOT NULL DEFAULT 0,
                        closed_at VARCHAR(32) NULL,
                        created_at VARCHAR(32) NOT NULL,
                        updated_at VARCHAR(32) NOT NULL,
                        INDEX idx_sop_state (sop_state),
                        INDEX idx_codes_sent_at (codes_sent_at)
                    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL,
                        role VARCHAR(32) NOT NULL,
                        content TEXT NOT NULL,
                        created_at VARCHAR(32) NOT NULL,
                        INDEX idx_msg_user (user_id, id)
                    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pending_human_queries (
                        id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                        user_id VARCHAR(64) NOT NULL,
                        question TEXT NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'pending',
                        human_reply TEXT NULL,
                        created_at VARCHAR(32) NOT NULL,
                        answered_at VARCHAR(32) NULL,
                        INDEX idx_phq_user_status (user_id, status)
                    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                    """
                )
            self._ensure_column(conn, "sop_sessions", "game_role_id", "game_role_id VARCHAR(255) NULL")
            self._ensure_column(conn, "sop_sessions", "channel_chat_id", "channel_chat_id VARCHAR(255) NULL")
            self._ensure_column(conn, "sop_sessions", "is_closed", "is_closed TINYINT(1) NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sop_sessions", "closed_at", "closed_at VARCHAR(32) NULL")
            conn.commit()
        finally:
            conn.close()

    def get_or_create_session(self, user_id: str, default_game_name: str = "顽石英雄之大楚复古") -> SOPSessionState:
        now = _now_iso()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sop_sessions WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO sop_sessions (
                            user_id, sop_state, game_name, area_name, role_name, game_role_id,
                            channel_chat_id, screenshot_path, retry_count, codes_sent_at,
                            follow_up_30m_sent, follow_up_1h_sent, next_day_visited,
                            is_closed, closed_at, created_at, updated_at
                        ) VALUES (%s, 'greeting', %s, NULL, NULL, NULL, NULL, NULL, 0, NULL, 0, 0, 0, 0, NULL, %s, %s)
                        """,
                        (user_id, default_game_name, now, now),
                    )
                    conn.commit()
                    cur.execute("SELECT * FROM sop_sessions WHERE user_id = %s", (user_id,))
                    row = cur.fetchone()
            return _row_to_state(row)
        finally:
            conn.close()

    def update_session(
        self,
        user_id: str,
        *,
        sop_state: str | object = _UNSET,
        game_name: str | None | object = _UNSET,
        area_name: str | None | object = _UNSET,
        role_name: str | None | object = _UNSET,
        game_role_id: str | None | object = _UNSET,
        channel_chat_id: str | None | object = _UNSET,
        screenshot_path: str | None | object = _UNSET,
        retry_count: int | object = _UNSET,
        codes_sent_at: str | None | object = _UNSET,
        follow_up_30m_sent: bool | object = _UNSET,
        follow_up_1h_sent: bool | object = _UNSET,
        next_day_visited: bool | object = _UNSET,
        is_closed: bool | object = _UNSET,
        closed_at: str | None | object = _UNSET,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        cur_state = self.get_or_create_session(user_id, default_game_name)
        now = _now_iso()

        def v(new_value: Any, current: Any) -> Any:
            return current if new_value is _UNSET else new_value

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sop_sessions
                    SET sop_state = %s, game_name = %s, area_name = %s, role_name = %s, game_role_id = %s,
                        channel_chat_id = %s, screenshot_path = %s, retry_count = %s, codes_sent_at = %s,
                        follow_up_30m_sent = %s, follow_up_1h_sent = %s, next_day_visited = %s,
                        is_closed = %s, closed_at = %s, updated_at = %s
                    WHERE user_id = %s
                    """,
                    (
                        v(sop_state, cur_state.sop_state),
                        v(game_name, cur_state.game_name),
                        v(area_name, cur_state.area_name),
                        v(role_name, cur_state.role_name),
                        v(game_role_id, cur_state.game_role_id),
                        v(channel_chat_id, cur_state.channel_chat_id),
                        v(screenshot_path, cur_state.screenshot_path),
                        v(retry_count, cur_state.retry_count),
                        v(codes_sent_at, cur_state.codes_sent_at),
                        int(v(follow_up_30m_sent, cur_state.follow_up_30m_sent)),
                        int(v(follow_up_1h_sent, cur_state.follow_up_1h_sent)),
                        int(v(next_day_visited, cur_state.next_day_visited)),
                        int(v(is_closed, cur_state.is_closed)),
                        v(closed_at, cur_state.closed_at),
                        now,
                        user_id,
                    ),
                )
                conn.commit()
                cur.execute("SELECT * FROM sop_sessions WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
            return _row_to_state(row)
        finally:
            conn.close()

    def reset_session(self, user_id: str, default_game_name: str = "顽石英雄之大楚复古") -> SOPSessionState:
        now = _now_iso()
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sop_sessions
                    SET sop_state='greeting', game_name=%s, area_name=NULL, role_name=NULL, game_role_id=NULL,
                        channel_chat_id=NULL, screenshot_path=NULL, retry_count=0, codes_sent_at=NULL,
                        follow_up_30m_sent=0, follow_up_1h_sent=0, next_day_visited=0,
                        is_closed=0, closed_at=NULL, updated_at=%s
                    WHERE user_id=%s
                    """,
                    (default_game_name, now, user_id),
                )
            conn.commit()
        finally:
            conn.close()
        return self.get_or_create_session(user_id, default_game_name)

    def close_session(self, user_id: str, default_game_name: str) -> SOPSessionState:
        return self.update_session(user_id, is_closed=True, closed_at=_now_iso(), default_game_name=default_game_name)

    def reopen_session(self, user_id: str, default_game_name: str) -> SOPSessionState:
        return self.update_session(user_id, is_closed=False, closed_at=None, default_game_name=default_game_name)

    def _get_pending_followups(
        self,
        now_iso: str,
        *,
        state_sql: str,
        delta: timedelta,
    ) -> list[SOPSessionState]:
        now_dt = _parse_iso(now_iso)
        if now_dt is None:
            return []
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM sop_sessions WHERE codes_sent_at IS NOT NULL AND {state_sql}")
                rows = cur.fetchall()
            out: list[SOPSessionState] = []
            for row in rows:
                sent_at = _parse_iso(row.get("codes_sent_at"))
                if sent_at is not None and sent_at + delta <= now_dt:
                    out.append(_row_to_state(row))
            return out
        finally:
            conn.close()

    def get_pending_30m_followups(self, now_iso: str) -> list[SOPSessionState]:
        return self._get_pending_followups(
            now_iso,
            state_sql="follow_up_30m_sent = 0 AND sop_state = 'follow_up_pending'",
            delta=timedelta(minutes=30),
        )

    def get_pending_1h_followups(self, now_iso: str) -> list[SOPSessionState]:
        return self._get_pending_followups(
            now_iso,
            state_sql="follow_up_1h_sent = 0 AND sop_state IN ('follow_up_pending', 'follow_up_30min')",
            delta=timedelta(hours=1),
        )

    def get_pending_next_day_visits(self, now_iso: str) -> list[SOPSessionState]:
        return self._get_pending_followups(
            now_iso,
            state_sql="next_day_visited = 0 AND sop_state = 'silent'",
            delta=timedelta(hours=24),
        )

    def append_message(self, user_id: str, role: str, content: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO messages (user_id, role, content, created_at) VALUES (%s, %s, %s, %s)",
                    (user_id, role, content, _now_iso()),
                )
            conn.commit()
        finally:
            conn.close()

    def get_recent_messages(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content, created_at
                    FROM messages
                    WHERE user_id = %s
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
                rows = cur.fetchall()
            return [dict(row) for row in reversed(rows)]
        finally:
            conn.close()

    def get_session_messages(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.get_recent_messages(user_id, limit=limit)

    def create_human_query(self, user_id: str, question: str) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pending_human_queries (user_id, question, status, created_at)
                    VALUES (%s, %s, 'pending', %s)
                    """,
                    (user_id, question, _now_iso()),
                )
                query_id = int(cur.lastrowid)
            conn.commit()
            return query_id
        finally:
            conn.close()

    def update_human_reply(self, query_id: int, reply: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE pending_human_queries
                    SET human_reply = %s, status = 'answered', answered_at = %s
                    WHERE id = %s
                    """,
                    (reply, _now_iso(), query_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get_pending_delivery_queries(self, user_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, question, status, human_reply, created_at, answered_at
                    FROM pending_human_queries
                    WHERE user_id = %s AND status = 'answered'
                    ORDER BY id ASC
                    """,
                    (user_id,),
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def mark_query_delivered(self, query_id: int) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE pending_human_queries SET status='delivered' WHERE id = %s", (query_id,))
            conn.commit()
        finally:
            conn.close()

    def get_pending_queries_all(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, question, status, human_reply, created_at, answered_at
                    FROM pending_human_queries
                    WHERE status IN ('pending', 'answered')
                    ORDER BY id ASC
                    """
                )
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def list_human_queries(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql = [
            "SELECT id, user_id, question, status, human_reply, created_at, answered_at FROM pending_human_queries"
        ]
        params: list[Any] = []
        if status:
            sql.append("WHERE status = %s")
            params.append(status)
        sql.append("ORDER BY created_at DESC LIMIT %s")
        params.append(limit)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(" ".join(sql), params)
                return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def list_sessions(
        self,
        *,
        limit: int = 20,
        include_closed: bool = True,
        sop_state: str | None = None,
        query: str | None = None,
    ) -> list[SOPSessionState]:
        sql = ["SELECT * FROM sop_sessions WHERE 1=1"]
        params: list[Any] = []
        if not include_closed:
            sql.append("AND is_closed = 0")
        if sop_state:
            sql.append("AND sop_state = %s")
            params.append(sop_state)
        if query:
            like = f"%{query}%"
            sql.append("AND (user_id LIKE %s OR COALESCE(area_name, '') LIKE %s OR COALESCE(role_name, '') LIKE %s)")
            params.extend([like, like, like])
        sql.append("ORDER BY updated_at DESC LIMIT %s")
        params.append(limit)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(" ".join(sql), params)
                return [_row_to_state(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def get_summary_counts(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS count FROM sop_sessions")
                total_customers = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM sop_sessions WHERE is_closed = 0")
                open_customers = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM sop_sessions WHERE is_closed = 1")
                closed_customers = int(cur.fetchone()["count"])
                cur.execute(
                    """
                    SELECT COUNT(*) AS count FROM sop_sessions
                    WHERE sop_state IN (
                        'sending_code', 'follow_up_pending', 'follow_up_30min',
                        'follow_up_1hour', 'silent', 'next_day_visit',
                        'reactivation', 'completed'
                    )
                    """
                )
                bound_customers = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM pending_human_queries WHERE status = 'pending'")
                pending_human_queries = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM pending_human_queries WHERE status = 'answered'")
                answered_human_queries = int(cur.fetchone()["count"])
                cur.execute("SELECT COUNT(*) AS count FROM pending_human_queries WHERE status = 'delivered'")
                delivered_human_queries = int(cur.fetchone()["count"])
                cur.execute(
                    """
                    SELECT sop_state, COUNT(*) AS count
                    FROM sop_sessions
                    GROUP BY sop_state
                    ORDER BY count DESC, sop_state ASC
                    """
                )
                state_rows = cur.fetchall()
                cur.execute("SELECT updated_at FROM sop_sessions")
                active_24h = 0
                cutoff = now_datetime() - timedelta(days=1)
                for row in cur.fetchall():
                    updated_at = _parse_iso(row["updated_at"])
                    if updated_at is not None and updated_at >= cutoff:
                        active_24h += 1
            return {
                "total_customers": total_customers,
                "open_customers": open_customers,
                "closed_customers": closed_customers,
                "ai_auto_reply_enabled_customers": open_customers,
                "ai_auto_reply_disabled_customers": closed_customers,
                "bound_customers": bound_customers,
                "active_24h": active_24h,
                "pending_human_queries": pending_human_queries,
                "answered_human_queries": answered_human_queries,
                "delivered_human_queries": delivered_human_queries,
                "sop_state_counts": {str(row["sop_state"]): int(row["count"]) for row in state_rows},
            }
        finally:
            conn.close()


class GameCSStore:
    def __init__(self, config: GameCSConfig) -> None:
        if config.db_driver == "sqlite":
            self._backend: _BaseGameCSStore = _SqliteGameCSStore(config.db_path)
        elif config.db_driver == "mysql":
            self._backend = _MySQLGameCSStore(config)
        else:
            raise ValueError(f"unsupported GAME_CS_DB_DRIVER: {config.db_driver}")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._backend, name)


def _row_to_state(row: Mapping[str, Any]) -> SOPSessionState:
    keys = set(row.keys())
    def value(key: str) -> Any:
        return row[key] if key in keys else None

    return SOPSessionState(
        user_id=str(row["user_id"]),
        sop_state=str(row["sop_state"]),
        game_name=value("game_name"),
        area_name=value("area_name"),
        role_name=value("role_name"),
        game_role_id=value("game_role_id"),
        channel_chat_id=value("channel_chat_id"),
        screenshot_path=value("screenshot_path"),
        retry_count=int(row["retry_count"]),
        codes_sent_at=value("codes_sent_at"),
        follow_up_30m_sent=bool(row["follow_up_30m_sent"]),
        follow_up_1h_sent=bool(row["follow_up_1h_sent"]),
        next_day_visited=bool(row["next_day_visited"]),
        is_closed=bool(row["is_closed"]) if "is_closed" in keys else False,
        closed_at=value("closed_at"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
