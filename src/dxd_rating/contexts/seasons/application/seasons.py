from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application.errors import (
    InvalidSeasonNameRequiredError,
    PlayerNotRegisteredError,
    PlayerSeasonStatsNotFoundError,
    SeasonAlreadyExistsError,
    SeasonNameLeadingDigitError,
    SeasonNameTooLongError,
    SeasonNotFoundError,
    SeasonStateError,
)
from dxd_rating.contexts.players.domain import format_player_display_name
from dxd_rating.contexts.ui.application import (
    SeasonTopRankingEntryPayload,
    SeasonTopRankingsNotification,
    enqueue_season_completion_notifications,
)
from dxd_rating.platform.db.models import (
    INITIAL_RATING,
    ActiveMatchState,
    CarryoverStatus,
    Match,
    MatchFormat,
    MatchState,
    Player,
    PlayerFormatStats,
    Season,
)
from dxd_rating.platform.db.session import session_scope
from dxd_rating.shared.constants import get_match_format_definitions

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc
SEASON_BOUNDARY_DAY = 14
AUTO_SEASON_NAME_SUFFIX = "delta"
MIN_CARRYOVER_GAMES = 5
CARRYOVER_FACTOR = Decimal("0.35")
CARRYOVER_MAX_RATING = 1750
SEASON_NAME_MAX_LENGTH = 64
SEASON_RENAME_PATTERN = re.compile(r"^[^\d].*$")


@dataclass(frozen=True, slots=True)
class SeasonInfo:
    season_id: int
    name: str
    start_at: datetime
    end_at: datetime
    completed: bool
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SeasonRenameResult:
    season_id: int
    name: str


@dataclass(frozen=True, slots=True)
class ActiveSeasonPair:
    active: Season
    upcoming: Season


@dataclass(frozen=True, slots=True)
class ForceEndSeasonResult:
    active_season_id: int
    upcoming_season_id: int
    forced_at: datetime
    previous_active_end_at: datetime
    previous_upcoming_start_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerFormatSeasonInfo:
    match_format: MatchFormat
    rating: float
    games_played: int
    wins: int
    losses: int
    draws: int
    last_played_at: datetime | None
    carryover_status: CarryoverStatus


@dataclass(frozen=True, slots=True)
class PlayerSeasonInfo:
    player_id: int
    discord_user_id: int
    display_name: str | None
    display_name_updated_at: datetime | None
    last_seen_at: datetime | None
    season: SeasonInfo
    format_stats: tuple[PlayerFormatSeasonInfo, ...]

    @property
    def resolved_display_name(self) -> str:
        return format_player_display_name(
            discord_user_id=self.discord_user_id,
            display_name=self.display_name,
        )


class SeasonService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def list_started_seasons(
        self,
        *,
        current_time: datetime | None = None,
        limit: int | None = None,
    ) -> tuple[SeasonInfo, ...]:
        with session_scope(self.session_factory) as session:
            return list_started_seasons(
                session,
                current_time=current_time,
                limit=limit,
            )

    def rename_season(self, season_id: int, name: str) -> SeasonRenameResult:
        with session_scope(self.session_factory) as session:
            season = session.get(Season, season_id)
            if season is None:
                raise SeasonNotFoundError()

            validated_name = validate_admin_season_name(name)
            duplicate_exists = session.scalar(
                select(
                    exists().where(
                        Season.id != season_id,
                        Season.name == validated_name,
                    )
                )
            )
            if duplicate_exists:
                raise SeasonAlreadyExistsError()

            season.name = validated_name
            session.flush()
            return SeasonRenameResult(season_id=season.id, name=season.name)


def build_auto_season_name(start_at: datetime) -> str:
    return f"{start_at.astimezone(JST):%Y%m}{AUTO_SEASON_NAME_SUFFIX}"


