from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import (
    InvalidLeaderboardPageError,
    InvalidMatchFormatError,
    LeaderboardPageNotFoundError,
)
from dxd_rating.contexts.leaderboard.application.snapshots import resolve_snapshot_date
from dxd_rating.contexts.players.domain import format_player_display_name
from dxd_rating.contexts.seasons.application import (
    get_active_and_upcoming_seasons,
    get_database_now,
)
from dxd_rating.platform.db.models import (
    LeaderboardSnapshot,
    MatchFormat,
    Player,
    PlayerFormatStats,
)
from dxd_rating.platform.db.session import session_scope

LEADERBOARD_PAGE_SIZE = 20
INVALID_MATCH_FORMAT_MESSAGE = "指定したフォーマットは存在しません。"
INVALID_LEADERBOARD_PAGE_MESSAGE = "page は 1 以上で指定してください。"
LEADERBOARD_PAGE_NOT_FOUND_MESSAGE = "指定したページにはランキングがありません。"
RANK_CHANGE_SNAPSHOT_DAY_OFFSETS = (1, 3, 7)


@dataclass(frozen=True, slots=True)
class CurrentLeaderboardEntry:
    rank: int
    display_name: str
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int
    rank_change_1d: int | None
    rank_change_3d: int | None
    rank_change_7d: int | None


@dataclass(frozen=True, slots=True)
class CurrentLeaderboardPage:
    season_name: str
    season_id: int
    match_format: MatchFormat
    page: int
    page_size: int
    entries: tuple[CurrentLeaderboardEntry, ...]


class LeaderboardService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def get_current_leaderboard_page(
        self,
        match_format: MatchFormat | str,
        page: int,
    ) -> CurrentLeaderboardPage:
        with session_scope(self.session_factory) as session:
            return get_current_leaderboard_page(session, match_format=match_format, page=page)


def get_current_leaderboard_page(
    session: Session,
    *,
    match_format: MatchFormat | str,
    page: int,
    current_time: datetime | None = None,
) -> CurrentLeaderboardPage:
    if page < 1:
        raise InvalidLeaderboardPageError(INVALID_LEADERBOARD_PAGE_MESSAGE)

    resolved_match_format = _resolve_match_format(match_format)
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    active_season = get_active_and_upcoming_seasons(
        session,
        current_time=resolved_current_time,
    ).active
    page_offset = (page - 1) * LEADERBOARD_PAGE_SIZE

    leaderboard_rows = session.execute(
        select(PlayerFormatStats, Player)
        .join(Player, Player.id == PlayerFormatStats.player_id)
        .where(
            PlayerFormatStats.season_id == active_season.id,
            PlayerFormatStats.match_format == resolved_match_format,
            PlayerFormatStats.games_played > 0,
        )
        .order_by(
            PlayerFormatStats.rating.desc(),
            PlayerFormatStats.games_played.desc(),
            PlayerFormatStats.player_id.asc(),
        )
        .offset(page_offset)
        .limit(LEADERBOARD_PAGE_SIZE)
    ).all()

    if not leaderboard_rows:
        raise LeaderboardPageNotFoundError(LEADERBOARD_PAGE_NOT_FOUND_MESSAGE)

    player_ids = tuple(format_stats.player_id for format_stats, _ in leaderboard_rows)
    snapshot_ranks_by_key = _load_snapshot_ranks_by_key(
        session,
        season_id=active_season.id,
        match_format=resolved_match_format,
        player_ids=player_ids,
        current_time=resolved_current_time,
    )
    current_snapshot_date = resolve_snapshot_date(resolved_current_time)

    entries = tuple(
        CurrentLeaderboardEntry(
            rank=page_offset + index,
            display_name=format_player_display_name(
                discord_user_id=player.discord_user_id,
                display_name=player.display_name,
            ),
            rating=format_stats.rating,
            games_played=format_stats.games_played,
            wins=format_stats.wins,
            losses=format_stats.losses,
            draws=format_stats.draws,
            rank_change_1d=_calculate_rank_change(
                current_rank=page_offset + index,
                past_rank=snapshot_ranks_by_key.get(
                    (current_snapshot_date - timedelta(days=1), format_stats.player_id)
                ),
            ),
            rank_change_3d=_calculate_rank_change(
                current_rank=page_offset + index,
                past_rank=snapshot_ranks_by_key.get(
                    (current_snapshot_date - timedelta(days=3), format_stats.player_id)
                ),
            ),
            rank_change_7d=_calculate_rank_change(
                current_rank=page_offset + index,
                past_rank=snapshot_ranks_by_key.get(
                    (current_snapshot_date - timedelta(days=7), format_stats.player_id)
                ),
            ),
        )
        for index, (format_stats, player) in enumerate(leaderboard_rows, start=1)
    )
    return CurrentLeaderboardPage(
        season_name=active_season.name,
        season_id=active_season.id,
        match_format=resolved_match_format,
        page=page,
        page_size=LEADERBOARD_PAGE_SIZE,
        entries=entries,
    )


def _resolve_match_format(match_format: MatchFormat | str) -> MatchFormat:
    try:
        if isinstance(match_format, MatchFormat):
            return match_format
        return MatchFormat(match_format)
    except ValueError as exc:
        raise InvalidMatchFormatError(INVALID_MATCH_FORMAT_MESSAGE) from exc


def _load_snapshot_ranks_by_key(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat,
    player_ids: tuple[int, ...],
    current_time: datetime,
) -> dict[tuple[date, int], int]:
    current_snapshot_date = resolve_snapshot_date(current_time)
    snapshot_dates = tuple(
        current_snapshot_date - timedelta(days=day_offset)
        for day_offset in RANK_CHANGE_SNAPSHOT_DAY_OFFSETS
    )
    rows = session.scalars(
        select(LeaderboardSnapshot).where(
            LeaderboardSnapshot.snapshot_date.in_(snapshot_dates),
            LeaderboardSnapshot.season_id == season_id,
            LeaderboardSnapshot.match_format == match_format,
            LeaderboardSnapshot.player_id.in_(player_ids),
        )
    ).all()
    return {(row.snapshot_date, row.player_id): row.rank for row in rows}


def _calculate_rank_change(*, current_rank: int, past_rank: int | None) -> int | None:
    if past_rank is None:
        return None
    return past_rank - current_rank
