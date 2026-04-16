from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
DISCORD_DATETIME_FORMAT = "%Y/%m/%d %H:%M:%S JST"


def format_discord_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be timezone-aware")
    return value.astimezone(JST).strftime(DISCORD_DATETIME_FORMAT)
