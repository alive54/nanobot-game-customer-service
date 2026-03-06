from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GameCSConfig:
    service_token: str
    db_path: Path
    uploads_dir: Path
    openviking_path: Path
    openviking_target_uri: str
    max_image_bytes: int
    default_game_name: str
    personality: str
    game_api_base: str
    mock_api: bool
    code_daily_checkin: str
    code_lucky_draw: str
    code_universal: str
    code_guild: str
    followup_30m_delay: int
    followup_1h_delay: int
    max_collect_retries: int
    ai_enabled: bool
    ai_timeout_ms: int
    ai_max_context_msgs: int
    ai_fallback_mode: str
    kb_handoff_score_threshold: float
    ai_tool_whitelist: tuple[str, ...]
    ai_info_extract_confidence_threshold: float

    # Feature 1: intent + incomplete-info heartbeat
    intent_enabled: bool = True
    intent_model: str = "qwen-turbo"
    intent_api_key: str = "sk-c96d32af68ce4f85b03e4851dd2e5f68"
    intent_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    intent_timeout_ms: int = 5000
    collecting_timeout_s: int = 300

    # Feature 2: human escalation
    admin_gateway_url: str = "http://127.0.0.1:18790/message"
    admin_gateway_enabled: bool = True
    admin_gateway_token: str = ""
    admin_query_timeout_s: int = 600

    # Feature 4: vision extraction
    vision_enabled: bool = True
    vision_api_key: str = "sk-c96d32af68ce4f85b03e4851dd2e5f68"
    vision_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_model: str = "qwen3-vl-plus-2025-12-19"
    vision_timeout_ms: int = 30000

    # Feature 5: real game API
    game_api_token: str = ""
    game_api_timeout_s: float = 5.0
    game_api_retry_max: int = 3

    @staticmethod
    def from_env() -> "GameCSConfig":
        dashscope_api_key = os.getenv("DASHSCOPE_API_KEY", "sk-c96d32af68ce4f85b03e4851dd2e5f68").strip()
        vision_enabled = _env_bool("GAME_CS_VISION_ENABLED", True)  # 移除 bool(dashscope_api_key) 检查
#bool(dashscope_api_key) and _env_bool("GAME_CS_VISION_ENABLED", True)

        return GameCSConfig(
            service_token=os.getenv("GAME_CS_SERVICE_TOKEN", "dev-token"),
            db_path=Path(os.getenv("GAME_CS_DB_PATH", ".nanobot/game_cs.db")),
            uploads_dir=Path(os.getenv("GAME_CS_UPLOADS_DIR", ".nanobot/game_cs_uploads")),
            openviking_path=Path(os.getenv("GAME_CS_OPENVIKING_PATH", ".nanobot/openviking_data")),
            openviking_target_uri=os.getenv(
                "GAME_CS_OPENVIKING_TARGET_URI",
                "viking://resources/game-cs/",
            ),
            max_image_bytes=int(os.getenv("GAME_CS_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))),
            default_game_name=os.getenv("GAME_CS_DEFAULT_GAME_NAME", "顽石英雄之大楚复古"),
            personality=os.getenv("GAME_CS_PERSONALITY", "lively"),
            game_api_base=os.getenv("GAME_CS_GAME_API_BASE", "").strip(),
            mock_api=_env_bool("GAME_CS_MOCK_API", True),
            code_daily_checkin=os.getenv("GAME_CS_CODE_DAILY_CHECKIN", "DCXXX"),
            code_lucky_draw=os.getenv("GAME_CS_CODE_LUCKY_DRAW", "TXYYY"),
            code_universal=os.getenv("GAME_CS_CODE_UNIVERSAL", "ws888"),
            code_guild=os.getenv("GAME_CS_CODE_GUILD", "FgYdqf6"),
            followup_30m_delay=int(os.getenv("GAME_CS_FOLLOWUP_30M_DELAY", "1800")),
            followup_1h_delay=int(os.getenv("GAME_CS_FOLLOWUP_1H_DELAY", "3600")),
            max_collect_retries=int(os.getenv("GAME_CS_MAX_COLLECT_RETRIES", "300")),
            ai_enabled=_env_bool("GAME_CS_AI_ENABLED", False),
            ai_timeout_ms=int(os.getenv("GAME_CS_AI_TIMEOUT_MS", "50000")),
            ai_max_context_msgs=int(os.getenv("GAME_CS_AI_MAX_CONTEXT_MSGS", "8")),
            ai_fallback_mode=os.getenv("GAME_CS_AI_FALLBACK_MODE", "best_effort"),
            kb_handoff_score_threshold=float(
                os.getenv("GAME_CS_KB_HANDOFF_SCORE_THRESHOLD", "0.45")
            ),
            ai_tool_whitelist=tuple(
                s.strip()
                for s in os.getenv("GAME_CS_AI_TOOL_WHITELIST", "").split(",")
                if s.strip()
            ),
            ai_info_extract_confidence_threshold=float(
                os.getenv("GAME_CS_AI_INFO_EXTRACT_CONFIDENCE_THRESHOLD", "0.7")
            ),
            intent_enabled=_env_bool("GAME_CS_INTENT_ENABLED", True),
            intent_model=os.getenv("GAME_CS_INTENT_MODEL", "qwen-turbo"),
            intent_api_key=dashscope_api_key,
            intent_api_base=os.getenv(
                "GAME_CS_INTENT_API_BASE",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            intent_timeout_ms=int(os.getenv("GAME_CS_INTENT_TIMEOUT_MS", "5000")),
            collecting_timeout_s=int(os.getenv("GAME_CS_COLLECTING_TIMEOUT_S", "300")),
            admin_gateway_url=os.getenv(
                "GAME_CS_ADMIN_GATEWAY_URL",
                "http://127.0.0.1:18790/message",
            ),
            admin_gateway_enabled=_env_bool("GAME_CS_ADMIN_GATEWAY_ENABLED", True),
            admin_gateway_token=os.getenv("GAME_CS_ADMIN_GATEWAY_TOKEN", "").strip(),
            admin_query_timeout_s=int(os.getenv("GAME_CS_ADMIN_QUERY_TIMEOUT_S", "600")),
            vision_enabled=vision_enabled,
            vision_api_key=dashscope_api_key,
            vision_api_base=os.getenv(
                "GAME_CS_VISION_API_BASE",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            vision_model=os.getenv("GAME_CS_VISION_MODEL", "qwen3-vl-plus"),
            vision_timeout_ms=int(os.getenv("GAME_CS_VISION_TIMEOUT_MS", "30000")),
            game_api_token=os.getenv("GAME_CS_GAME_API_TOKEN", "").strip(),
            game_api_timeout_s=float(os.getenv("GAME_CS_GAME_API_TIMEOUT_S", "5.0")),
            game_api_retry_max=int(os.getenv("GAME_CS_GAME_API_RETRY_MAX", "3")),
        )
