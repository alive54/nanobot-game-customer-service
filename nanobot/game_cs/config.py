from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GameCSConfig:
    # ── Auth ──────────────────────────────────────────────────────────────────
    service_token: str

    # ── Storage ───────────────────────────────────────────────────────────────
    db_path: Path
    uploads_dir: Path

    # ── OpenViking ────────────────────────────────────────────────────────────
    openviking_path: Path
    openviking_target_uri: str

    # ── Image limits ──────────────────────────────────────────────────────────
    max_image_bytes: int

    # ── Game defaults ─────────────────────────────────────────────────────────
    default_game_name: str          # 顽石英雄之大楚复古
    personality: str                # lively / professional / steady / humorous

    # ── Game API ──────────────────────────────────────────────────────────────
    game_api_base: str              # e.g. http://game-api.internal
    mock_api: bool                  # True → skip real API calls (dev / demo mode)

    # ── Daily redeem codes (updated daily by operators via env) ───────────────
    code_daily_checkin: str         # 每日打卡码  — refreshed daily
    code_lucky_draw: str            # 天选码      — refreshed daily
    code_universal: str             # 通码        — relatively stable
    code_guild: str                 # 供宗号      — relatively stable

    # ── Follow-up timing ─────────────────────────────────────────────────────
    followup_30m_delay: int         # seconds until 30-min follow-up (default 1800)
    followup_1h_delay: int          # seconds until 1-hour fission follow-up (default 3600)

    # ── Retry limits ─────────────────────────────────────────────────────────
    max_collect_retries: int        # max times to re-ask for area/role info (default 3)

    @staticmethod
    def from_env() -> "GameCSConfig":
        return GameCSConfig(
            # Auth
            service_token=os.getenv("GAME_CS_SERVICE_TOKEN", "dev-token"),

            # Storage
            db_path=Path(os.getenv("GAME_CS_DB_PATH", ".nanobot/game_cs.db")),
            uploads_dir=Path(os.getenv("GAME_CS_UPLOADS_DIR", ".nanobot/game_cs_uploads")),

            # OpenViking
            openviking_path=Path(os.getenv("GAME_CS_OPENVIKING_PATH", ".nanobot/openviking_data")),
            openviking_target_uri=os.getenv(
                "GAME_CS_OPENVIKING_TARGET_URI", "viking://resources/"
            ),

            # Image limits
            max_image_bytes=int(os.getenv("GAME_CS_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))),

            # Game defaults
            default_game_name=os.getenv(
                "GAME_CS_DEFAULT_GAME_NAME", "顽石英雄之大楚复古"
            ),
            personality=os.getenv("GAME_CS_PERSONALITY", "lively"),

            # Game API
            game_api_base=os.getenv("GAME_CS_GAME_API_BASE", ""),
            mock_api=os.getenv("GAME_CS_MOCK_API", "true").lower() in ("1", "true", "yes"),

            # Daily codes
            code_daily_checkin=os.getenv("GAME_CS_CODE_DAILY_CHECKIN", "DCXXX"),
            code_lucky_draw=os.getenv("GAME_CS_CODE_LUCKY_DRAW", "TXYYY"),
            code_universal=os.getenv("GAME_CS_CODE_UNIVERSAL", "ws888"),
            code_guild=os.getenv("GAME_CS_CODE_GUILD", "FgYdqf6"),

            # Follow-up timing
            followup_30m_delay=int(os.getenv("GAME_CS_FOLLOWUP_30M_DELAY", "1800")),
            followup_1h_delay=int(os.getenv("GAME_CS_FOLLOWUP_1H_DELAY", "3600")),

            # Retry limits
            max_collect_retries=int(os.getenv("GAME_CS_MAX_COLLECT_RETRIES", "3")),
        )
