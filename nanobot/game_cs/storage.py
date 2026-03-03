from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SOPSessionState:
    """Mirrors the sop_sessions table row."""

    user_id: str
    sop_state: str
    game_name: str | None
    area_name: str | None
    role_name: str | None
    screenshot_path: str | None
    retry_count: int
    codes_sent_at: str | None
    follow_up_30m_sent: bool
    follow_up_1h_sent: bool
    next_day_visited: bool
    created_at: str
    updated_at: str

    # Convenience ------------------------------------------------------------------

    @property
    def is_bound(self) -> bool:
        """True once the user has passed the BINDING stage."""
        from .models import SOPState

        terminal_and_post_binding = {
            SOPState.SENDING_CODE,
            SOPState.FOLLOW_UP_PENDING,
            SOPState.FOLLOW_UP_30MIN,
            SOPState.FOLLOW_UP_1HOUR,
            SOPState.SILENT,
            SOPState.NEXT_DAY_VISIT,
            SOPState.REACTIVATION,
            SOPState.COMPLETED,
        }
        return self.sop_state in {s.value for s in terminal_and_post_binding}

    @property
    def has_full_info(self) -> bool:
        """True when area_name AND role_name have been captured."""
        return bool(self.area_name) and bool(self.role_name)


class GameCSStore:
    """SQLite-backed store for SOP session state and conversation history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                -- SOP session table aligned with PRD three-factor fields
                CREATE TABLE IF NOT EXISTS sop_sessions (
                    user_id              TEXT    NOT NULL PRIMARY KEY,
                    sop_state            TEXT    NOT NULL DEFAULT 'greeting',

                    -- Three-factor fields (game_name defaults to env value on first insert)
                    game_name            TEXT,
                    area_name            TEXT,
                    role_name            TEXT,

                    -- Optional screenshot path (for OCR assist)
                    screenshot_path      TEXT,

                    -- Retry counter for COLLECTING_INFO state
                    retry_count          INTEGER NOT NULL DEFAULT 0,

                    -- Follow-up timing & anti-duplicate flags
                    codes_sent_at        TEXT,          -- ISO-8601 UTC timestamp
                    follow_up_30m_sent   INTEGER NOT NULL DEFAULT 0,
                    follow_up_1h_sent    INTEGER NOT NULL DEFAULT 0,
                    next_day_visited     INTEGER NOT NULL DEFAULT 0,

                    created_at           TEXT    NOT NULL,
                    updated_at           TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sop_state
                    ON sop_sessions (sop_state);

                CREATE INDEX IF NOT EXISTS idx_codes_sent_at
                    ON sop_sessions (codes_sent_at)
                    WHERE codes_sent_at IS NOT NULL;

                -- Conversation history (used for context and OpenViking session memory)
                CREATE TABLE IF NOT EXISTS messages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    TEXT    NOT NULL,
                    role       TEXT    NOT NULL,   -- 'user' | 'assistant'
                    content    TEXT    NOT NULL,
                    created_at TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_msg_user
                    ON messages (user_id, id);
                """
            )

    # ── Session CRUD ──────────────────────────────────────────────────────────

    def get_or_create_session(
        self,
        user_id: str,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        """Return the existing session or create a new GREETING one."""
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO sop_sessions
                        (user_id, sop_state, game_name, area_name, role_name,
                         screenshot_path, retry_count, codes_sent_at,
                         follow_up_30m_sent, follow_up_1h_sent, next_day_visited,
                         created_at, updated_at)
                    VALUES (?, 'greeting', ?, NULL, NULL, NULL, 0, NULL, 0, 0, 0, ?, ?)
                    """,
                    (user_id, default_game_name, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)
                ).fetchone()
            return _row_to_state(row)

    def update_session(
        self,
        user_id: str,
        *,
        sop_state: str | None = None,
        game_name: str | None = None,
        area_name: str | None = None,
        role_name: str | None = None,
        screenshot_path: str | None = None,
        retry_count: int | None = None,
        codes_sent_at: str | None = None,
        follow_up_30m_sent: bool | None = None,
        follow_up_1h_sent: bool | None = None,
        next_day_visited: bool | None = None,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        """Update one or more fields on an existing (or freshly-created) session."""
        cur = self.get_or_create_session(user_id, default_game_name)
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sop_sessions
                SET sop_state          = ?,
                    game_name          = ?,
                    area_name          = ?,
                    role_name          = ?,
                    screenshot_path    = ?,
                    retry_count        = ?,
                    codes_sent_at      = ?,
                    follow_up_30m_sent = ?,
                    follow_up_1h_sent  = ?,
                    next_day_visited   = ?,
                    updated_at         = ?
                WHERE user_id = ?
                """,
                (
                    sop_state if sop_state is not None else cur.sop_state,
                    game_name if game_name is not None else cur.game_name,
                    area_name if area_name is not None else cur.area_name,
                    role_name if role_name is not None else cur.role_name,
                    screenshot_path if screenshot_path is not None else cur.screenshot_path,
                    retry_count if retry_count is not None else cur.retry_count,
                    codes_sent_at if codes_sent_at is not None else cur.codes_sent_at,
                    int(follow_up_30m_sent)
                    if follow_up_30m_sent is not None
                    else int(cur.follow_up_30m_sent),
                    int(follow_up_1h_sent)
                    if follow_up_1h_sent is not None
                    else int(cur.follow_up_1h_sent),
                    int(next_day_visited)
                    if next_day_visited is not None
                    else int(cur.next_day_visited),
                    now,
                    user_id,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM sop_sessions WHERE user_id = ?", (user_id,)
            ).fetchone()
            return _row_to_state(row)

    def reset_session(
        self,
        user_id: str,
        default_game_name: str = "顽石英雄之大楚复古",
    ) -> SOPSessionState:
        """Wipe a user's session back to GREETING (e.g. for re-registration)."""
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sop_sessions
                SET sop_state='greeting', game_name=?, area_name=NULL, role_name=NULL,
                    screenshot_path=NULL, retry_count=0, codes_sent_at=NULL,
                    follow_up_30m_sent=0, follow_up_1h_sent=0, next_day_visited=0,
                    updated_at=?
                WHERE user_id=?
                """,
                (default_game_name, now, user_id),
            )
            conn.commit()
        return self.get_or_create_session(user_id, default_game_name)

    # ── Follow-up queries ─────────────────────────────────────────────────────

    def get_pending_30m_followups(self, now_iso: str) -> list[SOPSessionState]:
        """
        Return sessions where the 30-minute follow-up is due but not yet sent.
        Caller passes current UTC timestamp as ISO-8601 string.
        """
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
        """
        Return sessions where the 1-hour fission follow-up is due but not yet sent.
        Matches sessions still in follow_up_pending OR follow_up_30min.
        """
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
        """
        Return sessions in SILENT state where next-day visit (24 h) is due.
        """
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

    # ── Message history ───────────────────────────────────────────────────────

    def append_message(self, user_id: str, role: str, content: str) -> None:
        """Append a chat message to the history."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, content, _now_iso()),
            )
            conn.commit()

    def get_recent_messages(self, user_id: str, limit: int = 10) -> list[dict]:
        """Return the last *limit* messages for a user, oldest first."""
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


# ── Row helper ────────────────────────────────────────────────────────────────

def _row_to_state(row: sqlite3.Row) -> SOPSessionState:
    return SOPSessionState(
        user_id=row["user_id"],
        sop_state=row["sop_state"],
        game_name=row["game_name"],
        area_name=row["area_name"],
        role_name=row["role_name"],
        screenshot_path=row["screenshot_path"],
        retry_count=row["retry_count"],
        codes_sent_at=row["codes_sent_at"],
        follow_up_30m_sent=bool(row["follow_up_30m_sent"]),
        follow_up_1h_sent=bool(row["follow_up_1h_sent"]),
        next_day_visited=bool(row["next_day_visited"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
