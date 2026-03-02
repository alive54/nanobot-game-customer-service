from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GameCSConfig:
    service_token: str
    db_path: Path
    uploads_dir: Path
    openviking_path: Path
    openviking_target_uri: str
    bind_steps: list[str]

    @staticmethod
    def from_env() -> "GameCSConfig":
        raw_steps = os.getenv(
            "GAME_CS_BIND_STEPS",
            "发送截图|发送游戏UID|发送游戏区服|确认绑定",
        )
        steps = [x.strip() for x in raw_steps.split("|") if x.strip()]
        return GameCSConfig(
            service_token=os.getenv("GAME_CS_SERVICE_TOKEN", "dev-token"),
            db_path=Path(os.getenv("GAME_CS_DB_PATH", ".nanobot/game_cs.db")),
            uploads_dir=Path(os.getenv("GAME_CS_UPLOADS_DIR", ".nanobot/game_cs_uploads")),
            openviking_path=Path(os.getenv("GAME_CS_OPENVIKING_PATH", ".nanobot/openviking_data")),
            openviking_target_uri=os.getenv("GAME_CS_OPENVIKING_TARGET_URI", "viking://resources/"),
            bind_steps=steps,
        )
