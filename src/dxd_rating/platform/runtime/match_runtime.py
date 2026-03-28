from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ParamSpec, Protocol, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import RetryableTaskError
from dxd_rating.contexts.matches.application import (
    ActiveMatchTimerState,
    MatchAdminOverrideResult,
    MatchApprovalResult,
    MatchFinalizationResult,
    MatchFlowService,
    MatchParentAssignmentResult,
    MatchReportSubmissionResult,
    MatchSpectateResult,
    PlayerPenaltyAdjustmentResult,
)
from dxd_rating.contexts.matchmaking.application import (
    CreatedMatchResult,
    ExpireQueueEntryResult,
    JoinQueueResult,
    LeaveQueueResult,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    PresenceReminderResult,
    PresentQueueResult,
    WaitingEntryTimerState,
)
from dxd_rating.platform.db.models import (
    MatchFormat,
    MatchReportInputResult,
    MatchResult,
    MatchState,
    PenaltyType,
)
from dxd_rating.platform.runtime.outbox import retry_delay_for_failure_count
from dxd_rating.shared.constants import (
    MATCH_PARENT_SELECTION_WINDOW,
    MATCH_QUEUE_TTL,
    PRESENCE_REMINDER_LEAD_TIME,
)

DEFAULT_RECONCILE_INTERVAL = timedelta(minutes=5)
P = ParamSpec("P")
R = TypeVar("R")


