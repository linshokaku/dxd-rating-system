from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypedDict

import psycopg
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from bot.constants import OUTBOX_NOTIFY_CHANNEL
from bot.db.session import session_scope
from bot.models import (
    Match,
    MatchParticipant,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchQueueRemovalReason,
    OutboxEvent,
    OutboxEventType,
    Player,
)
from bot.services.errors import (
    PlayerNotRegisteredError,
    QueueAlreadyJoinedError,
    QueueNotJoinedError,
    RetryableTaskError,
)

MATCH_QUEUE_TTL = timedelta(minutes=5)
PRESENCE_REMINDER_LEAD_TIME = timedelta(minutes=1)
MATCH_PLAYER_COUNT = 6
TEAM_PLAYER_COUNT = 3
DEFAULT_CLEANUP_BATCH_SIZE = 100

JOIN_QUEUE_MESSAGE = "キューに参加しました。5分間マッチングします。"
QUEUE_ALREADY_JOINED_MESSAGE = "すでにキュー参加中です。"
QUEUE_PRESENT_UPDATED_MESSAGE = "在席を更新しました。次の期限は5分後です。"
QUEUE_NOT_JOINED_MESSAGE = "キューに参加していません。"
QUEUE_PRESENT_EXPIRED_MESSAGE = "期限切れのためキューから外れました。"
QUEUE_LEFT_MESSAGE = "キューから退出しました。"
QUEUE_ALREADY_EXPIRED_MESSAGE = "すでに期限切れでキューから外れています。"
PRESENCE_REMINDER_NOTIFICATION_MESSAGE = (
    "在席確認です。1分以内に在席更新がない場合はマッチングキューから外れます。"
)
QUEUE_EXPIRED_NOTIFICATION_MESSAGE = "期限切れでマッチングキューから外れました。"
MATCH_CREATED_NOTIFICATION_MESSAGE = "マッチ成立です。"


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
    message: str = JOIN_QUEUE_MESSAGE


@dataclass(frozen=True, slots=True)
class PresentQueueResult:
    queue_entry_id: int
    revision: int | None
    expire_at: datetime | None
    expired: bool
    message: str


@dataclass(frozen=True, slots=True)
class LeaveQueueResult:
    queue_entry_id: int | None
    expired: bool
    message: str


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


class NotificationDestinationPayload(TypedDict):
    channel_id: int
    guild_id: int | None


