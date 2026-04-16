from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypedDict

import psycopg
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application.errors import (
    InvalidMatchFormatError,
    InvalidQueueNameError,
    PlayerNotRegisteredError,
    QueueAlreadyJoinedError,
    QueueJoinNotAllowedError,
    QueueJoinRestrictedError,
    QueueNotJoinedError,
    RetryableTaskError,
)
from dxd_rating.contexts.matchmaking.domain import (
    QueueEntrySnapshot,
    RandomLike,
    is_queue_join_allowed,
    prepare_matches_for_batch,
    validate_queue_class_definitions,
)
from dxd_rating.contexts.players.domain import resolve_registered_display_name
from dxd_rating.contexts.restrictions.application.access_restrictions import (
    get_active_player_access_restriction,
)
from dxd_rating.contexts.seasons.application import (
    get_active_and_upcoming_seasons,
    resolve_player_format_stats_for_season,
)
from dxd_rating.platform.db.models import (
    ActiveMatchState,
    ManagedUiChannel,
    ManagedUiType,
    Match,
    MatchFormat,
    MatchParticipant,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchQueueRemovalReason,
    MatchState,
    OutboxEvent,
    OutboxEventType,
    Player,
    PlayerAccessRestrictionType,
)
from dxd_rating.platform.db.session import session_scope
from dxd_rating.shared.constants import (
    MATCH_QUEUE_TTL,
    OUTBOX_NOTIFY_CHANNEL,
    PRESENCE_REMINDER_LEAD_TIME,
    PRODUCTION_MATCH_TIMING_WINDOWS,
    MatchFormatDefinition,
    MatchQueueClassDefinition,
    MatchTimingWindows,
    get_match_format_definition,
    get_match_queue_class_definitions,
    normalize_match_queue_name,
)

DEFAULT_CLEANUP_BATCH_SIZE = 100


def _is_transient_task_db_error(exc: Exception) -> bool:
    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        return True

    if isinstance(exc, (OperationalError, InterfaceError)):
        return True

    if not isinstance(exc, DBAPIError):
        return False

    if exc.connection_invalidated:
        return True

    return isinstance(exc.orig, (psycopg.OperationalError, psycopg.InterfaceError))


@dataclass(frozen=True, slots=True)
class JoinQueueResult:
    queue_entry_id: int
    revision: int
    expire_at: datetime
    queue_class_id: str


@dataclass(frozen=True, slots=True)
class PresentQueueResult:
    queue_entry_id: int
    revision: int | None
    expire_at: datetime | None
    expired: bool


@dataclass(frozen=True, slots=True)
class LeaveQueueResult:
    queue_entry_id: int | None
    expired: bool


@dataclass(frozen=True, slots=True)
class PresenceReminderResult:
    queue_entry_id: int
    reminded: bool


@dataclass(frozen=True, slots=True)
class ExpireQueueEntryResult:
    queue_entry_id: int
    expired: bool


@dataclass(frozen=True, slots=True)
class CreatedMatchResult:
    match_id: int
    queue_entry_ids: tuple[int, ...]
    player_ids: tuple[int, ...]
    match_format: MatchFormat
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PreparedMatch:
    match_format: MatchFormat
    team_a_entries: tuple[MatchQueueEntry, ...]
    team_b_entries: tuple[MatchQueueEntry, ...]

    @property
    def queue_entries(self) -> tuple[MatchQueueEntry, ...]:
        return (*self.team_a_entries, *self.team_b_entries)


@dataclass(frozen=True, slots=True)
class WaitingEntryTimerState:
    queue_entry_id: int
    revision: int
    expire_at: datetime
    last_reminded_revision: int | None


@dataclass(frozen=True, slots=True)
class MatchingQueueNotificationContext:
    channel_id: int
    guild_id: int | None
    mention_discord_user_id: int


@dataclass(frozen=True, slots=True)
class MatchmakingStatusSnapshotEntry:
    match_format: MatchFormat
    queue_name: str
    active_count: int


@dataclass(frozen=True, slots=True)
class MatchmakingStatusSnapshot:
    entries: tuple[MatchmakingStatusSnapshotEntry, ...]
    updated_at: datetime


class NotificationDestinationPayload(TypedDict, total=False):
    kind: str
    channel_id: int
    guild_id: int | None


class TeamRatingEntryPayload(TypedDict):
    discord_user_id: int
    rating: float


