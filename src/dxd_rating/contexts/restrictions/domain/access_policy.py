from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class PlayerAccessRestrictionDuration(StrEnum):
    ONE_DAY = "1d"
    THREE_DAYS = "3d"
    SEVEN_DAYS = "7d"
    FOURTEEN_DAYS = "14d"
    TWENTY_EIGHT_DAYS = "28d"
    FIFTY_SIX_DAYS = "56d"
    EIGHTY_FOUR_DAYS = "84d"
    PERMANENT = "permanent"


_RESTRICTION_DURATION_DELTAS = {
    PlayerAccessRestrictionDuration.ONE_DAY: timedelta(days=1),
    PlayerAccessRestrictionDuration.THREE_DAYS: timedelta(days=3),
    PlayerAccessRestrictionDuration.SEVEN_DAYS: timedelta(days=7),
    PlayerAccessRestrictionDuration.FOURTEEN_DAYS: timedelta(days=14),
    PlayerAccessRestrictionDuration.TWENTY_EIGHT_DAYS: timedelta(days=28),
    PlayerAccessRestrictionDuration.FIFTY_SIX_DAYS: timedelta(days=56),
    PlayerAccessRestrictionDuration.EIGHTY_FOUR_DAYS: timedelta(days=84),
}


def build_access_restriction_expires_at(
    *,
    current_time: datetime,
    duration: PlayerAccessRestrictionDuration,
) -> datetime | None:
    if duration == PlayerAccessRestrictionDuration.PERMANENT:
        return None
    return current_time + _RESTRICTION_DURATION_DELTAS[duration]


def normalize_access_restriction_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    normalized_reason = reason.strip()
    return normalized_reason or None
