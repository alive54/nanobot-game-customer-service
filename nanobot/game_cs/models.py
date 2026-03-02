from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GameMessageIn(BaseModel):
    user_id: str = Field(..., description="Unique player id in game platform")
    message: str = Field("", description="Player text message")
    screenshot_b64: str | None = Field(None, description="Optional base64 screenshot")
    screenshot_ext: str = Field("png", description="Screenshot extension")
    screenshot_url: str | None = Field(None, description="Optional screenshot URL")
    metadata: dict[str, str] = Field(default_factory=dict)


class GameReply(BaseModel):
    status: Literal["ok", "error"]
    reply: str
    next_step: str | None = None
    bound: bool = False
    timestamp: datetime
