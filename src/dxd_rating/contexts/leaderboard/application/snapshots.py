from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from dxd_rating.contexts.seasons.application import (
    get_active_and_upcoming_seasons,
    get_database_now,
)
from dxd_rating.platform.db.models import LeaderboardSnapshot, MatchFormat, PlayerFormatStats
from dxd_rating.shared.constants import get_match_format_definitions

JST = ZoneInfo("Asia/Tokyo")
SNAPSHOT_RETENTION_DAYS = 180


@dataclass(frozen=True, slots=True)
class LeaderboardSnapshotMaintenanceResult:
    snapshot_date: date
    season_id: int
    created_count: int
    deleted_count: int
    skipped_creation: bool


def maintain_leaderboard_snapshots(
    session: Session,
    *,
    current_time: datetime | None = None,
) -> LeaderboardSnapshotMaintenanceResult:
    session.flush()
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    active_season = get_active_and_upcoming_seasons(
        session,
        current_time=resolved_current_time,
    ).active
    snapshot_date = resolve_snapshot_date(resolved_current_time)

    skipped_creation = leaderboard_snapshot_exists(
        session,
        snapshot_date=snapshot_date,
        season_id=active_season.id,
    )
    created_count = 0
    if not skipped_creation:
        created_count = create_leaderboard_snapshots(
            session,
            snapshot_date=snapshot_date,
            season_id=active_season.id,
        )
        session.flush()

    deleted_count = delete_expired_leaderboard_snapshots(
        session,
        current_snapshot_date=snapshot_date,
    )
    return LeaderboardSnapshotMaintenanceResult(
        snapshot_date=snapshot_date,
        season_id=active_season.id,
        created_count=created_count,
        deleted_count=deleted_count,
        skipped_creation=skipped_creation,
    )


def resolve_snapshot_date(current_time: datetime) -> date:
    return current_time.astimezone(JST).date()


def leaderboard_snapshot_exists(
    session: Session,
    *,
    snapshot_date: date,
    season_id: int,
) -> bool:
    return bool(
        session.scalar(
            select(
                exists().where(
                    LeaderboardSnapshot.snapshot_date == snapshot_date,
                    LeaderboardSnapshot.season_id == season_id,
                )
            )
        )
    )


def create_leaderboard_snapshots(
    session: Session,
    *,
    snapshot_date: date,
    season_id: int,
) -> int:
    snapshots: list[LeaderboardSnapshot] = []
    for format_definition in get_match_format_definitions():
        snapshots.extend(
            _build_leaderboard_snapshots_for_format(
                session,
                snapshot_date=snapshot_date,
                season_id=season_id,
                match_format=format_definition.match_format,
            )
        )

    if snapshots:
        session.add_all(snapshots)

    return len(snapshots)


def delete_expired_leaderboard_snapshots(
    session: Session,
    *,
    current_snapshot_date: date,
) -> int:
    oldest_retained_snapshot_date = current_snapshot_date - timedelta(
        days=SNAPSHOT_RETENTION_DAYS - 1
    )
    deleted_snapshot_dates = session.scalars(
        delete(LeaderboardSnapshot)
        .where(LeaderboardSnapshot.snapshot_date < oldest_retained_snapshot_date)
        .returning(LeaderboardSnapshot.snapshot_date)
    ).all()
    return len(deleted_snapshot_dates)


def _build_leaderboard_snapshots_for_format(
    session: Session,
    *,
    snapshot_date: date,
    season_id: int,
    match_format: MatchFormat,
) -> list[LeaderboardSnapshot]:
    format_stats = session.scalars(
        select(PlayerFormatStats)
        .where(
            PlayerFormatStats.season_id == season_id,
            PlayerFormatStats.match_format == match_format,
            PlayerFormatStats.games_played > 0,
        )
        .order_by(
            PlayerFormatStats.rating.desc(),
            PlayerFormatStats.games_played.desc(),
            PlayerFormatStats.player_id.asc(),
        )
    ).all()

    return [
        LeaderboardSnapshot(
            snapshot_date=snapshot_date,
            season_id=season_id,
            match_format=match_format,
            player_id=format_stats_row.player_id,
            rank=rank,
            rating=format_stats_row.rating,
            games_played=format_stats_row.games_played,
        )
        for rank, format_stats_row in enumerate(format_stats, start=1)
    ]