def validate_admin_season_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise InvalidSeasonNameRequiredError()
    if len(normalized) > SEASON_NAME_MAX_LENGTH:
        raise SeasonNameTooLongError()
    if SEASON_RENAME_PATTERN.fullmatch(normalized) is None:
        raise SeasonNameLeadingDigitError()
    return normalized


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def ensure_active_and_upcoming_seasons(
    session: Session,
    *,
    current_time: datetime | None = None,
) -> ActiveSeasonPair:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    active_season = _get_active_season(session, resolved_current_time)

    if active_season is None:
        latest_season = session.scalar(
            select(Season).order_by(Season.start_at.desc(), Season.id.desc())
        )
        if latest_season is None:
            active_start_at, active_end_at = resolve_active_season_window(resolved_current_time)
            active_season = _create_season(
                session,
                start_at=active_start_at,
                end_at=active_end_at,
            )
        else:
            active_season = latest_season
            while active_season.end_at <= resolved_current_time:
                active_season = _ensure_following_season(session, active_season)

    upcoming_season = _ensure_following_season(session, active_season)
    return ActiveSeasonPair(active=active_season, upcoming=upcoming_season)


def get_active_and_upcoming_seasons(
    session: Session,
    *,
    current_time: datetime | None = None,
) -> ActiveSeasonPair:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    active_season = _get_active_season(session, resolved_current_time)
    if active_season is None:
        raise SeasonStateError("現在時刻に対応する稼働中シーズンが存在しません。")

    upcoming_season = session.scalar(
        select(Season).where(Season.start_at == active_season.end_at).order_by(Season.id)
    )
    if upcoming_season is None:
        raise SeasonStateError("稼働中シーズンに対応する次シーズンが存在しません。")

    return ActiveSeasonPair(active=active_season, upcoming=upcoming_season)


def force_end_active_season(
    session: Session,
    *,
    current_time: datetime | None = None,
) -> ForceEndSeasonResult:
    forced_at = get_database_now(session) if current_time is None else current_time
    season_pair = get_active_and_upcoming_seasons(session, current_time=forced_at)
    if forced_at <= season_pair.active.start_at:
        raise SeasonStateError("稼働中シーズンの開始時刻以前には強制終了できません。")

    previous_active_end_at = season_pair.active.end_at
    previous_upcoming_start_at = season_pair.upcoming.start_at
    season_pair.active.end_at = forced_at
    season_pair.upcoming.start_at = forced_at
    session.flush()

    return ForceEndSeasonResult(
        active_season_id=season_pair.active.id,
        upcoming_season_id=season_pair.upcoming.id,
        forced_at=forced_at,
        previous_active_end_at=previous_active_end_at,
        previous_upcoming_start_at=previous_upcoming_start_at,
    )


def list_started_seasons(
    session: Session,
    *,
    current_time: datetime | None = None,
    limit: int | None = None,
) -> tuple[SeasonInfo, ...]:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    statement = (
        select(Season)
        .where(Season.start_at <= resolved_current_time)
        .order_by(Season.start_at.desc(), Season.id.desc())
    )
    if limit is not None:
        statement = statement.limit(limit)

    seasons = session.scalars(statement).all()
    return tuple(
        SeasonInfo(
            season_id=season.id,
            name=season.name,
            start_at=season.start_at,
            end_at=season.end_at,
            completed=season.completed,
            completed_at=season.completed_at,
        )
        for season in seasons
    )


def ensure_player_stats_for_current_and_future_seasons(
    session: Session,
    *,
    player_id: int,
    current_time: datetime | None = None,
) -> None:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    get_active_and_upcoming_seasons(session, current_time=resolved_current_time)
    seasons = session.scalars(
        select(Season)
        .where(Season.end_at > resolved_current_time)
        .order_by(Season.start_at, Season.id)
    ).all()
    _ensure_player_format_stats_rows(
        session,
        player_ids=(player_id,),
        seasons=tuple(seasons),
    )
    session.flush()


