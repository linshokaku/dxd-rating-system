from datetime import datetime, timezone

from dxd_rating.contexts.restrictions.domain import (
    PlayerAccessRestrictionDuration,
    build_access_restriction_expires_at,
    normalize_access_restriction_reason,
)


def test_build_access_restriction_expires_at_handles_permanent_and_timed() -> None:
    current_time = datetime(2026, 3, 22, 15, 0, tzinfo=timezone.utc)

    assert (
        build_access_restriction_expires_at(
            current_time=current_time,
            duration=PlayerAccessRestrictionDuration.PERMANENT,
        )
        is None
    )
    assert build_access_restriction_expires_at(
        current_time=current_time,
        duration=PlayerAccessRestrictionDuration.ONE_DAY,
    ) == datetime(2026, 3, 23, 15, 0, tzinfo=timezone.utc)


def test_normalize_access_restriction_reason_trims_blank_values() -> None:
    assert normalize_access_restriction_reason("  spam  ") == "spam"
    assert normalize_access_restriction_reason("   ") is None
    assert normalize_access_restriction_reason(None) is None
