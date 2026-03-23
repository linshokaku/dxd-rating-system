from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol, cast

import psycopg
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.platform.db.models import OutboxEvent, OutboxEventType
from dxd_rating.platform.db.session import session_scope
from dxd_rating.shared.constants import OUTBOX_NOTIFY_CHANNEL

DEFAULT_OUTBOX_BATCH_SIZE = 100
DEFAULT_OUTBOX_POLL_INTERVAL = timedelta(minutes=5)
_MIN_OUTBOX_RETRY_DELAY = timedelta(seconds=1)
_MAX_OUTBOX_RETRY_DELAY = timedelta(seconds=512)
_LISTENER_RECONNECT_INITIAL_DELAY = timedelta(seconds=1)
_LISTENER_RECONNECT_MAX_DELAY = timedelta(seconds=512)

DispatchTrigger = str


def retry_delay_for_failure_count(failure_count: int) -> timedelta:
    exponent = max(failure_count - 1, 0)
    delay_seconds = _MIN_OUTBOX_RETRY_DELAY.total_seconds() * (2**exponent)
    return timedelta(seconds=min(delay_seconds, _MAX_OUTBOX_RETRY_DELAY.total_seconds()))


@dataclass(frozen=True, slots=True)
class PendingOutboxEvent:
    id: int
    event_type: OutboxEventType
    dedupe_key: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxStartupResult:
    published_event_ids: tuple[int, ...] = tuple()


@dataclass(frozen=True, slots=True)
class ScheduledRetry:
    event_id: int
    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class _DispatchCycleResult:
    published_event_ids: tuple[int, ...]
    discarded_event_ids: tuple[int, ...]
    scheduled_retries: tuple[ScheduledRetry, ...]
    reached_batch_size: bool


@dataclass(frozen=True, slots=True)
class _DispatchSingleResult:
    published_event_id: int | None = None
    discarded_event_id: int | None = None
    scheduled_retry: ScheduledRetry | None = None


class OutboxEventPublisher(Protocol):
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def publish(self, event: PendingOutboxEvent) -> None: ...


class NoopOutboxEventPublisher:
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    def publish(self, event: PendingOutboxEvent) -> None:
        del event


class NonRetryableOutboxPublishError(Exception):
    pass


class NoopOutboxDispatcher:
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    async def start(self) -> OutboxStartupResult:
        return OutboxStartupResult()

    async def stop(self) -> None:
        return None


class OutboxNotificationListener(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class PostgresOutboxNotificationListener:
    def __init__(
        self,
        database_url: str,
        *,
        on_notification: Callable[[], None],
        on_reconnected: Callable[[], None],
        channel: str = OUTBOX_NOTIFY_CHANNEL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.database_url = database_url
        self.on_notification = on_notification
        self.on_reconnected = on_reconnected
        self.channel = channel
        self.logger = logger or logging.getLogger(__name__)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._connection_lock = threading.Lock()
        self._connection: psycopg.Connection[tuple[object, ...]] | None = None

    def start(self) -> None:
        if self._thread is not None:
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="matching-queue-outbox-listener",
            daemon=True,
        )
        self._thread.start()
        if not self._ready_event.wait(timeout=5.0):
            raise RuntimeError("Outbox notification listener failed to start within 5 seconds")

    def stop(self) -> None:
        self._stop_event.set()
        self._close_current_connection()

        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def _run(self) -> None:
        reconnect_delay = _LISTENER_RECONNECT_INITIAL_DELAY
        has_connected_once = False

        while not self._stop_event.is_set():
            try:
                with psycopg.connect(
                    self._to_psycopg_dsn(self.database_url),
                    autocommit=True,
                ) as connection:
                    self._set_current_connection(connection)
                    connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self.channel)))
                    self.logger.info(
                        "Listening for outbox notifications channel=%s",
                        self.channel,
                    )
                    self._ready_event.set()

                    if has_connected_once:
                        self.logger.info(
                            "Reconnected outbox notification listener channel=%s",
                            self.channel,
                        )
                        self.on_reconnected()

                    has_connected_once = True
                    reconnect_delay = _LISTENER_RECONNECT_INITIAL_DELAY

                    while not self._stop_event.is_set():
                        for _notify in connection.notifies(timeout=1.0):
                            if self._stop_event.is_set():
                                break
                            self.on_notification()
            except psycopg.Error:
                if self._stop_event.is_set():
                    break

                self.logger.warning(
                    "Outbox notification listener disconnected; retrying in %ss",
                    int(reconnect_delay.total_seconds()),
                    exc_info=True,
                )
                if self._stop_event.wait(reconnect_delay.total_seconds()):
                    break
                reconnect_delay = min(reconnect_delay * 2, _LISTENER_RECONNECT_MAX_DELAY)
            finally:
                self._clear_current_connection()

    def _set_current_connection(self, connection: psycopg.Connection[tuple[object, ...]]) -> None:
        with self._connection_lock:
            self._connection = connection

    def _clear_current_connection(self) -> None:
        with self._connection_lock:
            self._connection = None

    def _close_current_connection(self) -> None:
        with self._connection_lock:
            connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except psycopg.Error:
                self.logger.debug("Failed to close outbox listener connection", exc_info=True)

    def _to_psycopg_dsn(self, database_url: str) -> str:
        sqlalchemy_url = make_url(database_url).set(drivername="postgresql")
        return sqlalchemy_url.render_as_string(hide_password=False)