def resolve_player_format_stats_for_season(
    session: Session,
    *,
    player_ids: tuple[int, ...],
    season_id: int,
    match_format: MatchFormat,
    lock_rows: bool = False,
) -> dict[int, PlayerFormatStats]:
    if not player_ids:
        return {}

    season = session.get(Season, season_id)
    if season is None:
        raise SeasonNotFoundError()

    _ensure_player_format_stats_rows(
        session,
        player_ids=player_ids,
        seasons=(season,),
    )

    statement = (
        select(PlayerFormatStats)
        .where(
            PlayerFormatStats.player_id.in_(player_ids),
            PlayerFormatStats.season_id == season_id,
            PlayerFormatStats.match_format == match_format,
        )
        .order_by(PlayerFormatStats.player_id)
    )
    if lock_rows:
        statement = statement.with_for_update()

    rows = session.scalars(statement).all()
    rows_by_player_id = {row.player_id: row for row in rows}
    missing_player_ids = sorted(set(player_ids) - set(rows_by_player_id))
    if missing_player_ids:
        raise PlayerSeasonStatsNotFoundError(
            "プレイヤー統計が見つかりません。"
            f" season_id={season_id}"
            f" match_format={match_format.value}"
            f" player_ids={missing_player_ids}"
        )

    pending_rows = [row for row in rows if row.carryover_status == CarryoverStatus.PENDING]
    if pending_rows:
        previous_season = _get_previous_season(session, season)
        previous_stats_by_player_id = _get_previous_stats_by_player_id(
            session,
            previous_season=previous_season,
            player_ids=tuple(row.player_id for row in pending_rows),
            match_format=match_format,
        )
        for row in pending_rows:
            _apply_carryover_to_row(
                row,
                previous_season=previous_season,
                previous_stats=previous_stats_by_player_id.get(row.player_id),
            )
        session.flush()

    return rows_by_player_id


def get_current_player_season_info_by_discord_user_id(
    session: Session,
    *,
    discord_user_id: int,
    current_time: datetime | None = None,
) -> PlayerSeasonInfo:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    active_season = get_active_and_upcoming_seasons(
        session,
        current_time=resolved_current_time,
    ).active
    return get_player_season_info_by_discord_user_id(
        session,
        discord_user_id=discord_user_id,
        season_id=active_season.id,
    )


def get_player_season_info_by_discord_user_id(
    session: Session,
    *,
    discord_user_id: int,
    season_id: int,
) -> PlayerSeasonInfo:
    player = session.scalar(select(Player).where(Player.discord_user_id == discord_user_id))
    if player is None:
        raise PlayerNotRegisteredError()

    season = session.get(Season, season_id)
    if season is None:
        raise SeasonNotFoundError()

    rows = session.scalars(
        select(PlayerFormatStats)
        .where(
            PlayerFormatStats.player_id == player.id,
            PlayerFormatStats.season_id == season.id,
        )
        .order_by(PlayerFormatStats.match_format)
    ).all()
    if not rows:
        raise PlayerSeasonStatsNotFoundError()

    rows_by_format = {row.match_format: row for row in rows}
    return PlayerSeasonInfo(
        player_id=player.id,
        discord_user_id=player.discord_user_id,
        display_name=player.display_name,
        display_name_updated_at=player.display_name_updated_at,
        last_seen_at=player.last_seen_at,
        season=SeasonInfo(
            season_id=season.id,
            name=season.name,
            start_at=season.start_at,
            end_at=season.end_at,
            completed=season.completed,
            completed_at=season.completed_at,
        ),
        format_stats=tuple(
            PlayerFormatSeasonInfo(
                match_format=format_definition.match_format,
                rating=rows_by_format[format_definition.match_format].rating,
                games_played=rows_by_format[format_definition.match_format].games_played,
                wins=rows_by_format[format_definition.match_format].wins,
                losses=rows_by_format[format_definition.match_format].losses,
                draws=rows_by_format[format_definition.match_format].draws,
                last_played_at=rows_by_format[format_definition.match_format].last_played_at,
                carryover_status=rows_by_format[format_definition.match_format].carryover_status,
            )
            for format_definition in get_match_format_definitions()
            if format_definition.match_format in rows_by_format
        ),
    )


