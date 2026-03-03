from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PersonalityType(str, Enum):
    LIVELY = "lively"           # 活泼热情型 (小楚) — 默认
    PROFESSIONAL = "professional"  # 专业严谨型 (阿复)
    STEADY = "steady"           # 沉稳可靠型 (古哥)
    HUMOROUS = "humorous"       # 幽默风趣型 (顽石)


class SOPState(str, Enum):
    GREETING = "greeting"                   # 开场白
    COLLECTING_INFO = "collecting_info"     # 信息收集
    VALIDATING = "validating"               # 信息验证
    BINDING = "binding"                     # 用户绑定
    SENDING_CODE = "sending_code"           # 发送兑换码
    FOLLOW_UP_PENDING = "follow_up_pending" # 等待回访定时器
    FOLLOW_UP_30MIN = "follow_up_30min"     # 30分钟回访
    FOLLOW_UP_1HOUR = "follow_up_1hour"     # 1小时回访（裂变）
    SILENT = "silent"                       # 沉默期
    NEXT_DAY_VISIT = "next_day_visit"       # 次日回访
    REACTIVATION = "reactivation"           # 沉默用户激活
    COMPLETED = "completed"                 # 流程结束
    EXCEPTION = "exception"                 # 异常处理


class GameMessageIn(BaseModel):
    user_id: str = Field(..., description="Unique player id in the messaging platform")
    message: str = Field("", description="Player text message")
    screenshot_b64: str | None = Field(None, description="Optional base64 screenshot for OCR assist")
    screenshot_ext: str = Field("png", description="Screenshot extension: png/jpg/jpeg/webp")
    screenshot_url: str | None = Field(None, description="Optional screenshot URL for OCR assist")
    metadata: dict[str, str] = Field(default_factory=dict)


class GameReply(BaseModel):
    status: Literal["ok", "error"]
    reply: str
    sop_state: str
    next_step: str | None = None
    bound: bool = False
    codes: dict[str, str] | None = None
    timestamp: datetime


class ParsedInfo(BaseModel):
    """Extracted game-role information from a user message."""
    game_name: str | None = None
    area_name: str | None = None
    role_name: str | None = None
    confidence: float = 0.0


class DailyCodes(BaseModel):
    """The four code types sent after successful binding."""
    daily_checkin: str = Field(..., description="每日打卡码")
    lucky_draw: str = Field(..., description="天选码")
    universal: str = Field(..., description="通码")
    guild: str = Field(..., description="供宗号")
