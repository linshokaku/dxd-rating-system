from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import (
    InvalidLeaderboardPageError,
    InvalidMatchFormatError,
    LeaderboardPageNotFoundError,
    SeasonNotFoundError,
    SeasonStateError,
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
    Season,
)
from dxd_rating.platform.db.session import session_scope

LEADERBOARD_PAGE_SIZE = 20
SEASON_TOP_RANKING_LIMIT = 12
INVALID_MATCH_FORMAT_MESSAGE = "指定したフォーマットは存在しません。"
INVALID_LEADERBOARD_PAGE_MESSAGE = "page は 1 以上で指定してください。"
LEADERBOARD_PAGE_NOT_FOUND_MESSAGE = "指定したページにはランキングがありません。"
SEASON_NOT_STARTED_MESSAGE = "指定したシーズンはまだ開始していません。"
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
    has_next_page: bool
    entries: tuple[CurrentLeaderboardEntry, ...]


@dataclass(frozen=True, slots=True)
class SeasonLeaderboardEntry:
    rank: int
    display_name: str
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int


@dataclass(frozen=True, slots=True)
class SeasonLeaderboardPage:
    season_id: int
    season_name: str
    match_format: MatchFormat
    page: int
    page_size: int
    has_next_page: bool
    entries: tuple[SeasonLeaderboardEntry, ...]


@dataclass(frozen=True, slots=True)
class SeasonTopRankings:
    season_id: int
    season_name: str
    match_format: MatchFormat
    entries: tuple[SeasonLeaderboardEntry, ...]


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

    def get_season_leaderboard_page(
        self,
        season_id: int,
        match_format: MatchFormat | str,
        page: int,
    ) -> SeasonLeaderboardPage:
        with session_scope(self.session_factory) as session:
            return get_season_leaderboard_page(
                session,
                season_id=season_id,
                match_format=match_format,
                page=page,
            )

    def get_season_top_rankings(
        self,
        season_id: int,
        match_format: MatchFormat | str,
        *,
        limit: int = SEASON_TOP_RANKING_LIMIT,
    ) -> SeasonTopRankings:
        with session_scope(self.session_factory) as session:
            return get_season_top_rankings(
                session,
                season_id=season_id,
                match_format=match_format,
                limit=limit,
            )


def get_current_leaderboard_page(
    session: Session,
    *,
    match_format: MatchFormat | str,
    page: int,
    current_time: datetime | None = None,
) -> CurrentLeaderboardPage:
    if page < 1:
        raise InvalidLeaderboardPageError(INVALID_LEADERBOARD_PAGE_MESSAGE)

    resolved_current_time = get_database_now(session) if current_time is None else current_time
    resolved_match_format = _resolve_match_format(match_format)
    active_season = get_active_and_upcoming_seasons(
        session,
        current_time=resolved_current_time,
    ).active
    page_offset, leaderboard_rows, has_next_page = _load_current_leaderboard_rows(
        session,
        season_id=active_season.id,
        match_format=resolved_match_format,
        page=page,
    )

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
        has_next_page=has_next_page,
        entries=entries,
    )


def get_season_leaderboard_page(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat | str,
    page: int,
    current_time: datetime | None = None,
) -> SeasonLeaderboardPage:
    if page < 1:
        raise InvalidLeaderboardPageError(INVALID_LEADERBOARD_PAGE_MESSAGE)

    resolved_current_time = get_database_now(session) if current_time is None else current_time
    resolved_match_format = _resolve_match_format(match_format)
    season = _resolve_started_season(
        session,
        season_id=season_id,
        current_time=resolved_current_time,
    )
    page_offset, leaderboard_rows, has_next_page = _load_season_leaderboard_rows(
        session,
        season_id=season.id,
        match_format=resolved_match_format,
        page=page,
    )

    entries = tuple(
        SeasonLeaderboardEntry(
            rank=page_offset + index,
            display_name=_resolve_display_name(player),
            rating=format_stats.rating,
            games_played=format_stats.games_played,
            wins=format_stats.wins,
            losses=format_stats.losses,
            draws=format_stats.draws,
        )
        for index, (format_stats, player) in enumerate(leaderboard_rows, start=1)
    )
    return SeasonLeaderboardPage(
        season_id=season.id,
        season_name=season.name,
        match_format=resolved_match_format,
        page=page,
        page_size=LEADERBOARD_PAGE_SIZE,
        has_next_page=has_next_page,
        entries=entries,
    )


