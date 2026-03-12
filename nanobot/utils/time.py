"""Project-wide time helpers pinned to Beijing time."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE_NAME = "Asia/Shanghai"
BEIJING_TZ = ZoneInfo(DEFAULT_TIMEZONE_NAME)


def ensure_beijing_timezone(value: datetime) -> datetime:
    """Normalize datetimes to Asia/Shanghai."""
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)


def now_datetime() -> datetime:
    """Return the current Beijing time as an aware datetime."""
    return datetime.now(BEIJING_TZ)


def now_iso() -> str:
    """Return the current Beijing time in ISO-8601 format."""
    return now_datetime().strftime("%Y-%m-%d %H:%M:%S")


def now_compact_timestamp() -> str:
    """Return a compact timestamp suitable for filenames."""
    return now_datetime().strftime("%Y%m%d_%H%M%S_%f")


def parse_datetime(value: str | None) -> datetime | None:
    """Parse legacy and current ISO strings and normalize to Beijing time."""
    if not value:
        return None

    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    parsed = datetime.fromisoformat(normalized)
    return ensure_beijing_timezone(parsed)
