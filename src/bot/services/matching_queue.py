from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
MATCH_CREATED_NOTIFICATION_MESSAGE = "マッチ成立です。対戦相手とチーム分けを確認してください。"


@dataclass(frozen=True, slots=True)
class PresenceReminderTask:
    queue_entry_id: int
    expected_revision: int
    remind_at: datetime


@dataclass(frozen=True, slots=True)
class ExpireTask:
    queue_entry_id: int
    expected_revision: int
    expire_at: datetime


@dataclass(frozen=True, slots=True)
class JoinQueueResult:
    queue_entry_id: int
    expire_at: datetime
    message: str = JOIN_QUEUE_MESSAGE


@dataclass(frozen=True, slots=True)
class PresentQueueResult:
    queue_entry_id: int
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
class StartupSyncResult:
    cleaned_up_queue_entry_ids: tuple[int, ...]
    reminded_queue_entry_ids: tuple[int, ...]
    rescheduled_reminder_queue_entry_ids: tuple[int, ...]
    rescheduled_expire_queue_entry_ids: tuple[int, ...]
    created_match_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _WaitingEntryTimerState:
    queue_entry_id: int
    revision: int
    expire_at: datetime
    last_reminded_revision: int | None


@dataclass(frozen=True, slots=True)
class MatchingQueueNotificationContext:
    channel_id: int
    guild_id: int | None
    mention_discord_user_id: int


class MatchingQueueTaskScheduler(Protocol):
    def schedule_presence_reminder(self, task: PresenceReminderTask) -> None: ...

    def schedule_expire(self, task: ExpireTask) -> None: ...

    def cancel_presence_reminder(self, queue_entry_id: int) -> None: ...

    def cancel_expire(self, queue_entry_id: int) -> None: ...


class NoopMatchingQueueTaskScheduler:
    def schedule_presence_reminder(self, task: PresenceReminderTask) -> None:
        del task

    def schedule_expire(self, task: ExpireTask) -> None:
        del task

    def cancel_presence_reminder(self, queue_entry_id: int) -> None:
        del queue_entry_id

    def cancel_expire(self, queue_entry_id: int) -> None:
        del queue_entry_id


