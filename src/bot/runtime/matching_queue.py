from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from bot.runtime.outbox import (
    DEFAULT_OUTBOX_POLL_INTERVAL,
    OutboxDispatcher,
    OutboxEventPublisher,
)
from bot.services import (
    ExpireQueueEntryResult,
    ExpireTask,
    MatchingQueueService,
    MatchingQueueTaskScheduler,
    PresenceReminderResult,
    PresenceReminderTask,
    StartupSyncResult,
)

DEFAULT_RECONCILE_INTERVAL = timedelta(minutes=5)

PresenceReminderHandler = Callable[[int, int], PresenceReminderResult]
ExpireHandler = Callable[[int, int], ExpireQueueEntryResult]


class AsyncioMatchingQueueTaskScheduler(MatchingQueueTaskScheduler):
    def __init__(
        self,
        *,
        presence_reminder_handler: PresenceReminderHandler | None = None,
        expire_handler: ExpireHandler | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._presence_reminder_handler = presence_reminder_handler
        self._expire_handler = expire_handler
        self._logger = logger or logging.getLogger(__name__)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._presence_reminder_tasks: dict[int, asyncio.Task[None]] = {}
        self._expire_tasks: dict[int, asyncio.Task[None]] = {}

    def bind_handlers(
        self,
        *,
        presence_reminder_handler: PresenceReminderHandler,
        expire_handler: ExpireHandler,
    ) -> None:
        self._presence_reminder_handler = presence_reminder_handler
        self._expire_handler = expire_handler

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("AsyncioMatchingQueueTaskScheduler loop is already bound")
        self._loop = loop

    def schedule_presence_reminder(self, task: PresenceReminderTask) -> None:
        self._submit_to_loop(self._schedule_presence_reminder_on_loop, task)

    def schedule_expire(self, task: ExpireTask) -> None:
        self._submit_to_loop(self._schedule_expire_on_loop, task)

    def cancel_presence_reminder(self, queue_entry_id: int) -> None:
        self._submit_to_loop(self._cancel_presence_reminder_on_loop, queue_entry_id)

    def cancel_expire(self, queue_entry_id: int) -> None:
        self._submit_to_loop(self._cancel_expire_on_loop, queue_entry_id)

    async def aclose(self) -> None:
        loop = self._require_loop()
        if self._is_running_on_bound_loop():
            await self._aclose_on_loop()
            return

        future = asyncio.run_coroutine_threadsafe(self._aclose_on_loop(), loop)
        await asyncio.wrap_future(future)

    def _submit_to_loop(self, callback: Callable[..., None], *args: object) -> None:
        if self._closed:
            raise RuntimeError("AsyncioMatchingQueueTaskScheduler is closed")

        loop = self._require_loop()
        if self._is_running_on_bound_loop():
            callback(*args)
            return

        loop.call_soon_threadsafe(callback, *args)

    def _schedule_presence_reminder_on_loop(self, task: PresenceReminderTask) -> None:
        current_task = self._presence_reminder_tasks.pop(task.queue_entry_id, None)
        self._cancel_task(current_task)
        self._presence_reminder_tasks[task.queue_entry_id] = asyncio.create_task(
            self._run_presence_reminder(task),
            name=f"presence-reminder-{task.queue_entry_id}-{task.expected_revision}",
        )

    def _schedule_expire_on_loop(self, task: ExpireTask) -> None:
        current_task = self._expire_tasks.pop(task.queue_entry_id, None)
        self._cancel_task(current_task)
        self._expire_tasks[task.queue_entry_id] = asyncio.create_task(
            self._run_expire(task),
            name=f"expire-{task.queue_entry_id}-{task.expected_revision}",
        )

    def _cancel_presence_reminder_on_loop(self, queue_entry_id: int) -> None:
        self._cancel_task(self._presence_reminder_tasks.pop(queue_entry_id, None))

    def _cancel_expire_on_loop(self, queue_entry_id: int) -> None:
        self._cancel_task(self._expire_tasks.pop(queue_entry_id, None))

    async def _run_presence_reminder(self, task: PresenceReminderTask) -> None:
        try:
            await self._sleep_until(task.remind_at)
            handler = self._require_presence_reminder_handler()
            await asyncio.to_thread(handler, task.queue_entry_id, task.expected_revision)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Failed to execute presence reminder queue_entry_id=%s revision=%s",
                task.queue_entry_id,
                task.expected_revision,
            )
        finally:
            current_task = asyncio.current_task()
            if self._presence_reminder_tasks.get(task.queue_entry_id) is current_task:
                self._presence_reminder_tasks.pop(task.queue_entry_id, None)

    async def _run_expire(self, task: ExpireTask) -> None:
        try:
            await self._sleep_until(task.expire_at)
            handler = self._require_expire_handler()
            await asyncio.to_thread(handler, task.queue_entry_id, task.expected_revision)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception(
                "Failed to execute expire queue_entry_id=%s revision=%s",
                task.queue_entry_id,
                task.expected_revision,
            )
        finally:
            current_task = asyncio.current_task()
            if self._expire_tasks.get(task.queue_entry_id) is current_task:
                self._expire_tasks.pop(task.queue_entry_id, None)

    async def _sleep_until(self, scheduled_at: datetime) -> None:
        delay_seconds = self._seconds_until(scheduled_at)
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    def _seconds_until(self, scheduled_at: datetime) -> float:
        if scheduled_at.tzinfo is None:
            current_time = datetime.now()
        else:
            current_time = datetime.now(tz=scheduled_at.tzinfo)

        return max((scheduled_at - current_time).total_seconds(), 0.0)

    async def _aclose_on_loop(self) -> None:
        self._closed = True

        presence_tasks = list(self._presence_reminder_tasks.values())
        expire_tasks = list(self._expire_tasks.values())
        self._presence_reminder_tasks.clear()
        self._expire_tasks.clear()

        for task in [*presence_tasks, *expire_tasks]:
            task.cancel()

        if presence_tasks or expire_tasks:
            await asyncio.gather(*presence_tasks, *expire_tasks, return_exceptions=True)

    def _cancel_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None:
            task.cancel()

    def _require_presence_reminder_handler(self) -> PresenceReminderHandler:
        if self._presence_reminder_handler is None:
            raise RuntimeError("Presence reminder handler is not bound")
        return self._presence_reminder_handler

    def _require_expire_handler(self) -> ExpireHandler:
        if self._expire_handler is None:
            raise RuntimeError("Expire handler is not bound")
        return self._expire_handler

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("AsyncioMatchingQueueTaskScheduler loop is not bound")
        return self._loop

    def _is_running_on_bound_loop(self) -> bool:
        if self._loop is None:
            return False

        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False