def update_season_completion(
    session: Session,
    *,
    season_id: int,
    current_time: datetime | None = None,
) -> bool:
    season = session.get(Season, season_id)
    if season is None:
        raise SeasonNotFoundError()
    if season.completed:
        return False

    resolved_current_time = get_database_now(session) if current_time is None else current_time
    if season.end_at > resolved_current_time:
        return False

    # Callers may update related match state in the same transaction while using autoflush=False.
    # Flush here so the completion check sees those in-transaction changes.
    session.flush()

    remaining_match_exists = session.scalar(
        select(
            exists().where(
                Match.id == ActiveMatchState.match_id,
                Match.started_season_id == season_id,
                ActiveMatchState.state != MatchState.FINALIZED,
            )
        )
    )
    if remaining_match_exists:
        return False

    season.completed = True
    season.completed_at = resolved_current_time
    session.flush()
    enqueue_season_completion_notifications(
        session,
        season_id=season.id,
        season_name=season.name,
        completed_at=resolved_current_time,
        top_rankings=_build_season_top_rankings_notifications(
            session,
            season_id=season.id,
            current_time=resolved_current_time,
        ),
    )
    return True


def update_ended_season_completions(
    session: Session,
    *,
    current_time: datetime | None = None,
) -> tuple[int, ...]:
    resolved_current_time = get_database_now(session) if current_time is None else current_time
    season_ids = session.scalars(
        select(Season.id)
        .where(
            Season.end_at <= resolved_current_time,
            Season.completed.is_(False),
        )
        .order_by(Season.start_at, Season.id)
    ).all()
    updated_season_ids: list[int] = []
    for season_id in season_ids:
        if update_season_completion(
            session,
            season_id=season_id,
            current_time=resolved_current_time,
        ):
            updated_season_ids.append(season_id)
    return tuple(updated_season_ids)


def resolve_active_season_window(current_time: datetime) -> tuple[datetime, datetime]:
    localized_time = current_time.astimezone(JST)
    boundary_this_month = datetime(
        localized_time.year,
        localized_time.month,
        SEASON_BOUNDARY_DAY,
        tzinfo=JST,
    )
    if localized_time < boundary_this_month:
        start_year, start_month = _shift_month(localized_time.year, localized_time.month, -1)
        start_at_jst = datetime(start_year, start_month, SEASON_BOUNDARY_DAY, tzinfo=JST)
    else:
        start_at_jst = boundary_this_month

    end_year, end_month = _shift_month(start_at_jst.year, start_at_jst.month, 1)
    end_at_jst = datetime(end_year, end_month, SEASON_BOUNDARY_DAY, tzinfo=JST)
    return start_at_jst.astimezone(UTC), end_at_jst.astimezone(UTC)


def _build_season_top_rankings_notifications(
    session: Session,
    *,
    season_id: int,
    current_time: datetime,
) -> tuple[SeasonTopRankingsNotification, ...]:
    from dxd_rating.contexts.leaderboard.application import get_season_top_rankings

    notifications: list[SeasonTopRankingsNotification] = []
    for match_format_definition in get_match_format_definitions():
        rankings = get_season_top_rankings(
            session,
            season_id=season_id,
            match_format=match_format_definition.match_format,
            current_time=current_time,
        )
        notifications.append(
            SeasonTopRankingsNotification(
                match_format=rankings.match_format,
                entries=tuple(
                    SeasonTopRankingEntryPayload(
                        rank=entry.rank,
                        display_name=entry.display_name,
                        rating=entry.rating,
                    )
                    for entry in rankings.entries
                ),
            )
        )
    return tuple(notifications)


def resolve_next_season_window(start_at: datetime) -> tuple[datetime, datetime]:
    localized_start = start_at.astimezone(JST)
    end_year, end_month = _shift_month(localized_start.year, localized_start.month, 1)
    next_end_at_jst = datetime(end_year, end_month, SEASON_BOUNDARY_DAY, tzinfo=JST)
    return start_at, next_end_at_jst.astimezone(UTC)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month_index = (year * 12) + (month - 1) + delta
    return month_index // 12, (month_index % 12) + 1


