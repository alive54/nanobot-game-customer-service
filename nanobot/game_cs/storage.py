from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class GameCSStore:
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
        cols = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sop_sessions (
                    user_id              TEXT    NOT NULL PRIMARY KEY,
                    sop_state            TEXT    NOT NULL DEFAULT 'greeting',
                    game_name            TEXT,
                    area_name            TEXT,
                    role_name            TEXT,
                    game_role_id         TEXT,
                    channel_chat_id      TEXT,
                    screenshot_path      TEXT,
                    retry_count          INTEGER NOT NULL DEFAULT 0,
                    codes_sent_at        TEXT,
                    follow_up_30m_sent   INTEGER NOT NULL DEFAULT 0,
                    follow_up_1h_sent    INTEGER NOT NULL DEFAULT 0,
                    next_day_visited     INTEGER NOT NULL DEFAULT 0,
                    is_closed            INTEGER NOT NULL DEFAULT 0,
                    closed_at            TEXT,
                    created_at           TEXT    NOT NULL,
                    updated_at           TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sop_state
                    ON sop_sessions (sop_state);

                CREATE INDEX IF NOT EXISTS idx_codes_sent_at
                    ON sop_sessions (codes_sent_at)
                    WHERE codes_sent_at IS NOT NULL;

                CREATE TABLE IF NOT EXISTS messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    role       TEXT    NOT NULL,
                    content    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_msg_user
                    ON messages (user_id, id);

                CREATE TABLE IF NOT EXISTS pending_human_queries (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      TEXT    NOT NULL,
                    question     TEXT    NOT NULL,
                    status       TEXT    NOT NULL DEFAULT 'pending',
                    human_reply  TEXT,
                    created_at   TEXT    NOT NULL,
                    answered_at  TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_phq_user_status
                    ON pending_human_queries (user_id, status);
                """
            )

            # Backward-compatible migrations for older DB files.
            self._ensure_column(conn, "sop_sessions", "game_role_id", "game_role_id TEXT")
            self._ensure_column(conn, "sop_sessions", "channel_chat_id", "channel_chat_id TEXT")
            self._ensure_column(conn, "sop_sessions", "is_closed", "is_closed INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sop_sessions", "closed_at", "closed_at TEXT")
            conn.commit()

    def get_or_create_session(
        self,
        user_id: str,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sop_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sop_sessions (
                        user_id,
                        sop_state,
                        game_name,
                        area_name,
                        role_name,
                        game_role_id,
                        channel_chat_id,
                        screenshot_path,
                        retry_count,
                        codes_sent_at,
                        follow_up_30m_sent,
                        follow_up_1h_sent,
                        next_day_visited,
                        is_closed,
                        closed_at,
                        created_at,
                        updated_at
                    ) VALUES (?, 'greeting', ?, NULL, NULL, NULL, NULL, NULL, 0, NULL, 0, 0, 0, 0, NULL, ?, ?)
                    """,
                    (user_id, default_game_name, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM sop_sessions WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
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
                SET sop_state          = ?,
                    game_name          = ?,
                    area_name          = ?,
                    role_name          = ?,
                    game_role_id       = ?,
                    channel_chat_id    = ?,
                    screenshot_path    = ?,
                    retry_count        = ?,
                    codes_sent_at      = ?,
                    follow_up_30m_sent = ?,
                    follow_up_1h_sent  = ?,
                    next_day_visited   = ?,
                    is_closed          = ?,
                    closed_at          = ?,
                    updated_at         = ?
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
            row = conn.execute(
                "SELECT * FROM sop_sessions WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return _row_to_state(row)

    def reset_session(
        self,
        user_id: str,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sop_sessions
                SET sop_state='greeting',
                    game_name=?,
                    area_name=NULL,
                    role_name=NULL,
                    game_role_id=NULL,
                    channel_chat_id=NULL,
                    screenshot_path=NULL,
                    retry_count=0,
                    codes_sent_at=NULL,
                    follow_up_30m_sent=0,
                    follow_up_1h_sent=0,
                    next_day_visited=0,
                    is_closed=0,
                    closed_at=NULL,
                    updated_at=?
                WHERE user_id=?
                """,
                (default_game_name, now, user_id),
            )
            conn.commit()
        return self.get_or_create_session(user_id, default_game_name)

    def close_session(
        self,
        user_id: str,
        default_game_name: str,
    ) -> SOPSessionState:
        return self.update_session(
            user_id,
            is_closed=True,
            closed_at=_now_iso(),
            default_game_name=default_game_name,
        )

    def reopen_session(
        self,
        user_id: str,
        default_game_name: str,
    ) -> SOPSessionState:
        return self.update_session(
            user_id,
            is_closed=False,
            closed_at=None,
            default_game_name=default_game_name,
        )

    def get_pending_30m_followups(self, now_iso: str) -> list[SOPSessionState]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM sop_sessions
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
                SELECT *
                FROM sop_sessions
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
                SELECT *
                FROM sop_sessions
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

    def get_recent_messages(self, user_id: str, limit: int = 10) -> list[dict]:
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

    def get_session_messages(self, user_id: str, limit: int = 20) -> list[dict]:
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

    def get_pending_delivery_queries(self, user_id: str) -> list[dict]:
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
            conn.execute(
                "UPDATE pending_human_queries SET status='delivered' WHERE id = ?",
                (query_id,),
            )
            conn.commit()

    def get_pending_queries_all(self) -> list[dict]:
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

    def list_human_queries(self, status: str | None = None) -> list[dict]:
        sql = [
            """
            SELECT id, user_id, question, status, human_reply, created_at, answered_at
            FROM pending_human_queries
            """
        ]
        params: list[Any] = []
        if status:
            sql.append("WHERE status = ?")
            params.append(status)
        sql.append("ORDER BY id ASC")
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(
        self,
        *,
        limit: int = 100,
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
            sql.append(
                "AND (user_id LIKE ? OR COALESCE(area_name, '') LIKE ? OR COALESCE(role_name, '') LIKE ?)"
            )
            params.extend([like, like, like])
        sql.append("ORDER BY datetime(updated_at) DESC LIMIT ?")
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [_row_to_state(r) for r in rows]

    def get_summary_counts(self) -> dict[str, Any]:
        with self._connect() as conn:
            total_customers = conn.execute("SELECT COUNT(*) FROM sop_sessions").fetchone()[0]
            open_customers = conn.execute(
                "SELECT COUNT(*) FROM sop_sessions WHERE is_closed = 0"
            ).fetchone()[0]
            closed_customers = conn.execute(
                "SELECT COUNT(*) FROM sop_sessions WHERE is_closed = 1"
            ).fetchone()[0]
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
                """
                SELECT sop_state, COUNT(*) AS count
                FROM sop_sessions
                GROUP BY sop_state
                ORDER BY count DESC, sop_state ASC
                """
            ).fetchall()
        return {
            "total_customers": int(total_customers),
            "open_customers": int(open_customers),
            "closed_customers": int(closed_customers),
            "bound_customers": int(bound_customers),
            "active_24h": int(active_24h),
            "pending_human_queries": int(pending_human_queries),
            "answered_human_queries": int(answered_human_queries),
            "delivered_human_queries": int(delivered_human_queries),
            "sop_state_counts": {
                str(row["sop_state"]): int(row["count"]) for row in state_rows
            },
        }


def _row_to_state(row: sqlite3.Row) -> SOPSessionState:
    return SOPSessionState(
        user_id=row["user_id"],
        sop_state=row["sop_state"],
        game_name=row["game_name"],
        area_name=row["area_name"],
        role_name=row["role_name"],
        game_role_id=row["game_role_id"] if "game_role_id" in row.keys() else None,
        channel_chat_id=row["channel_chat_id"] if "channel_chat_id" in row.keys() else None,
        screenshot_path=row["screenshot_path"],
        retry_count=row["retry_count"],
        codes_sent_at=row["codes_sent_at"],
        follow_up_30m_sent=bool(row["follow_up_30m_sent"]),
        follow_up_1h_sent=bool(row["follow_up_1h_sent"]),
        next_day_visited=bool(row["next_day_visited"]),
        is_closed=bool(row["is_closed"]) if "is_closed" in row.keys() else False,
        closed_at=row["closed_at"] if "closed_at" in row.keys() else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