def get_season_top_rankings(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat | str,
    limit: int = SEASON_TOP_RANKING_LIMIT,
    current_time: datetime | None = None,
) -> SeasonTopRankings:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    resolved_match_format = _resolve_match_format(match_format)
    season = _resolve_started_season(
        session,
        season_id=season_id,
        current_time=resolved_current_time,
    )
    leaderboard_rows = _query_leaderboard_rows(
        session,
        season_id=season.id,
        match_format=resolved_match_format,
        limit=limit,
    )
    return SeasonTopRankings(
        season_id=season.id,
        season_name=season.name,
        match_format=resolved_match_format,
        entries=_build_season_leaderboard_entries(leaderboard_rows, page_offset=0),
    )


def _resolve_match_format(match_format: MatchFormat | str) -> MatchFormat:
    try:
        if isinstance(match_format, MatchFormat):
            return match_format
        return MatchFormat(match_format)
    except ValueError as exc:
        raise InvalidMatchFormatError(INVALID_MATCH_FORMAT_MESSAGE) from exc


def _resolve_started_season(
    session: Session,
    *,
    season_id: int,
    current_time: datetime,
) -> Season:
    season = session.get(Season, season_id)
    if season is None:
        raise SeasonNotFoundError("指定したシーズンが見つかりません。")
    if season.start_at > current_time:
        raise SeasonStateError(SEASON_NOT_STARTED_MESSAGE)
    return season


def _load_season_leaderboard_rows(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat,
    page: int,
) -> tuple[int, list[tuple[PlayerFormatStats, Player]], bool]:
    if page < 1:
        raise InvalidLeaderboardPageError(INVALID_LEADERBOARD_PAGE_MESSAGE)

    page_offset = (page - 1) * LEADERBOARD_PAGE_SIZE
    leaderboard_rows = _query_leaderboard_rows(
        session,
        season_id=season_id,
        match_format=match_format,
        offset=page_offset,
        limit=LEADERBOARD_PAGE_SIZE + 1,
    )
    if not leaderboard_rows:
        raise LeaderboardPageNotFoundError(LEADERBOARD_PAGE_NOT_FOUND_MESSAGE)

    has_next_page = len(leaderboard_rows) > LEADERBOARD_PAGE_SIZE
    if has_next_page:
        leaderboard_rows = leaderboard_rows[:LEADERBOARD_PAGE_SIZE]

    return page_offset, leaderboard_rows, has_next_page


def _load_current_leaderboard_rows(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat,
    page: int,
) -> tuple[int, list[tuple[PlayerFormatStats, Player]], bool]:
    if page < 1:
        raise InvalidLeaderboardPageError(INVALID_LEADERBOARD_PAGE_MESSAGE)

    page_offset = (page - 1) * LEADERBOARD_PAGE_SIZE
    leaderboard_rows = _query_leaderboard_rows(
        session,
        season_id=season_id,
        match_format=match_format,
        offset=page_offset,
        limit=LEADERBOARD_PAGE_SIZE + 1,
    )
    if not leaderboard_rows:
        raise LeaderboardPageNotFoundError(LEADERBOARD_PAGE_NOT_FOUND_MESSAGE)

    has_next_page = len(leaderboard_rows) > LEADERBOARD_PAGE_SIZE
    if has_next_page:
        leaderboard_rows = leaderboard_rows[:LEADERBOARD_PAGE_SIZE]

    return page_offset, leaderboard_rows, has_next_page


def _query_leaderboard_rows(
    session: Session,
    *,
    season_id: int,
    match_format: MatchFormat,
    limit: int,
    offset: int = 0,
) -> list[tuple[PlayerFormatStats, Player]]:
    return list(
        session.execute(
            select(PlayerFormatStats, Player)
            .join(Player, Player.id == PlayerFormatStats.player_id)
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
            .offset(offset)
            .limit(limit)
        )
        .tuples()
        .all()
    )


def _build_season_leaderboard_entries(
    leaderboard_rows: list[tuple[PlayerFormatStats, Player]],
    *,
    page_offset: int,
) -> tuple[SeasonLeaderboardEntry, ...]:
    return tuple(
        SeasonLeaderboardEntry(
            rank=page_offset + index,
            display_name=_resolve_display_name(player),
            rating=format_stats.rating,
            games_played=format_stats.games_played,
            wins=format_stats.wins,
            losses=format_stats.losses,
            draws=format_stats.draws,
        )
        for index, (format_stats, player) in enumerate(leaderboard_rows, start=1)
    )


def _resolve_display_name(player: Player) -> str:
    return format_player_display_name(
        discord_user_id=player.discord_user_id,
        display_name=player.display_name,
    )


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
