from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, ParamSpec, Protocol, TypeVar

from sqlalchemy.orm import Session, sessionmaker

from bot.runtime.outbox import retry_delay_for_failure_count
from bot.services import (
    PRESENCE_REMINDER_LEAD_TIME,
    CreatedMatchResult,
    ExpireQueueEntryResult,
    JoinQueueResult,
    LeaveQueueResult,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    PresenceReminderResult,
    PresentQueueResult,
    RetryableTaskError,
    WaitingEntryTimerState,
)

DEFAULT_RECONCILE_INTERVAL = timedelta(minutes=5)
P = ParamSpec("P")
R = TypeVar("R")


class MatchingQueueRuntimeService(Protocol):
    def join_queue(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult: ...

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

    def try_create_matches(self) -> tuple[CreatedMatchResult, ...]: ...

    def load_waiting_entry_timer_states(
        self,
    ) -> tuple[datetime, tuple[WaitingEntryTimerState, ...]]: ...


@dataclass(frozen=True, slots=True)
class StartupSyncResult:
    cleaned_up_queue_entry_ids: tuple[int, ...]
    reminded_queue_entry_ids: tuple[int, ...]
    rescheduled_reminder_queue_entry_ids: tuple[int, ...]
    rescheduled_expire_queue_entry_ids: tuple[int, ...]
    created_match_ids: tuple[int, ...]


class ScheduledTaskKind(StrEnum):
    PRESENCE_REMINDER = "presence-reminder"
    EXPIRE = "expire"


@dataclass(frozen=True, slots=True)
class ScheduledTaskKey:
    queue_entry_id: int
    kind: ScheduledTaskKind


class MatchingQueueRuntime:
    def __init__(
        self,
        service: MatchingQueueRuntimeService,
        *,
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.reconcile_interval = reconcile_interval
        self.logger = logger or logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduled_tasks: dict[ScheduledTaskKey, asyncio.Task[None]] = {}
        self._reconcile_task: asyncio.Task[None] | None = None
        self._closed = False
        self._state_lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        session_factory: sessionmaker[Session],
        *,
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> MatchingQueueRuntime:
        service = MatchingQueueService(
            session_factory=session_factory,
            logger=logger,
        )

        return cls(
            service=service,
            reconcile_interval=reconcile_interval,
            logger=logger,
        )

    async def join_queue(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(
            self.service.join_queue,
            player_id,
            notification_context=notification_context,
        )
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
        await self._try_create_matches_safely(context="join")
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

    async def leave(self, player_id: int) -> LeaveQueueResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await asyncio.to_thread(self.service.leave, player_id)
        if result.queue_entry_id is not None:
            self._cancel_scheduled_task(self._presence_reminder_task_key(result.queue_entry_id))
            self._cancel_scheduled_task(self._expire_task_key(result.queue_entry_id))
        return result

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

    async def start(self) -> StartupSyncResult:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("MatchingQueueRuntime is already closed")
            if self._reconcile_task is not None:
                raise RuntimeError("MatchingQueueRuntime is already started")

            loop = asyncio.get_running_loop()
            self.bind_loop(loop)
            startup_result = await self.run_startup_sync()
            self._reconcile_task = asyncio.create_task(
                self._run_reconcile_loop(),
                name="matching-queue-reconcile",
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

    async def run_startup_sync(self) -> StartupSyncResult:
        self._ensure_open()
        self._bind_current_loop_if_needed()
        result = await self._run_sync_cycle(False)
        self._log_sync_result("Startup sync", result)
        return result

    async def run_reconcile_cycle(self) -> StartupSyncResult:
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
                self.logger.exception("Matching queue reconcile cycle failed")

    async def _run_sync_cycle(self, warn_on_cleanup: bool) -> StartupSyncResult:
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

        return StartupSyncResult(
            cleaned_up_queue_entry_ids=cleaned_up_queue_entry_ids,
            reminded_queue_entry_ids=tuple(reminded_queue_entry_ids),
            rescheduled_reminder_queue_entry_ids=tuple(rescheduled_reminder_queue_entry_ids),
            rescheduled_expire_queue_entry_ids=tuple(rescheduled_expire_queue_entry_ids),
            created_match_ids=tuple(match.match_id for match in created_matches),
        )

    async def _try_create_matches(self) -> tuple[CreatedMatchResult, ...]:
        created_matches = await asyncio.to_thread(self.service.try_create_matches)
        for created_match in created_matches:
            for queue_entry_id in created_match.queue_entry_ids:
                self._cancel_scheduled_task(self._presence_reminder_task_key(queue_entry_id))
                self._cancel_scheduled_task(self._expire_task_key(queue_entry_id))
        return created_matches

    async def _try_create_matches_safely(self, *, context: str) -> tuple[CreatedMatchResult, ...]:
        try:
            return await self._try_create_matches()
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

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("MatchingQueueRuntime loop is already bound")
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
                queue_entry_id=key.queue_entry_id,
                task_kind=key.kind,
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

    async def _run_handler_with_retry(
        self,
        *,
        task_name: str,
        queue_entry_id: int,
        task_kind: ScheduledTaskKind,
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
                        "Stopped retrying %s queue_entry_id=%s failure_count=%s "
                        "next_retry_at=%s deadline=%s",
                        task_name,
                        queue_entry_id,
                        failure_count,
                        next_retry_at.isoformat(),
                        deadline.isoformat(),
                    )
                    return

                self.logger.warning(
                    "Retrying %s queue_entry_id=%s failure_count=%s next_retry_at=%s "
                    "error_type=%s kind=%s",
                    task_name,
                    queue_entry_id,
                    failure_count,
                    next_retry_at.isoformat(),
                    type(exc).__name__,
                    task_kind.value,
                    exc_info=exc,
                )
                await asyncio.sleep(retry_delay.total_seconds())
                continue

            if failure_count > 0:
                self.logger.info(
                    "Recovered %s queue_entry_id=%s failure_count=%s",
                    task_name,
                    queue_entry_id,
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
        await self._aclose_scheduled_tasks_on_loop()

    async def _aclose_scheduled_tasks_on_loop(self) -> None:
        scheduled_tasks = list(self._scheduled_tasks.values())
        self._scheduled_tasks.clear()

        for task in scheduled_tasks:
            task.cancel()

        if scheduled_tasks:
            await asyncio.gather(*scheduled_tasks, return_exceptions=True)

    def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        if task is asyncio.current_task():
            return
        task.cancel()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("MatchingQueueRuntime is closed")

    def _require_running_on_bound_loop(self) -> None:
        if self._loop is None:
            raise RuntimeError("MatchingQueueRuntime loop is not bound")
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("MatchingQueueRuntime must be called on the bound loop")

    def _seconds_until(self, scheduled_at: datetime) -> float:
        if scheduled_at.tzinfo is None:
            current_time = datetime.now()
        else:
            current_time = datetime.now(tz=scheduled_at.tzinfo)

        return max((scheduled_at - current_time).total_seconds(), 0.0)

    def _current_time_for(self, reference: datetime) -> datetime:
        if reference.tzinfo is None:
            return datetime.now()
        return datetime.now(tz=reference.tzinfo)

    def _has_sync_activity(self, result: StartupSyncResult) -> bool:
        return any(
            (
                result.cleaned_up_queue_entry_ids,
                result.reminded_queue_entry_ids,
                result.rescheduled_reminder_queue_entry_ids,
                result.rescheduled_expire_queue_entry_ids,
                result.created_match_ids,
            )
        )

    def _log_sync_result(self, context: str, result: StartupSyncResult) -> None:
        self.logger.info(
            "%s finished cleaned_up=%s reminded=%s rescheduled_reminders=%s "
            "rescheduled_expires=%s created_matches=%s",
            context,
            len(result.cleaned_up_queue_entry_ids),
            len(result.reminded_queue_entry_ids),
            len(result.rescheduled_reminder_queue_entry_ids),
            len(result.rescheduled_expire_queue_entry_ids),
            len(result.created_match_ids),
        )