def _get_active_season(session: Session, current_time: datetime) -> Season | None:
    return session.scalar(
        select(Season)
        .where(
            Season.start_at <= current_time,
            Season.end_at > current_time,
        )
        .order_by(Season.start_at.desc(), Season.id.desc())
    )


def _ensure_following_season(session: Session, season: Season) -> Season:
    following_season = session.scalar(
        select(Season).where(Season.start_at == season.end_at).order_by(Season.id)
    )
    if following_season is not None:
        return following_season

    _, following_end_at = resolve_next_season_window(season.end_at)
    return _create_season(
        session,
        start_at=season.end_at,
        end_at=following_end_at,
    )


def _create_season(
    session: Session,
    *,
    start_at: datetime,
    end_at: datetime,
) -> Season:
    season = Season(
        name=build_auto_season_name(start_at),
        start_at=start_at,
        end_at=end_at,
        completed=False,
        completed_at=None,
    )
    session.add(season)
    session.flush()

    player_ids = tuple(session.scalars(select(Player.id).order_by(Player.id)).all())
    _ensure_player_format_stats_rows(
        session,
        player_ids=player_ids,
        seasons=(season,),
    )
    session.flush()
    return season


def _ensure_player_format_stats_rows(
    session: Session,
    *,
    player_ids: tuple[int, ...],
    seasons: tuple[Season, ...],
) -> None:
    if not player_ids or not seasons:
        return

    season_ids = [season.id for season in seasons]
    existing_keys = {
        (row.player_id, row.season_id, row.match_format)
        for row in session.scalars(
            select(PlayerFormatStats).where(
                PlayerFormatStats.player_id.in_(player_ids),
                PlayerFormatStats.season_id.in_(season_ids),
            )
        ).all()
    }
    for player_id in player_ids:
        for season in seasons:
            for format_definition in get_match_format_definitions():
                key = (player_id, season.id, format_definition.match_format)
                if key in existing_keys:
                    continue
                session.add(
                    PlayerFormatStats(
                        player_id=player_id,
                        season_id=season.id,
                        match_format=format_definition.match_format,
                    )
                )


def _get_previous_season(session: Session, season: Season) -> Season | None:
    return session.scalar(
        select(Season).where(Season.end_at == season.start_at).order_by(Season.id.desc())
    )


def _get_previous_stats_by_player_id(
    session: Session,
    *,
    previous_season: Season | None,
    player_ids: tuple[int, ...],
    match_format: MatchFormat,
) -> dict[int, PlayerFormatStats]:
    if previous_season is None or not player_ids:
        return {}

    rows = session.scalars(
        select(PlayerFormatStats).where(
            PlayerFormatStats.player_id.in_(player_ids),
            PlayerFormatStats.season_id == previous_season.id,
            PlayerFormatStats.match_format == match_format,
        )
    ).all()
    return {row.player_id: row for row in rows}


def _apply_carryover_to_row(
    row: PlayerFormatStats,
    *,
    previous_season: Season | None,
    previous_stats: PlayerFormatStats | None,
) -> None:
    if (
        previous_season is None
        or previous_stats is None
        or previous_stats.games_played < MIN_CARRYOVER_GAMES
    ):
        row.rating = float(INITIAL_RATING)
        row.carryover_status = CarryoverStatus.NOT_APPLIED
        row.carryover_source_season_id = None
        row.carryover_source_rating = None
        return

    delta = max(Decimal("0"), Decimal(str(previous_stats.rating - INITIAL_RATING)))
    carryover_delta = int((delta * CARRYOVER_FACTOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    row.rating = float(min(CARRYOVER_MAX_RATING, INITIAL_RATING + carryover_delta))
    row.carryover_status = CarryoverStatus.APPLIED
    row.carryover_source_season_id = previous_season.id
    row.carryover_source_rating = previous_stats.rating