class MatchRuntimeService(Protocol):
    def join_queue(
        self,
        player_id: int,
        match_format: MatchFormat | str,
        queue_name: str,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult: ...

    def update_waiting_notification_context(
        self,
        queue_entry_id: int,
        notification_context: MatchingQueueNotificationContext,
    ) -> bool: ...

    def get_waiting_entry_notification_channel_id(self, player_id: int) -> int | None: ...

    def present(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> PresentQueueResult: ...

    def leave(self, player_id: int) -> LeaveQueueResult: ...

    def process_presence_reminder(
        self, queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult: ...

    def process_expire(
        self, queue_entry_id: int, expected_revision: int
    ) -> ExpireQueueEntryResult: ...

    def cleanup_expired_entries(
        self,
        *,
        batch_size: int = ...,
        warn_on_cleanup: bool = ...,
    ) -> tuple[int, ...]: ...

    def try_create_matches(
        self,
        queue_class_id: str | None = None,
    ) -> tuple[CreatedMatchResult, ...]: ...

    def load_waiting_entry_timer_states(
        self,
    ) -> tuple[datetime, tuple[WaitingEntryTimerState, ...]]: ...


class MatchFlowRuntimeService(Protocol):
    def volunteer_parent(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchParentAssignmentResult: ...

    def spectate_match(
        self,
        match_id: int,
        player_id: int,
    ) -> MatchSpectateResult: ...

    def submit_report(
        self,
        match_id: int,
        player_id: int,
        input_result: MatchReportInputResult,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchReportSubmissionResult: ...

    def approve_provisional_result(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchApprovalResult: ...

    def process_parent_deadline(self, match_id: int) -> MatchParentAssignmentResult: ...

    def process_report_open(self, match_id: int) -> bool: ...

    def process_report_deadline(self, match_id: int) -> MatchFinalizationResult: ...

    def process_approval_deadline(self, match_id: int) -> MatchFinalizationResult: ...

    def override_match_result(
        self,
        match_id: int,
        final_result: MatchResult,
        *,
        admin_discord_user_id: int,
    ) -> MatchAdminOverrideResult: ...

    def adjust_penalty(
        self,
        player_id: int,
        penalty_type: PenaltyType,
        delta: int,
        *,
        admin_discord_user_id: int,
    ) -> PlayerPenaltyAdjustmentResult: ...

    def load_active_match_timer_states(
        self,
    ) -> tuple[datetime, tuple[ActiveMatchTimerState, ...]]: ...


@dataclass(frozen=True, slots=True)
class MatchRuntimeSyncResult:
    cleaned_up_queue_entry_ids: tuple[int, ...]
    reminded_queue_entry_ids: tuple[int, ...]
    rescheduled_reminder_queue_entry_ids: tuple[int, ...]
    rescheduled_expire_queue_entry_ids: tuple[int, ...]
    created_match_ids: tuple[int, ...]
    auto_assigned_parent_match_ids: tuple[int, ...] = tuple()
    opened_report_match_ids: tuple[int, ...] = tuple()
    started_approval_match_ids: tuple[int, ...] = tuple()
    finalized_match_ids: tuple[int, ...] = tuple()
    rescheduled_parent_deadline_match_ids: tuple[int, ...] = tuple()
    rescheduled_report_open_match_ids: tuple[int, ...] = tuple()
    rescheduled_report_deadline_match_ids: tuple[int, ...] = tuple()
    rescheduled_approval_deadline_match_ids: tuple[int, ...] = tuple()


class ScheduledTaskKind(StrEnum):
    PRESENCE_REMINDER = "presence-reminder"
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class ScheduledTaskKey:
    queue_entry_id: int
    kind: ScheduledTaskKind


class MatchScheduledTaskKind(StrEnum):
    PARENT_DEADLINE = "parent-deadline"
    REPORT_OPEN = "report-open"
    REPORT_DEADLINE = "report-deadline"
    APPROVAL_DEADLINE = "approval-deadline"


@dataclass(frozen=True, slots=True)
class MatchScheduledTaskKey:
    match_id: int
    kind: MatchScheduledTaskKind


class MatchRuntime:
    def __init__(
        self,
        service: MatchRuntimeService,
        *,
        match_service: MatchFlowRuntimeService | None = None,
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.match_service = match_service
        self.reconcile_interval = reconcile_interval
        self.logger = logger or logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled_tasks: dict[ScheduledTaskKey, asyncio.Task[None]] = {}
        self._scheduled_match_tasks: dict[MatchScheduledTaskKey, asyncio.Task[None]] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._database_clock_offset = timedelta()
        self._closed = False
        self._state_lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        session_factory: sessionmaker[Session],
        *,
        admin_discord_user_ids: frozenset[int] = frozenset(),
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> MatchRuntime:
        queue_service = MatchingQueueService(
            session_factory=session_factory,
            logger=logger,
        )
        match_service = MatchFlowService(
            session_factory=session_factory,
            admin_discord_user_ids=admin_discord_user_ids,
            logger=logger,
        )
        return cls(
            service=queue_service,
            match_service=match_service,
            reconcile_interval=reconcile_interval,
            logger=logger,
        )

    async def join_queue(
        self,
        player_id: int,
        match_format: MatchFormat | str,
        queue_name: str,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            self.service.join_queue,
            player_id,
            match_format,
            queue_name,
            notification_context=notification_context,
        )
        self._observe_database_time(result.expire_at - MATCH_QUEUE_TTL)
        self._schedule_task(
            key=self._presence_reminder_task_key(result.queue_entry_id),
            task_name="presence reminder",
            scheduled_at=result.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            deadline=result.expire_at,
            handler_call=self._handler_call(
                self.process_presence_reminder,
                result.queue_entry_id,
                result.revision,
            ),
        )
        self._schedule_task(
            key=self._expire_task_key(result.queue_entry_id),
            task_name="expire",
            scheduled_at=result.expire_at,
            deadline=None,
            handler_call=self._handler_call(
                self.process_expire,
                result.queue_entry_id,
                result.revision,
            ),
        )
        await self._try_create_matches_safely(
            context="join",
            queue_class_id=result.queue_class_id,
        )
        return result

    async def present(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> PresentQueueResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            self.service.present,
            player_id,
            notification_context=notification_context,
        )
        if result.expired:
            self._cancel_scheduled_task(self._presence_reminder_task_key(result.queue_entry_id))
            self._cancel_scheduled_task(self._expire_task_key(result.queue_entry_id))
            return result

        if result.revision is None or result.expire_at is None:
            raise RuntimeError(
                "present result for waiting entry must include revision and expire_at"
            )

        self._observe_database_time(result.expire_at - MATCH_QUEUE_TTL)
        self._cancel_scheduled_task(self._presence_reminder_task_key(result.queue_entry_id))
        self._cancel_scheduled_task(self._expire_task_key(result.queue_entry_id))
        self._schedule_task(
            key=self._presence_reminder_task_key(result.queue_entry_id),
            task_name="presence reminder",
            scheduled_at=result.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            deadline=result.expire_at,
            handler_call=self._handler_call(
                self.process_presence_reminder,
                result.queue_entry_id,
                result.revision,
            ),
        )
        self._schedule_task(
            key=self._expire_task_key(result.queue_entry_id),
            task_name="expire",
            scheduled_at=result.expire_at,
            deadline=None,
            handler_call=self._handler_call(
                self.process_expire,
                result.queue_entry_id,
                result.revision,
            ),
        )
        return result

    async def update_waiting_notification_context(
        self,
        queue_entry_id: int,
        notification_context: MatchingQueueNotificationContext,
    ) -> bool:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        return await asyncio.to_thread(
            self.service.update_waiting_notification_context,
            queue_entry_id,
            notification_context,
        )

    async def get_waiting_entry_notification_channel_id(self, player_id: int) -> int | None:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        return await asyncio.to_thread(
            self.service.get_waiting_entry_notification_channel_id,
            player_id,
        )

    async def leave(self, player_id: int) -> LeaveQueueResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(self.service.leave, player_id)
        if result.queue_entry_id is not None:
            self._cancel_scheduled_task(self._presence_reminder_task_key(result.queue_entry_id))
            self._cancel_scheduled_task(self._expire_task_key(result.queue_entry_id))
        return result

    async def volunteer_parent(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchParentAssignmentResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            match_service.volunteer_parent,
            match_id,
            player_id,
            notification_context=notification_context,
        )
        if result.assigned:
            if result.finalized:
                self._cancel_all_match_tasks(match_id)
            elif result.approval_deadline_at is not None:
                self._cancel_match_task(self._parent_deadline_task_key(match_id))
                self._schedule_match_approval_task(match_id, result.approval_deadline_at)
            else:
                self._cancel_match_task(self._parent_deadline_task_key(match_id))
                self._schedule_match_reporting_tasks(
                    match_id=match_id,
                    report_open_at=result.report_open_at,
                    report_deadline_at=result.report_deadline_at,
                )
        return result

    async def spectate_match(
        self,
        match_id: int,
        player_id: int,
    ) -> MatchSpectateResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        return await asyncio.to_thread(
            match_service.spectate_match,
            match_id,
            player_id,
        )

    async def submit_match_report(
        self,
        match_id: int,
        player_id: int,
        input_result: MatchReportInputResult,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchReportSubmissionResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            match_service.submit_report,
            match_id,
            player_id,
            input_result,
            notification_context=notification_context,
        )
        if result.finalized:
            self._cancel_all_match_tasks(match_id)
        elif result.approval_started:
            self._cancel_match_task(self._report_deadline_task_key(match_id))
            self._schedule_match_approval_task(match_id, result.approval_deadline_at)
        return result

    async def approve_match_result(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchApprovalResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            match_service.approve_provisional_result,
            match_id,
            player_id,
            notification_context=notification_context,
        )
        if result.finalized:
            self._cancel_all_match_tasks(match_id)
        return result

    async def admin_override_match_result(
        self,
        match_id: int,
        final_result: MatchResult,
        *,
        admin_discord_user_id: int,
    ) -> MatchAdminOverrideResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            match_service.override_match_result,
            match_id,
            final_result,
            admin_discord_user_id=admin_discord_user_id,
        )
        self._cancel_all_match_tasks(match_id)
        return result

    async def adjust_penalty(
        self,
        player_id: int,
        penalty_type: PenaltyType,
        delta: int,
        *,
        admin_discord_user_id: int,
    ) -> PlayerPenaltyAdjustmentResult:
        match_service = self._require_match_service()
        self._ensure_open()
        self._bind_current_loop_if_needed()
        return await asyncio.to_thread(
            match_service.adjust_penalty,
            player_id,
            penalty_type,
            delta,
            admin_discord_user_id=admin_discord_user_id,
        )

    async def process_presence_reminder(
        self, queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        return await asyncio.to_thread(
            self.service.process_presence_reminder,
            queue_entry_id,
            expected_revision,
        )

    async def process_expire(
        self, queue_entry_id: int, expected_revision: int
    ) -> ExpireQueueEntryResult:
        result = await asyncio.to_thread(
            self.service.process_expire,
            queue_entry_id,
            expected_revision,
        )
        if result.expired:
            self._cancel_scheduled_task(self._presence_reminder_task_key(queue_entry_id))
            self._cancel_scheduled_task(self._expire_task_key(queue_entry_id))
        return result

    async def process_parent_deadline(self, match_id: int) -> MatchParentAssignmentResult:
        match_service = self._require_match_service()
        result = await asyncio.to_thread(match_service.process_parent_deadline, match_id)
        if result.assigned:
            if result.finalized:
                self._cancel_all_match_tasks(match_id)
            elif result.approval_deadline_at is not None:
                self._cancel_match_task(self._parent_deadline_task_key(match_id))
                self._schedule_match_approval_task(match_id, result.approval_deadline_at)
            else:
                self._cancel_match_task(self._parent_deadline_task_key(match_id))
                self._schedule_match_reporting_tasks(
                    match_id=match_id,
                    report_open_at=result.report_open_at,
                    report_deadline_at=result.report_deadline_at,
                )
        return result

    async def process_report_open(self, match_id: int) -> bool:
        match_service = self._require_match_service()
        return await asyncio.to_thread(match_service.process_report_open, match_id)

    async def process_report_deadline(self, match_id: int) -> MatchFinalizationResult:
        match_service = self._require_match_service()
        result = await asyncio.to_thread(match_service.process_report_deadline, match_id)
        if result.finalized:
            self._cancel_all_match_tasks(match_id)
        elif result.approval_deadline_at is not None:
            self._cancel_match_task(self._report_deadline_task_key(match_id))
            self._schedule_match_approval_task(match_id, result.approval_deadline_at)
        return result

    async def process_approval_deadline(self, match_id: int) -> MatchFinalizationResult:
        match_service = self._require_match_service()
        result = await asyncio.to_thread(match_service.process_approval_deadline, match_id)
        if result.finalized:
            self._cancel_all_match_tasks(match_id)
        return result

    async def start(self) -> MatchRuntimeSyncResult:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("MatchRuntime is already closed")
            if self._reconcile_task is not None:
                raise RuntimeError("MatchRuntime is already started")

            loop = asyncio.get_running_loop()
            self.bind_loop(loop)
            startup_result = await self.run_startup_sync()
            self._reconcile_task = asyncio.create_task(
                self._run_reconcile_loop(),
                name="match-runtime-reconcile",
            )
            return startup_result

    async def stop(self) -> None:
        async with self._state_lock:
            self._closed = True
            reconcile_task = self._reconcile_task
            self._reconcile_task = None

        if reconcile_task is not None:
            reconcile_task.cancel()
            await asyncio.gather(reconcile_task, return_exceptions=True)

        await self._aclose_scheduled_tasks()

    async def run_startup_sync(self) -> MatchRuntimeSyncResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await self._run_sync_cycle(False)
        self._log_sync_result("Startup sync", result)
        return result

    async def run_reconcile_cycle(self) -> MatchRuntimeSyncResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await self._run_sync_cycle(True)
        if self._has_sync_activity(result):
            self._log_sync_result("Reconcile cycle", result)
        return result

    async def _run_reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.reconcile_interval.total_seconds())
            except asyncio.CancelledError:
                raise

            try:
                await self.run_reconcile_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Match runtime reconcile cycle failed")

    async def _run_sync_cycle(self, warn_on_cleanup: bool) -> MatchRuntimeSyncResult:
        cleaned_up_queue_entry_ids = await asyncio.to_thread(
            self.service.cleanup_expired_entries,
            warn_on_cleanup=warn_on_cleanup,
        )
        for queue_entry_id in cleaned_up_queue_entry_ids:
            self._cancel_scheduled_task(self._presence_reminder_task_key(queue_entry_id))
            self._cancel_scheduled_task(self._expire_task_key(queue_entry_id))

        created_matches = await self._try_create_matches()
        snapshot_time, waiting_entries = await asyncio.to_thread(
            self.service.load_waiting_entry_timer_states,
        )
        self._observe_database_time(snapshot_time)

        reminded_queue_entry_ids: list[int] = []
        rescheduled_reminder_queue_entry_ids: list[int] = []
        rescheduled_expire_queue_entry_ids: list[int] = []

        for waiting_entry in waiting_entries:
            remind_at = waiting_entry.expire_at - PRESENCE_REMINDER_LEAD_TIME
            already_reminded = waiting_entry.last_reminded_revision == waiting_entry.revision

            if not already_reminded and remind_at <= snapshot_time < waiting_entry.expire_at:
                reminder_result = await self.process_presence_reminder(
                    waiting_entry.queue_entry_id,
                    waiting_entry.revision,
                )
                if reminder_result.reminded:
                    reminded_queue_entry_ids.append(waiting_entry.queue_entry_id)
            elif not already_reminded and snapshot_time < remind_at:
                self._cancel_scheduled_task(
                    self._presence_reminder_task_key(waiting_entry.queue_entry_id)
                )
                if self._schedule_task(
                    key=self._presence_reminder_task_key(waiting_entry.queue_entry_id),
                    task_name="presence reminder",
                    scheduled_at=remind_at,
                    deadline=waiting_entry.expire_at,
                    handler_call=self._handler_call(
                        self.process_presence_reminder,
                        waiting_entry.queue_entry_id,
                        waiting_entry.revision,
                    ),
                ):
                    rescheduled_reminder_queue_entry_ids.append(waiting_entry.queue_entry_id)

            self._cancel_scheduled_task(self._expire_task_key(waiting_entry.queue_entry_id))
            if self._schedule_task(
                key=self._expire_task_key(waiting_entry.queue_entry_id),
                task_name="expire",
                scheduled_at=waiting_entry.expire_at,
                deadline=None,
                handler_call=self._handler_call(
                    self.process_expire,
                    waiting_entry.queue_entry_id,
                    waiting_entry.revision,
                ),
            ):
                rescheduled_expire_queue_entry_ids.append(waiting_entry.queue_entry_id)

        match_sync_result = await self._sync_active_matches()
        return MatchRuntimeSyncResult(
            cleaned_up_queue_entry_ids=cleaned_up_queue_entry_ids,
            reminded_queue_entry_ids=tuple(reminded_queue_entry_ids),
            rescheduled_reminder_queue_entry_ids=tuple(rescheduled_reminder_queue_entry_ids),
            rescheduled_expire_queue_entry_ids=tuple(rescheduled_expire_queue_entry_ids),
            created_match_ids=tuple(match.match_id for match in created_matches),
            auto_assigned_parent_match_ids=match_sync_result.auto_assigned_parent_match_ids,
            opened_report_match_ids=match_sync_result.opened_report_match_ids,
            started_approval_match_ids=match_sync_result.started_approval_match_ids,
            finalized_match_ids=match_sync_result.finalized_match_ids,
            rescheduled_parent_deadline_match_ids=match_sync_result.rescheduled_parent_deadline_match_ids,
            rescheduled_report_open_match_ids=match_sync_result.rescheduled_report_open_match_ids,
            rescheduled_report_deadline_match_ids=match_sync_result.rescheduled_report_deadline_match_ids,
            rescheduled_approval_deadline_match_ids=match_sync_result.rescheduled_approval_deadline_match_ids,
        )

    async def _sync_active_matches(self) -> MatchRuntimeSyncResult:
        if self.match_service is None:
            return MatchRuntimeSyncResult(
                cleaned_up_queue_entry_ids=tuple(),
                reminded_queue_entry_ids=tuple(),
                rescheduled_reminder_queue_entry_ids=tuple(),
                rescheduled_expire_queue_entry_ids=tuple(),
                created_match_ids=tuple(),
            )

        snapshot_time, active_matches = await asyncio.to_thread(
            self.match_service.load_active_match_timer_states
        )
        self._observe_database_time(snapshot_time)
        auto_assigned_parent_match_ids: list[int] = []
        opened_report_match_ids: list[int] = []
        started_approval_match_ids: list[int] = []
        finalized_match_ids: list[int] = []
        rescheduled_parent_deadline_match_ids: list[int] = []
        rescheduled_report_open_match_ids: list[int] = []
        rescheduled_report_deadline_match_ids: list[int] = []
        rescheduled_approval_deadline_match_ids: list[int] = []

        for active_match in active_matches:
            if active_match.state == MatchState.WAITING_FOR_PARENT:
                if active_match.parent_deadline_at <= snapshot_time:
                    result = await self.process_parent_deadline(active_match.match_id)
                    if result.assigned:
                        auto_assigned_parent_match_ids.append(active_match.match_id)
                else:
                    self._cancel_match_task(self._parent_deadline_task_key(active_match.match_id))
                    if self._schedule_match_task(
                        key=self._parent_deadline_task_key(active_match.match_id),
                        task_name="match parent deadline",
                        scheduled_at=active_match.parent_deadline_at,
                        deadline=None,
                        handler_call=self._handler_call(
                            self.process_parent_deadline,
                            active_match.match_id,
                        ),
                    ):
                        rescheduled_parent_deadline_match_ids.append(active_match.match_id)
                continue

            if active_match.state == MatchState.WAITING_FOR_RESULT_REPORTS:
                if (
                    active_match.report_open_at is not None
                    and active_match.reporting_opened_at is None
                    and active_match.report_open_at <= snapshot_time
                ):
                    if await self.process_report_open(active_match.match_id):
                        opened_report_match_ids.append(active_match.match_id)
                elif (
                    active_match.report_open_at is not None
                    and active_match.reporting_opened_at is None
                ):
                    self._cancel_match_task(self._report_open_task_key(active_match.match_id))
                    if self._schedule_match_task(
                        key=self._report_open_task_key(active_match.match_id),
                        task_name="match report open",
                        scheduled_at=active_match.report_open_at,
                        deadline=active_match.report_deadline_at,
                        handler_call=self._handler_call(
                            self.process_report_open,
                            active_match.match_id,
                        ),
                    ):
                        rescheduled_report_open_match_ids.append(active_match.match_id)

                if active_match.report_deadline_at is not None and (
                    active_match.report_deadline_at <= snapshot_time
                ):
                    report_deadline_result = await self.process_report_deadline(
                        active_match.match_id
                    )
                    if report_deadline_result.finalized:
                        finalized_match_ids.append(active_match.match_id)
                    elif report_deadline_result.approval_deadline_at is not None:
                        started_approval_match_ids.append(active_match.match_id)
                elif active_match.report_deadline_at is not None:
                    self._cancel_match_task(self._report_deadline_task_key(active_match.match_id))
                    if self._schedule_match_task(
                        key=self._report_deadline_task_key(active_match.match_id),
                        task_name="match report deadline",
                        scheduled_at=active_match.report_deadline_at,
                        deadline=None,
                        handler_call=self._handler_call(
                            self.process_report_deadline,
                            active_match.match_id,
                        ),
                    ):
                        rescheduled_report_deadline_match_ids.append(active_match.match_id)
                continue

            if active_match.state == MatchState.AWAITING_RESULT_APPROVALS:
                if (
                    active_match.approval_deadline_at is not None
                    and active_match.approval_deadline_at <= snapshot_time
                ):
                    approval_deadline_result = await self.process_approval_deadline(
                        active_match.match_id
                    )
                    if approval_deadline_result.finalized:
                        finalized_match_ids.append(active_match.match_id)
                elif active_match.approval_deadline_at is not None:
                    self._cancel_match_task(self._approval_deadline_task_key(active_match.match_id))
                    if self._schedule_match_task(
                        key=self._approval_deadline_task_key(active_match.match_id),
                        task_name="match approval deadline",
                        scheduled_at=active_match.approval_deadline_at,
                        deadline=None,
                        handler_call=self._handler_call(
                            self.process_approval_deadline,
                            active_match.match_id,
                        ),
                    ):
                        rescheduled_approval_deadline_match_ids.append(active_match.match_id)

        return MatchRuntimeSyncResult(
            cleaned_up_queue_entry_ids=tuple(),
            reminded_queue_entry_ids=tuple(),
            rescheduled_reminder_queue_entry_ids=tuple(),
            rescheduled_expire_queue_entry_ids=tuple(),
            created_match_ids=tuple(),
            auto_assigned_parent_match_ids=tuple(auto_assigned_parent_match_ids),
            opened_report_match_ids=tuple(opened_report_match_ids),
            started_approval_match_ids=tuple(started_approval_match_ids),
            finalized_match_ids=tuple(finalized_match_ids),
            rescheduled_parent_deadline_match_ids=tuple(rescheduled_parent_deadline_match_ids),
            rescheduled_report_open_match_ids=tuple(rescheduled_report_open_match_ids),
            rescheduled_report_deadline_match_ids=tuple(rescheduled_report_deadline_match_ids),
            rescheduled_approval_deadline_match_ids=tuple(rescheduled_approval_deadline_match_ids),
        )

    async def _try_create_matches(
        self,
        queue_class_id: str | None = None,
    ) -> tuple[CreatedMatchResult, ...]:
        if queue_class_id is None:
            created_matches = await asyncio.to_thread(self.service.try_create_matches)
        else:
            created_matches = await asyncio.to_thread(
                self.service.try_create_matches,
                queue_class_id,
            )
        for created_match in created_matches:
            for queue_entry_id in created_match.queue_entry_ids:
                self._cancel_scheduled_task(self._presence_reminder_task_key(queue_entry_id))
                self._cancel_scheduled_task(self._expire_task_key(queue_entry_id))
            if created_match.created_at is not None:
                self._cancel_match_task(self._parent_deadline_task_key(created_match.match_id))
                self._schedule_match_task(
                    key=self._parent_deadline_task_key(created_match.match_id),
                    task_name="match parent deadline",
                    scheduled_at=created_match.created_at + MATCH_PARENT_SELECTION_WINDOW,
                    deadline=None,
                    handler_call=self._handler_call(
                        self.process_parent_deadline,
                        created_match.match_id,
                    ),
                )
        return created_matches

    async def _try_create_matches_safely(
        self,
        *,
        context: str,
        queue_class_id: str | None = None,
    ) -> tuple[CreatedMatchResult, ...]:
        try:
            return await self._try_create_matches(queue_class_id)
        except Exception:
            self.logger.exception("Failed to try_create_matches after %s", context)
            return tuple()

    def _schedule_task(
        self,
        *,
        key: ScheduledTaskKey,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[Any]],
    ) -> bool:
        try:
            self._require_running_on_bound_loop()
            current_task = self._scheduled_tasks.pop(key, None)
            self._cancel_task(current_task)
            self._scheduled_tasks[key] = asyncio.create_task(
                self._run_scheduled_task(
                    key=key,
                    task_name=task_name,
                    scheduled_at=scheduled_at,
                    deadline=deadline,
                    handler_call=handler_call,
                ),
                name=f"{key.kind.value}-{key.queue_entry_id}",
            )
        except Exception:
            self.logger.exception(
                "Failed to schedule %s queue_entry_id=%s scheduled_at=%s",
                task_name,
                key.queue_entry_id,
                scheduled_at.isoformat(),
            )
            return False
        return True

    def _schedule_match_task(
        self,
        *,
        key: MatchScheduledTaskKey,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[Any]],
    ) -> bool:
        try:
            self._require_running_on_bound_loop()
            current_task = self._scheduled_match_tasks.pop(key, None)
            self._cancel_task(current_task)
            self._scheduled_match_tasks[key] = asyncio.create_task(
                self._run_match_scheduled_task(
                    key=key,
                    task_name=task_name,
                    scheduled_at=scheduled_at,
                    deadline=deadline,
                    handler_call=handler_call,
                ),
                name=f"{key.kind.value}-{key.match_id}",
            )
        except Exception:
            self.logger.exception(
                "Failed to schedule %s match_id=%s scheduled_at=%s",
                task_name,
                key.match_id,
                scheduled_at.isoformat(),
            )
            return False
        return True

    def _handler_call(
        self,
        handler: Callable[P, Awaitable[R]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Callable[[], Awaitable[R]]:
        async def call_handler() -> R:
            return await handler(*args, **kwargs)

        return call_handler

    def _cancel_scheduled_task(self, key: ScheduledTaskKey) -> None:
        self._require_running_on_bound_loop()
        self._cancel_task(self._scheduled_tasks.pop(key, None))

    def _cancel_match_task(self, key: MatchScheduledTaskKey) -> None:
        self._require_running_on_bound_loop()
        self._cancel_task(self._scheduled_match_tasks.pop(key, None))

    def _cancel_all_match_tasks(self, match_id: int) -> None:
        self._cancel_match_task(self._parent_deadline_task_key(match_id))
        self._cancel_match_task(self._report_open_task_key(match_id))
        self._cancel_match_task(self._report_deadline_task_key(match_id))
        self._cancel_match_task(self._approval_deadline_task_key(match_id))

    def _presence_reminder_task_key(self, queue_entry_id: int) -> ScheduledTaskKey:
        return ScheduledTaskKey(
            queue_entry_id=queue_entry_id,
            kind=ScheduledTaskKind.PRESENCE_REMINDER,
        )

    def _expire_task_key(self, queue_entry_id: int) -> ScheduledTaskKey:
        return ScheduledTaskKey(
            queue_entry_id=queue_entry_id,
            kind=ScheduledTaskKind.EXPIRE,
        )

    def _parent_deadline_task_key(self, match_id: int) -> MatchScheduledTaskKey:
        return MatchScheduledTaskKey(
            match_id=match_id,
            kind=MatchScheduledTaskKind.PARENT_DEADLINE,
        )

    def _report_open_task_key(self, match_id: int) -> MatchScheduledTaskKey:
        return MatchScheduledTaskKey(
            match_id=match_id,
            kind=MatchScheduledTaskKind.REPORT_OPEN,
        )

    def _report_deadline_task_key(self, match_id: int) -> MatchScheduledTaskKey:
        return MatchScheduledTaskKey(
            match_id=match_id,
            kind=MatchScheduledTaskKind.REPORT_DEADLINE,
        )

    def _approval_deadline_task_key(self, match_id: int) -> MatchScheduledTaskKey:
        return MatchScheduledTaskKey(
            match_id=match_id,
            kind=MatchScheduledTaskKind.APPROVAL_DEADLINE,
        )

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("MatchRuntime loop is already bound")
        self._loop = loop

    def _bind_current_loop_if_needed(self) -> None:
        self.bind_loop(asyncio.get_running_loop())

    async def _run_scheduled_task(
        self,
        *,
        key: ScheduledTaskKey,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            await self._run_handler_with_retry(
                task_name=task_name,
                entity_label="queue_entry_id",
                entity_id=key.queue_entry_id,
                task_kind=key.kind.value,
                scheduled_at=scheduled_at,
                deadline=deadline,
                handler_call=handler_call,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "Failed to execute %s queue_entry_id=%s",
                task_name,
                key.queue_entry_id,
            )
        finally:
            current_task = asyncio.current_task()
            if self._scheduled_tasks.get(key) is current_task:
                self._scheduled_tasks.pop(key, None)

    async def _run_match_scheduled_task(
        self,
        *,
        key: MatchScheduledTaskKey,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[Any]],
    ) -> None:
        try:
            await self._run_handler_with_retry(
                task_name=task_name,
                entity_label="match_id",
                entity_id=key.match_id,
                task_kind=key.kind.value,
                scheduled_at=scheduled_at,
                deadline=deadline,
                handler_call=handler_call,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "Failed to execute %s match_id=%s",
                task_name,
                key.match_id,
            )
        finally:
            current_task = asyncio.current_task()
            if self._scheduled_match_tasks.get(key) is current_task:
                self._scheduled_match_tasks.pop(key, None)

    async def _run_handler_with_retry(
        self,
        *,
        task_name: str,
        entity_label: str,
        entity_id: int,
        task_kind: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[Any]],
    ) -> None:
        failure_count = 0
        await self._sleep_until(scheduled_at)

        while True:
            try:
                await handler_call()
            except RetryableTaskError as exc:
                failure_count += 1
                retry_delay = retry_delay_for_failure_count(failure_count)
                next_retry_at = self._current_time_for(scheduled_at) + retry_delay

                if deadline is not None and next_retry_at >= deadline:
                    self.logger.info(
                        "Stopped retrying %s %s=%s failure_count=%s next_retry_at=%s deadline=%s",
                        task_name,
                        entity_label,
                        entity_id,
                        failure_count,
                        next_retry_at.isoformat(),
                        deadline.isoformat(),
                    )
                    return

                self.logger.warning(
                    "Retrying %s %s=%s failure_count=%s next_retry_at=%s error_type=%s kind=%s",
                    task_name,
                    entity_label,
                    entity_id,
                    failure_count,
                    next_retry_at.isoformat(),
                    type(exc).__name__,
                    task_kind,
                    exc_info=exc,
                )
                await asyncio.sleep(retry_delay.total_seconds())
                continue

            if failure_count > 0:
                self.logger.info(
                    "Recovered %s %s=%s failure_count=%s",
                    task_name,
                    entity_label,
                    entity_id,
                    failure_count,
                )
            return

    async def _sleep_until(self, scheduled_at: datetime) -> None:
        delay_seconds = self._seconds_until(scheduled_at)
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    async def _aclose_scheduled_tasks(self) -> None:
        if self._loop is None:
            return

        self._require_running_on_bound_loop()
        scheduled_tasks = list(self._scheduled_tasks.values())
        match_tasks = list(self._scheduled_match_tasks.values())
        self._scheduled_tasks.clear()
        self._scheduled_match_tasks.clear()

        for task in [*scheduled_tasks, *match_tasks]:
            task.cancel()

        if scheduled_tasks or match_tasks:
            await asyncio.gather(*scheduled_tasks, *match_tasks, return_exceptions=True)

    def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        if task is asyncio.current_task():
            return
        task.cancel()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MatchRuntime is closed")

    def _require_running_on_bound_loop(self) -> None:
        if self._loop is None:
            raise RuntimeError("MatchRuntime loop is not bound")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("MatchRuntime must be called on the bound loop")

    def _require_match_service(self) -> MatchFlowRuntimeService:
        if self.match_service is None:
            raise RuntimeError("Match flow service is not configured")
        return self.match_service

    def _schedule_match_reporting_tasks(
        self,
        *,
        match_id: int,
        report_open_at: datetime | None,
        report_deadline_at: datetime | None,
    ) -> None:
        if report_open_at is not None:
            self._cancel_match_task(self._report_open_task_key(match_id))
            self._schedule_match_task(
                key=self._report_open_task_key(match_id),
                task_name="match report open",
                scheduled_at=report_open_at,
                deadline=report_deadline_at,
                handler_call=self._handler_call(self.process_report_open, match_id),
            )
        if report_deadline_at is not None:
            self._cancel_match_task(self._report_deadline_task_key(match_id))
            self._schedule_match_task(
                key=self._report_deadline_task_key(match_id),
                task_name="match report deadline",
                scheduled_at=report_deadline_at,
                deadline=None,
                handler_call=self._handler_call(self.process_report_deadline, match_id),
            )

    def _schedule_match_approval_task(
        self,
        match_id: int,
        approval_deadline_at: datetime | None,
    ) -> None:
        if approval_deadline_at is None:
            return
        self._cancel_match_task(self._approval_deadline_task_key(match_id))
        self._schedule_match_task(
            key=self._approval_deadline_task_key(match_id),
            task_name="match approval deadline",
            scheduled_at=approval_deadline_at,
            deadline=None,
            handler_call=self._handler_call(self.process_approval_deadline, match_id),
        )

    def _seconds_until(self, scheduled_at: datetime) -> float:
        current_time = self._database_now_for(scheduled_at)
        return max((scheduled_at - current_time).total_seconds(), 0.0)

    def _current_time_for(self, reference: datetime) -> datetime:
        return self._database_now_for(reference)

    def _database_now_for(self, reference: datetime) -> datetime:
        return self._app_now_for_reference(reference) + self._database_clock_offset

    def _observe_database_time(self, database_time: datetime) -> None:
        self._database_clock_offset = database_time - self._app_now_for_reference(database_time)

    def _app_now_for_reference(self, reference: datetime) -> datetime:
        if reference.tzinfo is None:
            return datetime.now()
        return datetime.now(tz=reference.tzinfo)

    def _has_sync_activity(self, result: MatchRuntimeSyncResult) -> bool:
        return any(
            (
                result.cleaned_up_queue_entry_ids,
                result.reminded_queue_entry_ids,
                result.rescheduled_reminder_queue_entry_ids,
                result.rescheduled_expire_queue_entry_ids,
                result.created_match_ids,
                result.auto_assigned_parent_match_ids,
                result.opened_report_match_ids,
                result.started_approval_match_ids,
                result.finalized_match_ids,
                result.rescheduled_parent_deadline_match_ids,
                result.rescheduled_report_open_match_ids,
                result.rescheduled_report_deadline_match_ids,
                result.rescheduled_approval_deadline_match_ids,
            )
        )

    def _log_sync_result(self, context: str, result: MatchRuntimeSyncResult) -> None:
        self.logger.info(
            "%s finished cleaned_up=%s reminded=%s rescheduled_reminders=%s "
            "rescheduled_expires=%s created_matches=%s auto_assigned_parents=%s "
            "opened_reports=%s started_approvals=%s finalized_matches=%s "
            "rescheduled_parent_deadlines=%s rescheduled_report_open=%s "
            "rescheduled_report_deadlines=%s rescheduled_approval_deadlines=%s",
            context,
            len(result.cleaned_up_queue_entry_ids),
            len(result.reminded_queue_entry_ids),
            len(result.rescheduled_reminder_queue_entry_ids),
            len(result.rescheduled_expire_queue_entry_ids),
            len(result.created_match_ids),
            len(result.auto_assigned_parent_match_ids),
            len(result.opened_report_match_ids),
            len(result.started_approval_match_ids),
            len(result.finalized_match_ids),
            len(result.rescheduled_parent_deadline_match_ids),
            len(result.rescheduled_report_open_match_ids),
            len(result.rescheduled_report_deadline_match_ids),
            len(result.rescheduled_approval_deadline_match_ids),
        )