class MatchingQueueRuntime:
    def __init__(
        self,
        service: MatchingQueueService,
        scheduler: AsyncioMatchingQueueTaskScheduler,
        *,
        outbox_dispatcher: OutboxDispatcher | None = None,
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.scheduler = scheduler
        self.outbox_dispatcher = outbox_dispatcher
        self.reconcile_interval = reconcile_interval
        self.logger = logger or logging.getLogger(__name__)
        self._reconcile_task: asyncio.Task[None] | None = None
        self._closed = False
        self._state_lock = asyncio.Lock()

    @classmethod
    def create(
        cls,
        session_factory: sessionmaker[Session],
        *,
        outbox_publisher: OutboxEventPublisher | None = None,
        reconcile_interval: timedelta = DEFAULT_RECONCILE_INTERVAL,
        outbox_dispatcher_poll_interval: timedelta | None = None,
        logger: logging.Logger | None = None,
    ) -> MatchingQueueRuntime:
        scheduler = AsyncioMatchingQueueTaskScheduler(logger=logger)
        service = MatchingQueueService(
            session_factory=session_factory,
            task_scheduler=scheduler,
            logger=logger,
        )
        scheduler.bind_handlers(
            presence_reminder_handler=service.process_presence_reminder,
            expire_handler=service.process_expire,
        )

        outbox_dispatcher = None
        if outbox_publisher is not None:
            outbox_dispatcher = OutboxDispatcher(
                session_factory=session_factory,
                publisher=outbox_publisher,
                poll_interval=outbox_dispatcher_poll_interval or DEFAULT_OUTBOX_POLL_INTERVAL,
                logger=logger,
            )

        return cls(
            service=service,
            scheduler=scheduler,
            outbox_dispatcher=outbox_dispatcher,
            reconcile_interval=reconcile_interval,
            logger=logger,
        )

    async def start(self) -> StartupSyncResult:
        async with self._state_lock:
            if self._closed:
                raise RuntimeError("MatchingQueueRuntime is already closed")
            if self._reconcile_task is not None:
                raise RuntimeError("MatchingQueueRuntime is already started")

            loop = asyncio.get_running_loop()
            self.scheduler.bind_loop(loop)
            startup_result = await self.run_startup_sync()
            if self.outbox_dispatcher is not None:
                self.outbox_dispatcher.bind_loop(loop)
                await self.outbox_dispatcher.start()
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

        if self.outbox_dispatcher is not None:
            await self.outbox_dispatcher.stop()

        try:
            await self.scheduler.aclose()
        except RuntimeError:
            pass

    async def run_startup_sync(self) -> StartupSyncResult:
        result = await asyncio.to_thread(self.service.run_startup_sync)
        self._log_sync_result("Startup sync", result)
        return result

    async def run_reconcile_cycle(self) -> StartupSyncResult:
        result = await asyncio.to_thread(self.service.run_reconcile_cycle)
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