class MatchingQueueService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        logger: logging.Logger | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(__name__)

    def join_queue(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult:
        result: JoinQueueResult | None = None

        with session_scope(self.session_factory) as session:
            player = self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            current_time = self._get_database_now(session)
            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is not None and waiting_entry.expire_at > current_time:
                raise QueueAlreadyJoinedError(QUEUE_ALREADY_JOINED_MESSAGE)

            if waiting_entry is not None and waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    waiting_entry,
                    removed_at=current_time,
                )
                session.flush()

            new_entry = MatchQueueEntry(
                player_id=player_id,
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
                raise QueueNotJoinedError(QUEUE_NOT_JOINED_MESSAGE)

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
                    message=QUEUE_PRESENT_EXPIRED_MESSAGE,
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
                    message=QUEUE_PRESENT_UPDATED_MESSAGE,
                )

        if result is None:
            raise RuntimeError("present result was not created")
        return result

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
                    message=QUEUE_LEFT_MESSAGE,
                )
            elif waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    waiting_entry,
                    removed_at=current_time,
                )
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=True,
                    message=QUEUE_ALREADY_EXPIRED_MESSAGE,
                )
            else:
                waiting_entry.status = MatchQueueEntryStatus.LEFT
                waiting_entry.removed_at = current_time
                waiting_entry.removal_reason = MatchQueueRemovalReason.USER_LEAVE
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=False,
                    message=QUEUE_LEFT_MESSAGE,
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

    def try_create_matches(self) -> tuple[CreatedMatchResult, ...]:
        created_matches: list[CreatedMatchResult] = []

        while True:
            created_match = self._try_create_single_match()
            if created_match is None:
                break
            created_matches.append(created_match)

        return tuple(created_matches)

    def _try_create_single_match(self) -> CreatedMatchResult | None:
        created_match: CreatedMatchResult | None = None

        with session_scope(self.session_factory) as session:
            queue_entries = session.scalars(
                select(MatchQueueEntry)
                .where(
                    MatchQueueEntry.status == MatchQueueEntryStatus.WAITING,
                    MatchQueueEntry.expire_at > func.now(),
                )
                .order_by(MatchQueueEntry.joined_at, MatchQueueEntry.id)
                .limit(MATCH_PLAYER_COUNT)
                .with_for_update(skip_locked=True)
            ).all()

            if len(queue_entries) < MATCH_PLAYER_COUNT:
                return None

            current_time = self._get_database_now(session)
            match = Match(created_at=current_time)
            session.add(match)
            session.flush()

            for index, queue_entry in enumerate(queue_entries):
                team = (
                    MatchParticipantTeam.TEAM_A
                    if index < TEAM_PLAYER_COUNT
                    else MatchParticipantTeam.TEAM_B
                )
                slot = (index % TEAM_PLAYER_COUNT) + 1
                participant = MatchParticipant(
                    match_id=match.id,
                    player_id=queue_entry.player_id,
                    queue_entry_id=queue_entry.id,
                    team=team,
                    slot=slot,
                    created_at=current_time,
                )
                session.add(participant)
                queue_entry.status = MatchQueueEntryStatus.MATCHED

            session.flush()
            for payload in self._build_match_created_payloads(match.id, queue_entries):
                destination = payload["destination"]
                channel_id = destination["channel_id"]
                self._enqueue_outbox_event(
                    session,
                    event_type=OutboxEventType.MATCH_CREATED,
                    dedupe_key=f"match_created:{match.id}:{channel_id}",
                    payload=payload,
                )

            created_match = CreatedMatchResult(
                match_id=match.id,
                queue_entry_ids=tuple(queue_entry.id for queue_entry in queue_entries),
                player_ids=tuple(queue_entry.player_id for queue_entry in queue_entries),
            )

        if created_match is not None:
            self.logger.info(
                "Created match match_id=%s queue_entry_ids=%s",
                created_match.match_id,
                created_match.queue_entry_ids,
            )
        return created_match

    def _build_match_created_payloads(
        self, match_id: int, queue_entries: Sequence[MatchQueueEntry]
    ) -> tuple[dict[str, Any], ...]:
        destinations_by_channel_id: dict[int, NotificationDestinationPayload] = {}
        for queue_entry in queue_entries:
            destination = self._build_notification_destination_payload(
                queue_entry,
                event_context="match_created",
            )
            destinations_by_channel_id.setdefault(destination["channel_id"], destination)

        team_a_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id
            for queue_entry in queue_entries[:TEAM_PLAYER_COUNT]
        ]
        team_b_discord_user_ids = [
            queue_entry.notification_mention_discord_user_id
            for queue_entry in queue_entries[TEAM_PLAYER_COUNT:]
        ]

        return tuple(
            {
                "match_id": match_id,
                "queue_entry_ids": [queue_entry.id for queue_entry in queue_entries],
                "player_ids": [queue_entry.player_id for queue_entry in queue_entries],
                "destination": destination,
                "team_a_discord_user_ids": team_a_discord_user_ids,
                "team_b_discord_user_ids": team_b_discord_user_ids,
            }
            for destination in destinations_by_channel_id.values()
        )

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
            raise PlayerNotRegisteredError(f"Player is not registered: {player_id}")
        return player

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
        entry.notification_mention_discord_user_id = notification_context.mention_discord_user_id
        entry.notification_recorded_at = recorded_at

    def _build_presence_reminder_payload(self, entry: MatchQueueEntry) -> dict[str, Any]:
        return {
            "queue_entry_id": entry.id,
            "player_id": entry.player_id,
            "revision": entry.revision,
            "expire_at": entry.expire_at.isoformat(),
            "destination": self._build_notification_destination_payload(
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
            "destination": self._build_notification_destination_payload(
                entry,
                event_context="queue_expired",
            ),
            "mention_discord_user_id": entry.notification_mention_discord_user_id,
        }

    def _build_notification_destination_payload(
        self,
        entry: MatchQueueEntry,
        *,
        event_context: str,
    ) -> NotificationDestinationPayload:
        if entry.notification_channel_id is None:
            raise ValueError(
                f"notification_channel_id is missing for {event_context} queue_entry_id={entry.id}"
            )
        return {
            "channel_id": entry.notification_channel_id,
            "guild_id": entry.notification_guild_id,
        }