class MatchingQueueService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        logger: logging.Logger | None = None,
        *,
        queue_class_definitions: Sequence[MatchQueueClassDefinition] | None = None,
        random_generator: RandomLike | None = None,
        match_timing_windows: MatchTimingWindows = PRODUCTION_MATCH_TIMING_WINDOWS,
    ) -> None:
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(__name__)
        self.match_timing_windows = match_timing_windows
        self._match_format_definitions_by_format = {
            definition.match_format: definition
            for definition in (
                get_match_format_definition(MatchFormat.ONE_VS_ONE),
                get_match_format_definition(MatchFormat.TWO_VS_TWO),
                get_match_format_definition(MatchFormat.THREE_VS_THREE),
            )
            if definition is not None
        }
        self._queue_class_definitions = validate_queue_class_definitions(
            queue_class_definitions or get_match_queue_class_definitions(),
            supported_match_formats=self._match_format_definitions_by_format,
        )
        self._queue_class_definitions_by_id = {
            definition.queue_class_id: definition for definition in self._queue_class_definitions
        }
        self._queue_class_definitions_by_key = {
            (definition.match_format, normalize_match_queue_name(definition.queue_name)): definition
            for definition in self._queue_class_definitions
        }
        self._queue_class_definitions_by_format: dict[
            MatchFormat, tuple[MatchQueueClassDefinition, ...]
        ] = {}
        for match_format in self._match_format_definitions_by_format:
            self._queue_class_definitions_by_format[match_format] = tuple(
                definition
                for definition in self._queue_class_definitions
                if definition.match_format == match_format
            )
        self._random = random_generator or random.Random()

    def join_queue(
        self,
        player_id: int,
        match_format: MatchFormat | str,
        queue_name: str,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult:
        result: JoinQueueResult | None = None

        with session_scope(self.session_factory) as session:
            player = self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            self._ensure_queue_join_not_restricted(session, player.id)
            resolved_match_format = self._resolve_match_format(match_format)
            current_time = self._get_database_now(session)
            active_season = get_active_and_upcoming_seasons(
                session,
                current_time=current_time,
            ).active
            queue_class_definition = self._resolve_queue_class_definition(
                resolved_match_format,
                queue_name,
            )
            player_format_stats = resolve_player_format_stats_for_season(
                session,
                player_ids=(player.id,),
                season_id=active_season.id,
                match_format=resolved_match_format,
                lock_rows=True,
            )[player.id]

            if not is_queue_join_allowed(
                rating=player_format_stats.rating,
                queue_class_definition=queue_class_definition,
                definitions_for_format=self._queue_class_definitions_by_format[
                    resolved_match_format
                ],
            ):
                raise QueueJoinNotAllowedError()

            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is not None and waiting_entry.expire_at > current_time:
                raise QueueAlreadyJoinedError()

            if waiting_entry is not None and waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    waiting_entry,
                    removed_at=current_time,
                )
                session.flush()

            new_entry = MatchQueueEntry(
                player_id=player_id,
                match_format=resolved_match_format,
                queue_class_id=queue_class_definition.queue_class_id,
                status=MatchQueueEntryStatus.WAITING,
                joined_at=current_time,
                last_present_at=current_time,
                expire_at=current_time + MATCH_QUEUE_TTL,
                revision=1,
                last_reminded_revision=None,
            )
            self._apply_notification_context(
                new_entry,
                notification_context,
                mention_discord_user_id=player.discord_user_id,
                recorded_at=current_time,
            )
            session.add(new_entry)
            session.flush()

            result = JoinQueueResult(
                queue_entry_id=new_entry.id,
                revision=new_entry.revision,
                expire_at=new_entry.expire_at,
                queue_class_id=new_entry.queue_class_id,
            )

        if result is None:
            raise RuntimeError("join_queue result was not created")
        return result

    def present(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> PresentQueueResult:
        result: PresentQueueResult | None = None

        with session_scope(self.session_factory) as session:
            player = self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            current_time = self._get_database_now(session)
            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is None:
                raise QueueNotJoinedError()

            if waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    waiting_entry,
                    removed_at=current_time,
                )
                result = PresentQueueResult(
                    queue_entry_id=waiting_entry.id,
                    revision=None,
                    expire_at=None,
                    expired=True,
                )
            else:
                waiting_entry.last_present_at = current_time
                waiting_entry.expire_at = current_time + MATCH_QUEUE_TTL
                waiting_entry.revision += 1
                waiting_entry.last_reminded_revision = None
                self._apply_notification_context(
                    waiting_entry,
                    notification_context,
                    mention_discord_user_id=player.discord_user_id,
                    recorded_at=current_time,
                )
                result = PresentQueueResult(
                    queue_entry_id=waiting_entry.id,
                    revision=waiting_entry.revision,
                    expire_at=waiting_entry.expire_at,
                    expired=False,
                )

        if result is None:
            raise RuntimeError("present result was not created")
        return result

    def update_waiting_notification_context(
        self,
        queue_entry_id: int,
        notification_context: MatchingQueueNotificationContext,
    ) -> bool:
        updated = False

        with session_scope(self.session_factory) as session:
            entry = self._get_queue_entry_for_update(session, queue_entry_id)
            if entry is None or entry.status != MatchQueueEntryStatus.WAITING:
                return False

            current_time = self._get_database_now(session)
            self._apply_notification_context(
                entry,
                notification_context,
                mention_discord_user_id=entry.notification_mention_discord_user_id,
                recorded_at=current_time,
            )
            updated = True

        return updated

    def update_waiting_presence_thread_channel_id(
        self,
        queue_entry_id: int,
        presence_thread_channel_id: int,
    ) -> bool:
        updated = False

        with session_scope(self.session_factory) as session:
            entry = self._get_queue_entry_for_update(session, queue_entry_id)
            if entry is None or entry.status != MatchQueueEntryStatus.WAITING:
                return False

            entry.presence_thread_channel_id = presence_thread_channel_id
            updated = True

        return updated

    def get_waiting_entry_notification_channel_id(self, player_id: int) -> int | None:
        with session_scope(self.session_factory) as session:
            return session.scalar(
                select(
                    func.coalesce(
                        MatchQueueEntry.presence_thread_channel_id,
                        MatchQueueEntry.notification_channel_id,
                    )
                ).where(
                    MatchQueueEntry.player_id == player_id,
                    MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
                )
            )

    def get_matchmaking_status_snapshot(self) -> MatchmakingStatusSnapshot:
        with session_scope(self.session_factory) as session:
            current_time = self._get_database_now(session)
            window_start = current_time - timedelta(minutes=30)
            joined_counts = self._count_queue_entries_by_class(
                session,
                joined_after=window_start,
            )
            left_counts = self._count_queue_entries_by_class(
                session,
                status=MatchQueueEntryStatus.LEFT,
                removed_after=window_start,
            )
            expired_counts = self._count_queue_entries_by_class(
                session,
                status=MatchQueueEntryStatus.EXPIRED,
                removed_after=window_start,
            )

        entries = tuple(
            MatchmakingStatusSnapshotEntry(
                match_format=definition.match_format,
                queue_name=definition.queue_name,
                active_count=max(
                    joined_counts.get(definition.queue_class_id, 0)
                    - left_counts.get(definition.queue_class_id, 0)
                    - expired_counts.get(definition.queue_class_id, 0),
                    0,
                ),
            )
            for definition in self._queue_class_definitions
        )
        return MatchmakingStatusSnapshot(entries=entries, updated_at=current_time)

    def leave(self, player_id: int) -> LeaveQueueResult:
        result: LeaveQueueResult | None = None

        with session_scope(self.session_factory) as session:
            self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            current_time = self._get_database_now(session)
            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is None:
                result = LeaveQueueResult(
                    queue_entry_id=None,
                    expired=False,
                )
            elif waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    waiting_entry,
                    removed_at=current_time,
                )
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=True,
                )
            else:
                waiting_entry.status = MatchQueueEntryStatus.LEFT
                waiting_entry.removed_at = current_time
                waiting_entry.removal_reason = MatchQueueRemovalReason.USER_LEAVE
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=False,
                )

        if result is None:
            raise RuntimeError("leave result was not created")
        return result

    def process_presence_reminder(
        self, queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        reminded = False

        try:
            with session_scope(self.session_factory) as session:
                entry = self._get_queue_entry_for_update(session, queue_entry_id)
                if entry is None:
                    return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=False)

                current_time = self._get_database_now(session)
                remind_at = entry.expire_at - PRESENCE_REMINDER_LEAD_TIME
                already_reminded = entry.last_reminded_revision == entry.revision

                if (
                    entry.status != MatchQueueEntryStatus.WAITING
                    or entry.revision != expected_revision
                    or entry.expire_at <= current_time
                    or remind_at > current_time
                    or already_reminded
                ):
                    return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=False)

                entry.last_reminded_revision = entry.revision
                self._enqueue_outbox_event(
                    session,
                    event_type=OutboxEventType.PRESENCE_REMINDER,
                    dedupe_key=f"presence_reminder:{entry.id}:{entry.revision}",
                    payload=self._build_presence_reminder_payload(entry),
                )
                reminded = True
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="processing presence reminder")
            raise

        if reminded:
            self.logger.info("Queued presence reminder for queue_entry_id=%s", queue_entry_id)
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=reminded)

    def process_expire(self, queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        expired = False

        try:
            with session_scope(self.session_factory) as session:
                entry = self._get_queue_entry_for_update(session, queue_entry_id)
                if entry is None:
                    return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=False)

                current_time = self._get_database_now(session)
                if (
                    entry.status != MatchQueueEntryStatus.WAITING
                    or entry.revision != expected_revision
                    or entry.expire_at > current_time
                ):
                    return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=False)

                self._mark_entry_expired(
                    entry,
                    removed_at=current_time,
                )
                self._enqueue_outbox_event(
                    session,
                    event_type=OutboxEventType.QUEUE_EXPIRED,
                    dedupe_key=f"queue_expired:{entry.id}:{entry.revision}",
                    payload=self._build_queue_expired_payload(entry),
                )
                expired = True
        except Exception as exc:
            self._raise_retryable_task_error(exc, operation="processing expire")
            raise

        if expired:
            self.logger.info("Expired queue entry queue_entry_id=%s", queue_entry_id)
        return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=expired)

    def cleanup_expired_entries(
        self,
        *,
        batch_size: int = DEFAULT_CLEANUP_BATCH_SIZE,
        warn_on_cleanup: bool = False,
    ) -> tuple[int, ...]:
        expired_queue_entry_ids: list[int] = []

        while True:
            batch_ids: list[int] = []

            with session_scope(self.session_factory) as session:
                current_time = self._get_database_now(session)
                expired_entries = session.scalars(
                    select(MatchQueueEntry)
                    .where(
                        MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
                        MatchQueueEntry.expire_at <= func.now(),
                    )
                    .order_by(MatchQueueEntry.expire_at, MatchQueueEntry.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                ).all()

                if not expired_entries:
                    break

                for entry in expired_entries:
                    self._mark_entry_expired(
                        entry,
                        removed_at=current_time,
                    )
                    self._enqueue_outbox_event(
                        session,
                        event_type=OutboxEventType.QUEUE_EXPIRED,
                        dedupe_key=f"queue_expired:{entry.id}:{entry.revision}",
                        payload=self._build_queue_expired_payload(entry),
                    )
                    batch_ids.append(entry.id)

            expired_queue_entry_ids.extend(batch_ids)

        if expired_queue_entry_ids:
            log_message = "Cleanup expired queue entries count=%s queue_entry_ids=%s"
            if warn_on_cleanup:
                self.logger.warning(
                    log_message,
                    len(expired_queue_entry_ids),
                    expired_queue_entry_ids,
                )
            else:
                self.logger.info(
                    log_message,
                    len(expired_queue_entry_ids),
                    expired_queue_entry_ids,
                )

        return tuple(expired_queue_entry_ids)

    def try_create_matches(
        self,
        queue_class_id: str | None = None,
    ) -> tuple[CreatedMatchResult, ...]:
        created_matches: list[CreatedMatchResult] = []

        if queue_class_id is None:
            for queue_class_definition in self._queue_class_definitions:
                while True:
                    created_batch = self._try_create_batch(queue_class_definition.queue_class_id)
                    if not created_batch:
                        break
                    created_matches.extend(created_batch)
            return tuple(created_matches)

        self._require_queue_class_definition_by_id(queue_class_id)
        while True:
            created_batch = self._try_create_batch(queue_class_id)
            if not created_batch:
                break
            created_matches.extend(created_batch)

        return tuple(created_matches)

    def _try_create_batch(self, queue_class_id: str) -> tuple[CreatedMatchResult, ...]:
        queue_class_definition = self._require_queue_class_definition_by_id(queue_class_id)
        format_definition = self._require_match_format_definition(
            queue_class_definition.match_format
        )
        created_matches: list[CreatedMatchResult] = []

        with session_scope(self.session_factory) as session:
            queue_entries = session.scalars(
                select(MatchQueueEntry)
                .where(
                    MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
                    MatchQueueEntry.queue_class_id == queue_class_id,
                    MatchQueueEntry.expire_at > func.now(),
                )
                .order_by(MatchQueueEntry.joined_at, MatchQueueEntry.id)
                .limit(format_definition.players_per_batch)
                .with_for_update(skip_locked=True)
            ).all()

            if len(queue_entries) < format_definition.players_per_batch:
                return tuple()

            current_time = self._get_database_now(session)
            active_season = get_active_and_upcoming_seasons(
                session,
                current_time=current_time,
            ).active
            player_ids = tuple(sorted({queue_entry.player_id for queue_entry in queue_entries}))
            for player_id in player_ids:
                self._acquire_player_lock(session, player_id)
            player_format_stats_by_player_id = resolve_player_format_stats_for_season(
                session,
                player_ids=player_ids,
                season_id=active_season.id,
                match_format=format_definition.match_format,
                lock_rows=True,
            )
            ratings_by_player_id = {
                player_id: player_format_stats.rating
                for player_id, player_format_stats in player_format_stats_by_player_id.items()
            }
            prepared_matches = self._prepare_matches_for_batch(
                queue_entries,
                format_definition,
                ratings_by_player_id=ratings_by_player_id,
            )
            for prepared_match in prepared_matches:
                match = Match(
                    match_format=prepared_match.match_format,
                    queue_class_id=queue_class_id,
                    started_season_id=active_season.id,
                    created_at=current_time,
                )
                session.add(match)
                session.flush()
                session.add(
                    ActiveMatchState(
                        match_id=match.id,
                        created_at=current_time,
                        parent_deadline_at=(
                            current_time + self.match_timing_windows.parent_selection_window
                        ),
                        parent_player_id=None,
                        parent_decided_at=None,
                        report_open_at=None,
                        reporting_opened_at=None,
                        report_deadline_at=None,
                        approval_started_at=None,
                        approval_deadline_at=None,
                        provisional_result=None,
                        admin_review_required=False,
                        admin_review_reasons=[],
                        state=MatchState.WAITING_FOR_PARENT,
                        finalized_at=None,
                        finalized_by_admin=False,
                    )
                )

                for team, team_entries in (
                    (MatchParticipantTeam.TEAM_A, prepared_match.team_a_entries),
                    (MatchParticipantTeam.TEAM_B, prepared_match.team_b_entries),
                ):
                    for slot, queue_entry in enumerate(team_entries, start=1):
                        participant = MatchParticipant(
                            match_id=match.id,
                            player_id=queue_entry.player_id,
                            queue_entry_id=queue_entry.id,
                            team=team,
                            slot=slot,
                            notification_channel_id=queue_entry.notification_channel_id,
                            notification_guild_id=queue_entry.notification_guild_id,
                            notification_mention_discord_user_id=(
                                queue_entry.notification_mention_discord_user_id
                            ),
                            notification_recorded_at=queue_entry.notification_recorded_at,
                            created_at=current_time,
                        )
                        session.add(participant)
                        queue_entry.status = MatchQueueEntryStatus.MATCHED

                session.flush()
                for payload in self._build_match_created_payloads(
                    session,
                    match.id,
                    prepared_match.match_format,
                    queue_class_id,
                    prepared_match.team_a_entries,
                    prepared_match.team_b_entries,
                    ratings_by_player_id=ratings_by_player_id,
                ):
                    destination = payload["destination"]
                    channel_id = destination["channel_id"]
                    self._enqueue_outbox_event(
                        session,
                        event_type=OutboxEventType.MATCH_CREATED,
                        dedupe_key=f"match_created:{match.id}:{channel_id}",
                        payload=payload,
                    )

                matched_entries_in_join_order = tuple(
                    sorted(
                        prepared_match.queue_entries,
                        key=lambda entry: (entry.joined_at, entry.id),
                    )
                )
                created_matches.append(
                    CreatedMatchResult(
                        match_id=match.id,
                        queue_entry_ids=tuple(
                            queue_entry.id for queue_entry in matched_entries_in_join_order
                        ),
                        player_ids=tuple(
                            queue_entry.player_id for queue_entry in matched_entries_in_join_order
                        ),
                        match_format=prepared_match.match_format,
                        created_at=current_time,
                    )
                )

        for created_match in created_matches:
            self.logger.info(
                "Created match match_id=%s match_format=%s queue_class_id=%s queue_entry_ids=%s",
                created_match.match_id,
                created_match.match_format.value,
                queue_class_id,
                created_match.queue_entry_ids,
            )
        return tuple(created_matches)

    def _prepare_matches_for_batch(
        self,
        queue_entries: Sequence[MatchQueueEntry],
        format_definition: MatchFormatDefinition,
        *,
        ratings_by_player_id: dict[int, float],
    ) -> tuple[PreparedMatch, ...]:
        prepared_match_plans = prepare_matches_for_batch(
            tuple(
                QueueEntrySnapshot(
                    queue_entry_id=queue_entry.id,
                    player_id=queue_entry.player_id,
                    match_format=queue_entry.match_format,
                    rating=ratings_by_player_id[queue_entry.player_id],
                    joined_at=queue_entry.joined_at,
                )
                for queue_entry in queue_entries
            ),
            format_definition,
            random_generator=self._random,
        )
        queue_entries_by_id = {queue_entry.id: queue_entry for queue_entry in queue_entries}
        return tuple(
            PreparedMatch(
                match_format=prepared_match_plan.match_format,
                team_a_entries=tuple(
                    queue_entries_by_id[queue_entry_id]
                    for queue_entry_id in prepared_match_plan.team_a_entry_ids
                ),
                team_b_entries=tuple(
                    queue_entries_by_id[queue_entry_id]
                    for queue_entry_id in prepared_match_plan.team_b_entry_ids
                ),
            )
            for prepared_match_plan in prepared_match_plans
        )

    def _build_match_created_payloads(
        self,
        session: Session,
        match_id: int,
        match_format: MatchFormat,
        queue_class_id: str,
        team_a_entries: Sequence[MatchQueueEntry],
        team_b_entries: Sequence[MatchQueueEntry],
        *,
        ratings_by_player_id: dict[int, float],
    ) -> tuple[dict[str, Any], ...]:
        queue_class_definition = self._require_queue_class_definition_by_id(queue_class_id)
        matchmaking_channel = self._get_managed_ui_channel(
            session,
            ManagedUiType.MATCHMAKING_CHANNEL,
        )
        team_a_rating_entries = self._build_team_rating_entries(
            team_entries=team_a_entries,
            ratings_by_player_id=ratings_by_player_id,
        )
        team_b_rating_entries = self._build_team_rating_entries(
            team_entries=team_b_entries,
            ratings_by_player_id=ratings_by_player_id,
        )
        participant_payloads = self._build_participant_match_created_payloads(
            match_id=match_id,
            match_format=match_format,
            queue_class_definition=queue_class_definition,
            matchmaking_channel=matchmaking_channel,
            team_a_entries=team_a_entries,
            team_b_entries=team_b_entries,
            team_a_rating_entries=team_a_rating_entries,
            team_b_rating_entries=team_b_rating_entries,
        )
        matchmaking_news_payload = self._build_matchmaking_news_match_created_payload(
            session,
            match_id=match_id,
            match_format=match_format,
            queue_class_definition=queue_class_definition,
            matchmaking_channel=matchmaking_channel,
            team_a_entries=team_a_entries,
            team_b_entries=team_b_entries,
            team_a_rating_entries=team_a_rating_entries,
            team_b_rating_entries=team_b_rating_entries,
        )
        if matchmaking_news_payload is not None:
            return (matchmaking_news_payload, *participant_payloads)

        return participant_payloads

    def _build_matchmaking_news_match_created_payload(
        self,
        session: Session,
        *,
        match_id: int,
        match_format: MatchFormat,
        queue_class_definition: MatchQueueClassDefinition,
        matchmaking_channel: ManagedUiChannel | None,
        team_a_entries: Sequence[MatchQueueEntry],
        team_b_entries: Sequence[MatchQueueEntry],
        team_a_rating_entries: Sequence[TeamRatingEntryPayload],
        team_b_rating_entries: Sequence[TeamRatingEntryPayload],
    ) -> dict[str, Any] | None:
        matchmaking_news_channel = self._get_managed_ui_channel(
            session,
            ManagedUiType.MATCHMAKING_NEWS_CHANNEL,
        )
        if matchmaking_news_channel is None:
            return None

        all_entries = tuple(
            sorted(
                [*team_a_entries, *team_b_entries],
                key=lambda entry: (entry.joined_at, entry.id),
            )
        )
        all_player_ids = [queue_entry.player_id for queue_entry in all_entries]
        players_by_id = {
            player.id: player
            for player in session.scalars(select(Player).where(Player.id.in_(all_player_ids))).all()
        }
        announcement_guild_id = next(
            (
                queue_entry.notification_guild_id
                for queue_entry in all_entries
                if queue_entry.notification_guild_id is not None
            ),
            None,
        )
        team_a_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id for queue_entry in team_a_entries
        ]
        team_b_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id for queue_entry in team_b_entries
        ]

        payload: dict[str, Any] = {
            "match_id": match_id,
            "match_format": match_format.value,
            "queue_name": queue_class_definition.queue_name,
            "destination": {
                "kind": "channel",
                "channel_id": matchmaking_news_channel.channel_id,
                "guild_id": announcement_guild_id,
            },
            "team_a_discord_user_ids": team_a_discord_user_ids,
            "team_b_discord_user_ids": team_b_discord_user_ids,
            "team_a_rating_entries": list(team_a_rating_entries),
            "team_b_rating_entries": list(team_b_rating_entries),
            "team_a_player_display_names": [
                self._resolve_match_announcement_player_display_name(
                    players_by_id,
                    queue_entry.player_id,
                )
                for queue_entry in team_a_entries
            ],
            "team_b_player_display_names": [
                self._resolve_match_announcement_player_display_name(
                    players_by_id,
                    queue_entry.player_id,
                )
                for queue_entry in team_b_entries
            ],
        }
        self._apply_match_operation_thread_payload(
            payload,
            matchmaking_channel=matchmaking_channel,
            create_match_operation_thread=True,
        )
        return payload

    def _build_participant_match_created_payloads(
        self,
        *,
        match_id: int,
        match_format: MatchFormat,
        queue_class_definition: MatchQueueClassDefinition,
        matchmaking_channel: ManagedUiChannel | None,
        team_a_entries: Sequence[MatchQueueEntry],
        team_b_entries: Sequence[MatchQueueEntry],
        team_a_rating_entries: Sequence[TeamRatingEntryPayload],
        team_b_rating_entries: Sequence[TeamRatingEntryPayload],
    ) -> tuple[dict[str, Any], ...]:
        all_entries = tuple(
            sorted(
                [*team_a_entries, *team_b_entries],
                key=lambda entry: (entry.joined_at, entry.id),
            )
        )
        team_a_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id for queue_entry in team_a_entries
        ]
        team_b_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id for queue_entry in team_b_entries
        ]
        queue_entry_ids = [entry.id for entry in all_entries]
        player_ids = [entry.player_id for entry in all_entries]

        payloads: list[dict[str, Any]] = []
        for queue_entry in all_entries:
            if queue_entry.presence_thread_channel_id is None:
                self.logger.warning(
                    "Skipping participant match_created notification without presence thread "
                    "queue_entry_id=%s player_id=%s notification_channel_id=%s",
                    queue_entry.id,
                    queue_entry.player_id,
                    queue_entry.notification_channel_id,
                )
                continue

            payloads.append(
                self._build_presence_thread_match_created_payload(
                    match_id=match_id,
                    match_format=match_format,
                    queue_class_definition=queue_class_definition,
                    matchmaking_channel=matchmaking_channel,
                    queue_entry=queue_entry,
                    team_a_discord_user_ids=team_a_discord_user_ids,
                    team_b_discord_user_ids=team_b_discord_user_ids,
                    team_a_rating_entries=team_a_rating_entries,
                    team_b_rating_entries=team_b_rating_entries,
                    queue_entry_ids=queue_entry_ids,
                    player_ids=player_ids,
                )
            )

        return tuple(payloads)

    def _build_presence_thread_match_created_payload(
        self,
        *,
        match_id: int,
        match_format: MatchFormat,
        queue_class_definition: MatchQueueClassDefinition,
        matchmaking_channel: ManagedUiChannel | None,
        queue_entry: MatchQueueEntry,
        team_a_discord_user_ids: Sequence[int],
        team_b_discord_user_ids: Sequence[int],
        team_a_rating_entries: Sequence[TeamRatingEntryPayload],
        team_b_rating_entries: Sequence[TeamRatingEntryPayload],
        queue_entry_ids: Sequence[int],
        player_ids: Sequence[int],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "match_id": match_id,
            "match_format": match_format.value,
            "queue_name": queue_class_definition.queue_name,
            "queue_entry_ids": list(queue_entry_ids),
            "player_ids": list(player_ids),
            "destination": self._build_presence_thread_destination_payload(
                queue_entry,
                event_context="match_created",
            ),
            "mention_discord_user_id": queue_entry.notification_mention_discord_user_id,
            "team_a_discord_user_ids": list(team_a_discord_user_ids),
            "team_b_discord_user_ids": list(team_b_discord_user_ids),
            "team_a_rating_entries": list(team_a_rating_entries),
            "team_b_rating_entries": list(team_b_rating_entries),
        }
        self._apply_match_operation_thread_payload(
            payload,
            matchmaking_channel=matchmaking_channel,
            create_match_operation_thread=True,
        )
        return payload

    def _build_team_rating_entries(
        self,
        *,
        team_entries: Sequence[MatchQueueEntry],
        ratings_by_player_id: dict[int, float],
    ) -> list[TeamRatingEntryPayload]:
        return [
            {
                "discord_user_id": queue_entry.notification_mention_discord_user_id,
                "rating": ratings_by_player_id[queue_entry.player_id],
            }
            for queue_entry in team_entries
        ]

    def _get_managed_ui_channel(
        self,
        session: Session,
        ui_type: ManagedUiType,
    ) -> ManagedUiChannel | None:
        return session.scalar(
            select(ManagedUiChannel)
            .where(ManagedUiChannel.ui_type == ui_type)
            .order_by(ManagedUiChannel.id.asc())
        )

    def _apply_match_operation_thread_payload(
        self,
        payload: dict[str, Any],
        *,
        matchmaking_channel: ManagedUiChannel | None,
        create_match_operation_thread: bool,
    ) -> None:
        if matchmaking_channel is None:
            return

        payload["match_operation_thread_parent_channel_id"] = matchmaking_channel.channel_id
        if create_match_operation_thread:
            payload["create_match_operation_thread"] = True

    def _resolve_match_announcement_player_display_name(
        self,
        players_by_id: dict[int, Player],
        player_id: int,
    ) -> str:
        player = players_by_id.get(player_id)
        if player is None:
            raise RuntimeError(f"Player not found while building match announcement: {player_id}")

        resolved_display_name = resolve_registered_display_name(
            discord_user_id=player.discord_user_id,
            display_name=player.display_name,
        )
        if resolved_display_name is not None:
            return resolved_display_name
        return str(player.discord_user_id)

    def load_waiting_entry_timer_states(
        self,
    ) -> tuple[datetime, tuple[WaitingEntryTimerState, ...]]:
        with session_scope(self.session_factory) as session:
            current_time = self._get_database_now(session)
            rows = session.execute(
                select(
                    MatchQueueEntry.id,
                    MatchQueueEntry.revision,
                    MatchQueueEntry.expire_at,
                    MatchQueueEntry.last_reminded_revision,
                )
                .where(
                    MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
                    MatchQueueEntry.expire_at > func.now(),
                )
                .order_by(MatchQueueEntry.expire_at, MatchQueueEntry.id)
            ).all()

        states = tuple(
            WaitingEntryTimerState(
                queue_entry_id=row.id,
                revision=row.revision,
                expire_at=row.expire_at,
                last_reminded_revision=row.last_reminded_revision,
            )
            for row in rows
        )
        return current_time, states

    def _ensure_player_exists(self, session: Session, player_id: int) -> Player:
        player = session.get(Player, player_id)
        if player is None:
            raise PlayerNotRegisteredError()
        return player

    def _resolve_match_format(self, match_format: MatchFormat | str) -> MatchFormat:
        try:
            if isinstance(match_format, MatchFormat):
                return match_format
            return MatchFormat(match_format)
        except ValueError as exc:
            raise InvalidMatchFormatError() from exc

    def _resolve_queue_class_definition(
        self,
        match_format: MatchFormat,
        queue_name: str,
    ) -> MatchQueueClassDefinition:
        definition = self._queue_class_definitions_by_key.get(
            (match_format, normalize_match_queue_name(queue_name))
        )
        if definition is None:
            raise InvalidQueueNameError()
        return definition

    def _ensure_queue_join_not_restricted(self, session: Session, player_id: int) -> None:
        restriction = get_active_player_access_restriction(
            session,
            player_id=player_id,
            restriction_type=PlayerAccessRestrictionType.QUEUE_JOIN,
        )
        if restriction is not None:
            raise QueueJoinRestrictedError()

    def _require_queue_class_definition_by_id(
        self,
        queue_class_id: str,
    ) -> MatchQueueClassDefinition:
        definition = self._queue_class_definitions_by_id.get(queue_class_id)
        if definition is None:
            raise ValueError(f"Unknown queue_class_id: {queue_class_id}")
        return definition

    def _require_match_format_definition(
        self,
        match_format: MatchFormat,
    ) -> MatchFormatDefinition:
        definition = self._match_format_definitions_by_format.get(match_format)
        if definition is None:
            raise ValueError(f"Unknown match_format: {match_format.value}")
        return definition

    def _count_queue_entries_by_class(
        self,
        session: Session,
        *,
        joined_after: datetime | None = None,
        status: MatchQueueEntryStatus | None = None,
        removed_after: datetime | None = None,
    ) -> dict[str, int]:
        query = select(
            MatchQueueEntry.queue_class_id,
            func.count(MatchQueueEntry.id),
        ).group_by(MatchQueueEntry.queue_class_id)

        if joined_after is not None:
            query = query.where(MatchQueueEntry.joined_at >= joined_after)
        if status is not None:
            query = query.where(MatchQueueEntry.status == status)
        if removed_after is not None:
            query = query.where(
                MatchQueueEntry.removed_at.is_not(None),
                MatchQueueEntry.removed_at >= removed_after,
            )

        return {queue_class_id: count for queue_class_id, count in session.execute(query).all()}

    def _raise_retryable_task_error(self, exc: Exception, *, operation: str) -> None:
        if isinstance(exc, RetryableTaskError):
            return

        if _is_transient_task_db_error(exc):
            raise RetryableTaskError(f"Temporary database failure while {operation}") from exc

    def _acquire_player_lock(self, session: Session, player_id: int) -> None:
        session.execute(select(func.pg_advisory_xact_lock(player_id)))

    def _get_waiting_entry_for_update(
        self, session: Session, player_id: int
    ) -> MatchQueueEntry | None:
        return session.scalar(
            select(MatchQueueEntry)
            .where(
                MatchQueueEntry.player_id == player_id,
                MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
            )
            .with_for_update()
        )

    def _get_queue_entry_for_update(
        self, session: Session, queue_entry_id: int
    ) -> MatchQueueEntry | None:
        return session.scalar(
            select(MatchQueueEntry).where(MatchQueueEntry.id == queue_entry_id).with_for_update()
        )

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()

    def _mark_entry_expired(
        self,
        entry: MatchQueueEntry,
        *,
        removed_at: datetime,
    ) -> None:
        entry.status = MatchQueueEntryStatus.EXPIRED
        entry.removed_at = removed_at
        entry.removal_reason = MatchQueueRemovalReason.TIMEOUT

    def _enqueue_outbox_event(
        self,
        session: Session,
        *,
        event_type: OutboxEventType,
        dedupe_key: str,
        payload: dict[str, Any],
    ) -> None:
        inserted_event_id = session.execute(
            pg_insert(OutboxEvent)
            .values(
                event_type=event_type,
                dedupe_key=dedupe_key,
                payload=payload,
            )
            .on_conflict_do_nothing(index_elements=[OutboxEvent.dedupe_key])
            .returning(OutboxEvent.id)
        ).scalar_one_or_none()

        if inserted_event_id is None:
            return

        session.execute(
            select(
                func.pg_notify(
                    OUTBOX_NOTIFY_CHANNEL,
                    str(inserted_event_id),
                )
            )
        )

    def _apply_notification_context(
        self,
        entry: MatchQueueEntry,
        notification_context: MatchingQueueNotificationContext | None,
        *,
        mention_discord_user_id: int,
        recorded_at: datetime,
    ) -> None:
        entry.notification_mention_discord_user_id = mention_discord_user_id

        if notification_context is None:
            return

        entry.notification_channel_id = notification_context.channel_id
        entry.notification_guild_id = notification_context.guild_id
        entry.notification_dm_discord_user_id = None
        entry.notification_interaction_application_id = None
        entry.notification_interaction_token = None
        entry.notification_mention_discord_user_id = notification_context.mention_discord_user_id
        entry.notification_recorded_at = recorded_at

    def _build_presence_reminder_payload(self, entry: MatchQueueEntry) -> dict[str, Any]:
        return {
            "queue_entry_id": entry.id,
            "player_id": entry.player_id,
            "revision": entry.revision,
            "expire_at": entry.expire_at.isoformat(),
            "destination": self._build_player_operation_destination_payload(
                entry,
                event_context="presence_reminder",
            ),
            "mention_discord_user_id": entry.notification_mention_discord_user_id,
        }

    def _build_queue_expired_payload(self, entry: MatchQueueEntry) -> dict[str, Any]:
        return {
            "queue_entry_id": entry.id,
            "player_id": entry.player_id,
            "revision": entry.revision,
            "expire_at": entry.expire_at.isoformat(),
            "destination": self._build_player_operation_destination_payload(
                entry,
                event_context="queue_expired",
            ),
            "mention_discord_user_id": entry.notification_mention_discord_user_id,
        }

    def _build_player_operation_destination_payload(
        self,
        entry: MatchQueueEntry,
        *,
        event_context: str,
    ) -> NotificationDestinationPayload:
        channel_id = (
            entry.presence_thread_channel_id
            if entry.presence_thread_channel_id is not None
            else entry.notification_channel_id
        )
        if channel_id is None:
            raise ValueError(
                f"notification_channel_id is missing for {event_context} queue_entry_id={entry.id}"
            )
        return {
            "kind": "channel",
            "channel_id": channel_id,
            "guild_id": entry.notification_guild_id,
        }

    def _build_presence_thread_destination_payload(
        self,
        entry: MatchQueueEntry,
        *,
        event_context: str,
    ) -> NotificationDestinationPayload:
        if entry.presence_thread_channel_id is None:
            raise ValueError(
                "presence_thread_channel_id is missing for "
                f"{event_context} queue_entry_id={entry.id}"
            )
        return {
            "kind": "channel",
            "channel_id": entry.presence_thread_channel_id,
            "guild_id": entry.notification_guild_id,
        }