class OutboxDispatcher:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        publisher: OutboxEventPublisher,
        *,
        batch_size: int = DEFAULT_OUTBOX_BATCH_SIZE,
        poll_interval: timedelta = DEFAULT_OUTBOX_POLL_INTERVAL,
        logger: logging.Logger | None = None,
        notification_listener_factory: (
            Callable[[Callable[[], None], Callable[[], None]], OutboxNotificationListener] | None
        ) = None,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.logger = logger or logging.getLogger(__name__)
        self.notification_listener_factory = notification_listener_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._notification_listener: OutboxNotificationListener | None = None
        self._fallback_poll_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._scheduled_dispatch_tasks: set[asyncio.Task[None]] = set()
        self._retry_tasks: dict[int, asyncio.Task[None]] = {}
        self._started = False

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.publisher.bind_loop(loop)

    async def start(self) -> OutboxStartupResult:
        async with self._state_lock:
            if self._started:
                return OutboxStartupResult()

            if self._loop is None:
                self.bind_loop(asyncio.get_running_loop())

            self._notification_listener = self._create_notification_listener()
            try:
                self._notification_listener.start()
            except Exception:
                self._notification_listener = None
                raise
            self._started = True
            try:
                published_event_ids = await self.dispatch_once(trigger="startup")
                await self._rebuild_retry_timers()
                self._fallback_poll_task = asyncio.create_task(
                    self._run_fallback_poll_loop(),
                    name="matching-queue-outbox-fallback-poll",
                )
            except Exception:
                notification_listener = self._notification_listener
                self._notification_listener = None
                self._started = False
                if notification_listener is not None:
                    notification_listener.stop()
                raise
            return OutboxStartupResult(published_event_ids=published_event_ids)

    async def stop(self) -> None:
        async with self._state_lock:
            if not self._started:
                return

            self._started = False
            fallback_poll_task = self._fallback_poll_task
            self._fallback_poll_task = None
            scheduled_dispatch_tasks = list(self._scheduled_dispatch_tasks)
            self._scheduled_dispatch_tasks.clear()
            retry_tasks = list(self._retry_tasks.values())
            self._retry_tasks.clear()
            notification_listener = self._notification_listener
            self._notification_listener = None

        if notification_listener is not None:
            notification_listener.stop()

        if fallback_poll_task is not None:
            fallback_poll_task.cancel()

        for task in [*scheduled_dispatch_tasks, *retry_tasks]:
            task.cancel()

        tasks_to_await = [
            task
            for task in [fallback_poll_task, *scheduled_dispatch_tasks, *retry_tasks]
            if task is not None
        ]
        await asyncio.gather(*tasks_to_await, return_exceptions=True)

    async def dispatch_once(self, *, trigger: DispatchTrigger = "manual") -> tuple[int, ...]:
        async with self._dispatch_lock:
            result = await asyncio.to_thread(self._dispatch_due_sync)

        for event_id in [*result.published_event_ids, *result.discarded_event_ids]:
            self._cancel_retry_task(event_id)

        for scheduled_retry in result.scheduled_retries:
            self._schedule_retry_task(scheduled_retry)

        if result.published_event_ids:
            if trigger == "fallback_poll":
                self.logger.warning(
                    "Fallback outbox poll published events count=%s event_ids=%s",
                    len(result.published_event_ids),
                    result.published_event_ids,
                )
            else:
                self.logger.info(
                    "Published outbox events count=%s event_ids=%s trigger=%s",
                    len(result.published_event_ids),
                    result.published_event_ids,
                    trigger,
                )

        if result.reached_batch_size:
            self._schedule_dispatch(trigger)

        return result.published_event_ids

    async def _run_fallback_poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll_interval.total_seconds())
            except asyncio.CancelledError:
                raise

            try:
                await self.dispatch_once(trigger="fallback_poll")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Fallback outbox poll failed")

    async def _rebuild_retry_timers(self) -> None:
        retries = await asyncio.to_thread(self._load_future_retry_schedules_sync)
        for scheduled_retry in retries:
            self._schedule_retry_task(scheduled_retry)

    def _dispatch_due_sync(self) -> _DispatchCycleResult:
        published_event_ids: list[int] = []
        discarded_event_ids: list[int] = []
        scheduled_retries: list[ScheduledRetry] = []

        while (
            len(published_event_ids) + len(discarded_event_ids) + len(scheduled_retries)
            < self.batch_size
        ):
            result = self._dispatch_single_due_event_sync()
            if result is None:
                break

            if result.published_event_id is not None:
                published_event_ids.append(result.published_event_id)
            if result.discarded_event_id is not None:
                discarded_event_ids.append(result.discarded_event_id)
            if result.scheduled_retry is not None:
                scheduled_retries.append(result.scheduled_retry)

        return _DispatchCycleResult(
            published_event_ids=tuple(published_event_ids),
            discarded_event_ids=tuple(discarded_event_ids),
            scheduled_retries=tuple(scheduled_retries),
            reached_batch_size=(
                len(published_event_ids) + len(discarded_event_ids) + len(scheduled_retries)
                >= self.batch_size
            ),
        )

    def _dispatch_single_due_event_sync(self) -> _DispatchSingleResult | None:
        with session_scope(self.session_factory) as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.discarded_at.is_(None),
                    OutboxEvent.next_attempt_at <= func.now(),
                )
                .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return None

            pending_event = PendingOutboxEvent(
                id=event.id,
                event_type=event.event_type,
                dedupe_key=event.dedupe_key,
                payload=event.payload,
                created_at=event.created_at,
            )

            try:
                self.publisher.publish(pending_event)
            except NonRetryableOutboxPublishError as exc:
                current_time = self._get_database_now(session)
                event.discarded_at = current_time
                event.last_error = self._format_publish_error(exc)
                event.last_failed_at = current_time
                self.logger.info(
                    "Discarded outbox event id=%s reason=%s",
                    event.id,
                    event.last_error,
                )
                return _DispatchSingleResult(discarded_event_id=event.id)
            except Exception as exc:
                current_time = self._get_database_now(session)
                event.failure_count += 1
                event.next_attempt_at = current_time + retry_delay_for_failure_count(
                    event.failure_count
                )
                event.last_error = self._format_publish_error(exc)
                event.last_failed_at = current_time
                self.logger.warning(
                    "Failed to publish outbox event id=%s failure_count=%s next_attempt_at=%s",
                    event.id,
                    event.failure_count,
                    event.next_attempt_at.isoformat(),
                    exc_info=True,
                )
                return _DispatchSingleResult(
                    scheduled_retry=ScheduledRetry(
                        event_id=event.id,
                        next_attempt_at=event.next_attempt_at,
                    )
                )

            event.published_at = self._get_database_now(session)
            event.last_error = None
            event.last_failed_at = None
            return _DispatchSingleResult(published_event_id=event.id)

    def _load_future_retry_schedules_sync(self) -> tuple[ScheduledRetry, ...]:
        with session_scope(self.session_factory) as session:
            rows = session.execute(
                select(OutboxEvent.id, OutboxEvent.next_attempt_at)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.discarded_at.is_(None),
                    OutboxEvent.next_attempt_at > func.now(),
                )
                .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
            ).all()

        return tuple(
            ScheduledRetry(event_id=row.id, next_attempt_at=row.next_attempt_at) for row in rows
        )

    def _create_notification_listener(self) -> OutboxNotificationListener:
        if self.notification_listener_factory is not None:
            return self.notification_listener_factory(
                self._handle_notification,
                self._handle_reconnected_listener,
            )

        return PostgresOutboxNotificationListener(
            self._session_factory_database_url(),
            on_notification=self._handle_notification,
            on_reconnected=self._handle_reconnected_listener,
            logger=self.logger,
        )

    def _handle_notification(self) -> None:
        self._schedule_dispatch("notify")

    def _handle_reconnected_listener(self) -> None:
        self._schedule_dispatch("reconnect")

    def _schedule_dispatch(self, trigger: DispatchTrigger) -> None:
        if not self._started:
            return

        loop = self._require_loop()
        if self._is_running_on_bound_loop():
            self._create_dispatch_task(trigger)
            return

        loop.call_soon_threadsafe(self._create_dispatch_task, trigger)

    def _create_dispatch_task(self, trigger: DispatchTrigger) -> None:
        if not self._started:
            return

        task = asyncio.create_task(
            self._run_scheduled_dispatch(trigger),
            name=f"matching-queue-outbox-dispatch-{trigger}",
        )
        self._scheduled_dispatch_tasks.add(task)
        task.add_done_callback(self._scheduled_dispatch_tasks.discard)

    async def _run_scheduled_dispatch(self, trigger: DispatchTrigger) -> None:
        try:
            await self.dispatch_once(trigger=trigger)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("Failed to dispatch outbox events trigger=%s", trigger)

    def _schedule_retry_task(self, scheduled_retry: ScheduledRetry) -> None:
        loop = self._require_loop()

        def schedule_on_loop() -> None:
            existing_task = self._retry_tasks.pop(scheduled_retry.event_id, None)
            self._cancel_task(existing_task)
            self._retry_tasks[scheduled_retry.event_id] = asyncio.create_task(
                self._run_retry_timer(scheduled_retry),
                name=f"matching-queue-outbox-retry-{scheduled_retry.event_id}",
            )

        if self._is_running_on_bound_loop():
            schedule_on_loop()
            return

        loop.call_soon_threadsafe(schedule_on_loop)

    async def _run_retry_timer(self, scheduled_retry: ScheduledRetry) -> None:
        try:
            delay_seconds = self._seconds_until(scheduled_retry.next_attempt_at)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            await self.dispatch_once(trigger="retry_timer")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "Failed to run outbox retry timer event_id=%s",
                scheduled_retry.event_id,
            )
        finally:
            current_task = asyncio.current_task()
            if self._retry_tasks.get(scheduled_retry.event_id) is current_task:
                self._retry_tasks.pop(scheduled_retry.event_id, None)

    def _cancel_retry_task(self, event_id: int) -> None:
        loop = self._require_loop()

        def cancel_on_loop() -> None:
            self._cancel_task(self._retry_tasks.pop(event_id, None))

        if self._is_running_on_bound_loop():
            cancel_on_loop()
            return

        loop.call_soon_threadsafe(cancel_on_loop)

    def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        if task is asyncio.current_task():
            return
        task.cancel()

    def _session_factory_database_url(self) -> str:
        bind = self.session_factory.kw.get("bind")
        if bind is None:
            raise RuntimeError("OutboxDispatcher session factory is not bound")

        engine = cast(Engine, getattr(bind, "engine", bind))
        return engine.url.render_as_string(hide_password=False)

    def _get_database_now(self, session: Session) -> datetime:
        return session.execute(select(func.now())).scalar_one()

    def _seconds_until(self, scheduled_at: datetime) -> float:
        if scheduled_at.tzinfo is None:
            current_time = datetime.now()
        else:
            current_time = datetime.now(tz=scheduled_at.tzinfo)
        return max((scheduled_at - current_time).total_seconds(), 0.0)

    def _format_publish_error(self, exc: Exception) -> str:
        error_message = f"{type(exc).__name__}: {exc}"
        return error_message[:1000]

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        return asyncio.get_running_loop()

    def _is_running_on_bound_loop(self) -> bool:
        if self._loop is None:
            return False

        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False