class MatchingQueueService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        task_scheduler: MatchingQueueTaskScheduler | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.task_scheduler = task_scheduler or NoopMatchingQueueTaskScheduler()
        self.logger = logger or logging.getLogger(__name__)

    def join_queue(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult:
        result: JoinQueueResult | None = None
        scheduled_entry: MatchQueueEntry | None = None

        with session_scope(self.session_factory) as session:
            self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            current_time = self._get_database_now(session)
            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is not None and waiting_entry.expire_at > current_time:
                raise QueueAlreadyJoinedError(QUEUE_ALREADY_JOINED_MESSAGE)

            if waiting_entry is not None and waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    session,
                    waiting_entry,
                    removed_at=current_time,
                    emit_outbox_event=False,
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
                recorded_at=current_time,
            )
            session.add(new_entry)
            session.flush()

            scheduled_entry = new_entry
            result = JoinQueueResult(queue_entry_id=new_entry.id, expire_at=new_entry.expire_at)

        if scheduled_entry is not None:
            self._schedule_waiting_entry(scheduled_entry, replace_existing=False)

        self._try_create_matches_safely(context="join")
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
        scheduled_entry: MatchQueueEntry | None = None
        cancel_queue_entry_id: int | None = None

        with session_scope(self.session_factory) as session:
            self._ensure_player_exists(session, player_id)
            self._acquire_player_lock(session, player_id)
            current_time = self._get_database_now(session)
            waiting_entry = self._get_waiting_entry_for_update(session, player_id)

            if waiting_entry is None:
                raise QueueNotJoinedError(QUEUE_NOT_JOINED_MESSAGE)

            if waiting_entry.expire_at <= current_time:
                self._mark_entry_expired(
                    session,
                    waiting_entry,
                    removed_at=current_time,
                    emit_outbox_event=False,
                )
                cancel_queue_entry_id = waiting_entry.id
                result = PresentQueueResult(
                    queue_entry_id=waiting_entry.id,
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
                    recorded_at=current_time,
                )
                scheduled_entry = waiting_entry
                result = PresentQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expire_at=waiting_entry.expire_at,
                    expired=False,
                    message=QUEUE_PRESENT_UPDATED_MESSAGE,
                )

        if cancel_queue_entry_id is not None:
            self._cancel_queue_entry_tasks(cancel_queue_entry_id)

        if scheduled_entry is not None:
            self._schedule_waiting_entry(scheduled_entry, replace_existing=True)

        if result is None:
            raise RuntimeError("present result was not created")
        return result

    def leave(self, player_id: int) -> LeaveQueueResult:
        result: LeaveQueueResult | None = None
        cancel_queue_entry_id: int | None = None

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
                    session,
                    waiting_entry,
                    removed_at=current_time,
                    emit_outbox_event=False,
                )
                cancel_queue_entry_id = waiting_entry.id
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=True,
                    message=QUEUE_ALREADY_EXPIRED_MESSAGE,
                )
            else:
                waiting_entry.status = MatchQueueEntryStatus.LEFT
                waiting_entry.removed_at = current_time
                waiting_entry.removal_reason = MatchQueueRemovalReason.USER_LEAVE
                cancel_queue_entry_id = waiting_entry.id
                result = LeaveQueueResult(
                    queue_entry_id=waiting_entry.id,
                    expired=False,
                    message=QUEUE_LEFT_MESSAGE,
                )

        if cancel_queue_entry_id is not None:
            self._cancel_queue_entry_tasks(cancel_queue_entry_id)

        if result is None:
            raise RuntimeError("leave result was not created")
        return result

    def process_presence_reminder(
        self, queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        reminded = False

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

        if reminded:
            self.logger.info("Queued presence reminder for queue_entry_id=%s", queue_entry_id)
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=reminded)

    def process_expire(self, queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        expired = False

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
                session,
                entry,
                removed_at=current_time,
                emit_outbox_event=True,
            )
            expired = True

        if expired:
            self._cancel_queue_entry_tasks(queue_entry_id)
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
                        session,
                        entry,
                        removed_at=current_time,
                        emit_outbox_event=True,
                    )
                    batch_ids.append(entry.id)

            for queue_entry_id in batch_ids:
                self._cancel_queue_entry_tasks(queue_entry_id)
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

    def run_startup_sync(self) -> StartupSyncResult:
        return self._run_sync_cycle(warn_on_cleanup=False)

    def run_reconcile_cycle(self) -> StartupSyncResult:
        return self._run_sync_cycle(warn_on_cleanup=True)

    def _run_sync_cycle(self, *, warn_on_cleanup: bool) -> StartupSyncResult:
        cleaned_up_queue_entry_ids = self.cleanup_expired_entries(warn_on_cleanup=warn_on_cleanup)
        created_matches = self.try_create_matches()
        snapshot_time, waiting_entries = self._load_waiting_entry_timer_states()

        reminded_queue_entry_ids: list[int] = []
        rescheduled_reminder_queue_entry_ids: list[int] = []
        rescheduled_expire_queue_entry_ids: list[int] = []

        for waiting_entry in waiting_entries:
            remind_at = waiting_entry.expire_at - PRESENCE_REMINDER_LEAD_TIME
            already_reminded = waiting_entry.last_reminded_revision == waiting_entry.revision

            if not already_reminded and remind_at <= snapshot_time < waiting_entry.expire_at:
                reminder_result = self.process_presence_reminder(
                    waiting_entry.queue_entry_id,
                    waiting_entry.revision,
                )
                if reminder_result.reminded:
                    reminded_queue_entry_ids.append(waiting_entry.queue_entry_id)
            elif not already_reminded and snapshot_time < remind_at:
                reminder_task = PresenceReminderTask(
                    queue_entry_id=waiting_entry.queue_entry_id,
                    expected_revision=waiting_entry.revision,
                    remind_at=remind_at,
                )
                if self._schedule_presence_reminder_task(reminder_task):
                    rescheduled_reminder_queue_entry_ids.append(waiting_entry.queue_entry_id)

            expire_task = ExpireTask(
                queue_entry_id=waiting_entry.queue_entry_id,
                expected_revision=waiting_entry.revision,
                expire_at=waiting_entry.expire_at,
            )
            if self._schedule_expire_task(expire_task):
                rescheduled_expire_queue_entry_ids.append(waiting_entry.queue_entry_id)

        return StartupSyncResult(
            cleaned_up_queue_entry_ids=cleaned_up_queue_entry_ids,
            reminded_queue_entry_ids=tuple(reminded_queue_entry_ids),
            rescheduled_reminder_queue_entry_ids=tuple(rescheduled_reminder_queue_entry_ids),
            rescheduled_expire_queue_entry_ids=tuple(rescheduled_expire_queue_entry_ids),
            created_match_ids=tuple(match.match_id for match in created_matches),
        )

    def _try_create_single_match(self) -> CreatedMatchResult | None:
        created_match: CreatedMatchResult | None = None
        matched_queue_entry_ids: list[int] = []

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
                matched_queue_entry_ids.append(queue_entry.id)

            session.flush()
            self._enqueue_outbox_event(
                session,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key=f"match_created:{match.id}",
                payload=self._build_match_created_payload(match.id, queue_entries),
            )

            created_match = CreatedMatchResult(
                match_id=match.id,
                queue_entry_ids=tuple(queue_entry.id for queue_entry in queue_entries),
                player_ids=tuple(queue_entry.player_id for queue_entry in queue_entries),
            )

        for queue_entry_id in matched_queue_entry_ids:
            self._cancel_queue_entry_tasks(queue_entry_id)

        if created_match is not None:
            self.logger.info(
                "Created match match_id=%s queue_entry_ids=%s",
                created_match.match_id,
                created_match.queue_entry_ids,
            )
        return created_match

    def _build_match_created_payload(
        self, match_id: int, queue_entries: Sequence[MatchQueueEntry]
    ) -> dict[str, Any]:
        team_a_player_ids = [
            queue_entry.player_id for queue_entry in queue_entries[:TEAM_PLAYER_COUNT]
        ]
        team_b_player_ids = [
            queue_entry.player_id for queue_entry in queue_entries[TEAM_PLAYER_COUNT:]
        ]
        return {
            "match_id": match_id,
            "queue_entry_ids": [queue_entry.id for queue_entry in queue_entries],
            "player_ids": [queue_entry.player_id for queue_entry in queue_entries],
            "teams": {
                MatchParticipantTeam.TEAM_A.value: team_a_player_ids,
                MatchParticipantTeam.TEAM_B.value: team_b_player_ids,
            },
        }

    def _load_waiting_entry_timer_states(
        self,
    ) -> tuple[datetime, tuple[_WaitingEntryTimerState, ...]]:
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
            _WaitingEntryTimerState(
                queue_entry_id=row.id,
                revision=row.revision,
                expire_at=row.expire_at,
                last_reminded_revision=row.last_reminded_revision,
            )
            for row in rows
        )
        return current_time, states

    def _ensure_player_exists(self, session: Session, player_id: int) -> None:
        player = session.get(Player, player_id)
        if player is None:
            raise PlayerNotRegisteredError(f"Player is not registered: {player_id}")

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
        session: Session,
        entry: MatchQueueEntry,
        *,
        removed_at: datetime,
        emit_outbox_event: bool,
    ) -> None:
        entry.status = MatchQueueEntryStatus.EXPIRED
        entry.removed_at = removed_at
        entry.removal_reason = MatchQueueRemovalReason.TIMEOUT

        if not emit_outbox_event:
            return

        self._enqueue_outbox_event(
            session,
            event_type=OutboxEventType.QUEUE_EXPIRED,
            dedupe_key=f"queue_expired:{entry.id}:{entry.revision}",
            payload=self._build_queue_expired_payload(entry),
        )

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

    def _schedule_waiting_entry(self, entry: MatchQueueEntry, *, replace_existing: bool) -> None:
        if replace_existing:
            self._cancel_queue_entry_tasks(entry.id)

        self._schedule_presence_reminder_task(
            PresenceReminderTask(
                queue_entry_id=entry.id,
                expected_revision=entry.revision,
                remind_at=entry.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            )
        )
        self._schedule_expire_task(
            ExpireTask(
                queue_entry_id=entry.id,
                expected_revision=entry.revision,
                expire_at=entry.expire_at,
            )
        )

    def _schedule_presence_reminder_task(self, task: PresenceReminderTask) -> bool:
        try:
            self.task_scheduler.schedule_presence_reminder(task)
        except Exception:
            self.logger.exception(
                "Failed to schedule presence reminder queue_entry_id=%s revision=%s",
                task.queue_entry_id,
                task.expected_revision,
            )
            return False
        return True

    def _schedule_expire_task(self, task: ExpireTask) -> bool:
        try:
            self.task_scheduler.schedule_expire(task)
        except Exception:
            self.logger.exception(
                "Failed to schedule expire queue_entry_id=%s revision=%s",
                task.queue_entry_id,
                task.expected_revision,
            )
            return False
        return True

    def _cancel_queue_entry_tasks(self, queue_entry_id: int) -> None:
        try:
            self.task_scheduler.cancel_presence_reminder(queue_entry_id)
        except Exception:
            self.logger.exception(
                "Failed to cancel presence reminder queue_entry_id=%s",
                queue_entry_id,
            )

        try:
            self.task_scheduler.cancel_expire(queue_entry_id)
        except Exception:
            self.logger.exception("Failed to cancel expire queue_entry_id=%s", queue_entry_id)

    def _try_create_matches_safely(self, *, context: str) -> None:
        try:
            self.try_create_matches()
        except Exception:
            self.logger.exception("Failed to try_create_matches after %s", context)

    def _apply_notification_context(
        self,
        entry: MatchQueueEntry,
        notification_context: MatchingQueueNotificationContext | None,
        *,
        recorded_at: datetime,
    ) -> None:
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
        }

    def _build_queue_expired_payload(self, entry: MatchQueueEntry) -> dict[str, Any]:
        return {
            "queue_entry_id": entry.id,
            "player_id": entry.player_id,
            "revision": entry.revision,
            "expire_at": entry.expire_at.isoformat(),
        }
