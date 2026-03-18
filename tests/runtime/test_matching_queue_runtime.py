from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import bot.runtime.matching_queue as matching_queue_runtime
import bot.runtime.outbox as outbox_runtime
from bot.models import Match, MatchQueueEntry, MatchQueueEntryStatus, OutboxEvent, OutboxEventType
from bot.notifications import DiscordOutboxEventPublisher
from bot.runtime import (
    AsyncioMatchingQueueTaskScheduler,
    MatchingQueueRuntime,
    OutboxDispatcher,
    PendingOutboxEvent,
)
from bot.services import (
    MATCH_CREATED_NOTIFICATION_MESSAGE,
    PRESENCE_REMINDER_LEAD_TIME,
    PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
    QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
    ExpireQueueEntryResult,
    ExpireTask,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    NoopMatchingQueueTaskScheduler,
    PresenceReminderResult,
    PresenceReminderTask,
    RetryableTaskError,
    StartupSyncResult,
    register_player,
)


@dataclass
class FakeMatchingQueueRuntimeService:
    startup_calls: int = 0
    reconcile_calls: int = 0
    reminder_calls: list[tuple[int, int]] = field(default_factory=list)
    expire_calls: list[tuple[int, int]] = field(default_factory=list)

    def process_presence_reminder(
        self, queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        self.reminder_calls.append((queue_entry_id, expected_revision))
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    def process_expire(self, queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        self.expire_calls.append((queue_entry_id, expected_revision))
        return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=True)

    def run_startup_sync(self) -> StartupSyncResult:
        self.startup_calls += 1
        return StartupSyncResult(
            cleaned_up_queue_entry_ids=tuple(),
            reminded_queue_entry_ids=tuple(),
            rescheduled_reminder_queue_entry_ids=tuple(),
            rescheduled_expire_queue_entry_ids=tuple(),
            created_match_ids=tuple(),
        )

    def run_reconcile_cycle(self) -> StartupSyncResult:
        self.reconcile_calls += 1
        return StartupSyncResult(
            cleaned_up_queue_entry_ids=tuple(),
            reminded_queue_entry_ids=tuple(),
            rescheduled_reminder_queue_entry_ids=tuple(),
            rescheduled_expire_queue_entry_ids=tuple(),
            created_match_ids=tuple(),
        )


@dataclass
class RecordingOutboxPublisher:
    events: list[PendingOutboxEvent] = field(default_factory=list)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    def publish(self, event: PendingOutboxEvent) -> None:
        self.events.append(event)


@dataclass
class FlakyOutboxPublisher:
    failures_remaining: int = 1
    attempts: int = 0
    events: list[PendingOutboxEvent] = field(default_factory=list)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    def publish(self, event: PendingOutboxEvent) -> None:
        self.attempts += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("temporary Discord failure")
        self.events.append(event)


@dataclass
class FakeOutboxNotificationListener:
    on_notification: Callable[[], None]
    on_reconnected: Callable[[], None]
    started: bool = False
    stopped: bool = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self) -> None:
        self.on_notification()

    def reconnect(self) -> None:
        self.on_reconnected()


@dataclass
class FakeOutboxNotificationListenerFactory:
    listener: FakeOutboxNotificationListener | None = None

    def __call__(
        self,
        on_notification: Callable[[], None],
        on_reconnected: Callable[[], None],
    ) -> FakeOutboxNotificationListener:
        self.listener = FakeOutboxNotificationListener(
            on_notification=on_notification,
            on_reconnected=on_reconnected,
        )
        return self.listener


@dataclass(frozen=True)
class FakeDiscordGuild:
    id: int


@dataclass
class FakeDiscordChannel:
    id: int
    guild: FakeDiscordGuild | None = None
    sent_messages: list[str] = field(default_factory=list)
    allowed_mentions_history: list[discord.AllowedMentions] = field(default_factory=list)

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions,
    ) -> None:
        self.sent_messages.append(content)
        self.allowed_mentions_history.append(allowed_mentions)


@dataclass
class FakeDiscordClient:
    channels: dict[int, FakeDiscordChannel] = field(default_factory=dict)
    uncached_channel_ids: set[int] = field(default_factory=set)
    fetched_channel_ids: list[int] = field(default_factory=list)

    def get_channel(self, channel_id: int) -> object | None:
        if channel_id in self.uncached_channel_ids:
            return None
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        self.fetched_channel_ids.append(channel_id)
        channel = self.channels.get(channel_id)
        if channel is None:
            raise LookupError(f"Unknown channel: {channel_id}")
        return channel


class JoinWithoutMatchAttemptService(MatchingQueueService):
    def _try_create_matches_safely(self, *, context: str) -> None:
        del context


def create_player(session: Session, discord_user_id: int) -> int:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player.id


def create_players(
    session: Session,
    count: int,
    *,
    start_discord_user_id: int,
) -> list[int]:
    return [create_player(session, start_discord_user_id + index) for index in range(count)]


def build_notification_context(
    discord_user_id: int,
    *,
    channel_id: int | None = None,
    guild_id: int | None = None,
) -> MatchingQueueNotificationContext:
    resolved_channel_id = channel_id if channel_id is not None else 800_000 + discord_user_id
    resolved_guild_id = guild_id if guild_id is not None else 810_000 + discord_user_id
    return MatchingQueueNotificationContext(
        channel_id=resolved_channel_id,
        guild_id=resolved_guild_id,
        mention_discord_user_id=discord_user_id,
    )


async def publish_with_bound_loop(
    publisher: DiscordOutboxEventPublisher,
    event: PendingOutboxEvent,
) -> None:
    publisher.bind_loop(asyncio.get_running_loop())
    await asyncio.to_thread(publisher.publish, event)


async def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 1.0,
    interval: float = 0.01,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("Condition was not met before timeout")


def test_asyncio_matching_queue_task_scheduler_runs_due_tasks() -> None:
    # 対応するテスト項目:
    # - `expire_at - 1分` に達した `waiting` 行に対して在席確認リマインドが 1 回だけ送られること
    # - `expire_at <= now()` の `waiting` 行が `expired` に遷移し、
    #   `removed_at` と `removal_reason = 'timeout'` が設定されること
    reminder_event = threading.Event()
    expire_event = threading.Event()
    reminder_calls: list[tuple[int, int]] = []
    expire_calls: list[tuple[int, int]] = []

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        reminder_calls.append((queue_entry_id, expected_revision))
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    def handle_expire(queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        expire_calls.append((queue_entry_id, expected_revision))
        expire_event.set()
        return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=True)

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=handle_presence_reminder,
            expire_handler=handle_expire,
        )
        scheduler.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        scheduler.schedule_presence_reminder(
            PresenceReminderTask(
                queue_entry_id=101,
                expected_revision=3,
                remind_at=current_time + timedelta(milliseconds=20),
            )
        )
        scheduler.schedule_expire(
            ExpireTask(
                queue_entry_id=202,
                expected_revision=4,
                expire_at=current_time + timedelta(milliseconds=30),
            )
        )

        assert await asyncio.to_thread(reminder_event.wait, 1.0)
        assert await asyncio.to_thread(expire_event.wait, 1.0)
        await scheduler.aclose()

    asyncio.run(scenario())

    assert reminder_calls == [(101, 3)]
    assert expire_calls == [(202, 4)]


def test_asyncio_matching_queue_task_scheduler_cancels_pending_tasks() -> None:
    # 対応するテスト項目:
    # - `leave` 後にローカルの在席確認リマインドタスクと expire タスクが cancel されること
    reminder_event = threading.Event()
    expire_event = threading.Event()

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        del queue_entry_id, expected_revision
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=1, reminded=True)

    def handle_expire(queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        del queue_entry_id, expected_revision
        expire_event.set()
        return ExpireQueueEntryResult(queue_entry_id=2, expired=True)

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=handle_presence_reminder,
            expire_handler=handle_expire,
        )
        scheduler.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        scheduler.schedule_presence_reminder(
            PresenceReminderTask(
                queue_entry_id=1,
                expected_revision=1,
                remind_at=current_time + timedelta(milliseconds=50),
            )
        )
        scheduler.schedule_expire(
            ExpireTask(
                queue_entry_id=2,
                expected_revision=1,
                expire_at=current_time + timedelta(milliseconds=50),
            )
        )
        scheduler.cancel_presence_reminder(1)
        scheduler.cancel_expire(2)

        await asyncio.sleep(0.1)
        await scheduler.aclose()

    asyncio.run(scenario())

    assert not reminder_event.is_set()
    assert not expire_event.is_set()


def test_asyncio_matching_queue_task_scheduler_retries_retryable_failures() -> None:
    reminder_event = threading.Event()
    expire_event = threading.Event()
    reminder_attempts = 0
    expire_attempts = 0

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        nonlocal reminder_attempts
        reminder_attempts += 1
        if reminder_attempts == 1:
            raise RetryableTaskError("temporary presence reminder failure")
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    def handle_expire(queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        nonlocal expire_attempts
        expire_attempts += 1
        if expire_attempts == 1:
            raise RetryableTaskError("temporary expire failure")
        expire_event.set()
        return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=True)

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=handle_presence_reminder,
            expire_handler=handle_expire,
        )
        scheduler.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        original_delay_fn = matching_queue_runtime.retry_delay_for_failure_count
        matching_queue_runtime.retry_delay_for_failure_count = lambda failure_count: timedelta(
            milliseconds=10
        )
        try:
            scheduler.schedule_presence_reminder(
                PresenceReminderTask(
                    queue_entry_id=301,
                    expected_revision=7,
                    remind_at=current_time + timedelta(milliseconds=10),
                )
            )
            scheduler.schedule_expire(
                ExpireTask(
                    queue_entry_id=302,
                    expected_revision=8,
                    expire_at=current_time + timedelta(milliseconds=10),
                )
            )

            assert await asyncio.to_thread(reminder_event.wait, 1.0)
            assert await asyncio.to_thread(expire_event.wait, 1.0)
        finally:
            matching_queue_runtime.retry_delay_for_failure_count = original_delay_fn
            await scheduler.aclose()

    asyncio.run(scenario())

    assert reminder_attempts == 2
    assert expire_attempts == 2


def test_asyncio_matching_queue_task_scheduler_stops_presence_retry_after_deadline() -> None:
    reminder_attempts = 0

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        nonlocal reminder_attempts
        del queue_entry_id, expected_revision
        reminder_attempts += 1
        raise RetryableTaskError("temporary presence reminder failure")

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=handle_presence_reminder,
            expire_handler=lambda queue_entry_id, expected_revision: ExpireQueueEntryResult(
                queue_entry_id=queue_entry_id,
                expired=False,
            ),
        )
        scheduler.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)
        remind_at = current_time - PRESENCE_REMINDER_LEAD_TIME + timedelta(milliseconds=5)

        original_delay_fn = matching_queue_runtime.retry_delay_for_failure_count
        matching_queue_runtime.retry_delay_for_failure_count = lambda failure_count: timedelta(
            milliseconds=20
        )
        try:
            scheduler.schedule_presence_reminder(
                PresenceReminderTask(
                    queue_entry_id=401,
                    expected_revision=2,
                    remind_at=remind_at,
                )
            )
            await asyncio.sleep(0.05)
        finally:
            matching_queue_runtime.retry_delay_for_failure_count = original_delay_fn
            await scheduler.aclose()

    asyncio.run(scenario())

    assert reminder_attempts == 1


def test_asyncio_matching_queue_task_scheduler_replaces_pending_retry_when_rescheduled() -> None:
    calls: list[tuple[int, int]] = []
    reminder_event = threading.Event()

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        calls.append((queue_entry_id, expected_revision))
        if expected_revision == 1:
            raise RetryableTaskError("temporary presence reminder failure")
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=handle_presence_reminder,
            expire_handler=lambda queue_entry_id, expected_revision: ExpireQueueEntryResult(
                queue_entry_id=queue_entry_id,
                expired=False,
            ),
        )
        scheduler.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        original_delay_fn = matching_queue_runtime.retry_delay_for_failure_count
        matching_queue_runtime.retry_delay_for_failure_count = lambda failure_count: timedelta(
            milliseconds=50
        )
        try:
            scheduler.schedule_presence_reminder(
                PresenceReminderTask(
                    queue_entry_id=501,
                    expected_revision=1,
                    remind_at=current_time + timedelta(milliseconds=10),
                )
            )
            await wait_until(lambda: calls == [(501, 1)], timeout=1.0)

            scheduler.schedule_presence_reminder(
                PresenceReminderTask(
                    queue_entry_id=501,
                    expected_revision=2,
                    remind_at=datetime.now(timezone.utc) + timedelta(milliseconds=10),
                )
            )

            assert await asyncio.to_thread(reminder_event.wait, 1.0)
            await asyncio.sleep(0.08)
        finally:
            matching_queue_runtime.retry_delay_for_failure_count = original_delay_fn
            await scheduler.aclose()

    asyncio.run(scenario())

    assert calls == [(501, 1), (501, 2)]


def test_matching_queue_runtime_runs_startup_sync_and_reconcile_loop() -> None:
    # 対応するテスト項目:
    # - 起動時に期限切れ行の cleanup が行われること
    # - 起動時に `try_create_matches()` が実行され、
    #   すでに 6 人以上待機しているケースを回収できること
    # - 起動時に reminder 対象の行へ即時リマインドできること
    # - 起動時に将来期限の `waiting` 行へ reminder タスクと expire タスクが再登録されること
    service = FakeMatchingQueueRuntimeService()

    async def scenario() -> None:
        scheduler = AsyncioMatchingQueueTaskScheduler(
            presence_reminder_handler=service.process_presence_reminder,
            expire_handler=service.process_expire,
        )
        runtime = MatchingQueueRuntime(
            service=service,
            scheduler=scheduler,
            reconcile_interval=timedelta(milliseconds=20),
        )

        await runtime.start()
        await asyncio.sleep(0.08)
        await runtime.stop()

    asyncio.run(scenario())

    assert service.startup_calls == 1
    assert service.reconcile_calls >= 1


def test_runtime_startup_sync_recovers_missing_tasks_after_join_commit(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    # 対応するテスト項目:
    # - `join` commit 後にプロセスが落ちてタスク未登録になっても、
    #   起動時再同期で復旧できること
    player_id = create_player(session, 70_001)
    crash_service = MatchingQueueService(
        session_factory=session_factory,
        task_scheduler=NoopMatchingQueueTaskScheduler(),
    )
    join_result = crash_service.join_queue(
        player_id,
        notification_context=build_notification_context(70_001),
    )

    async def scenario() -> StartupSyncResult:
        runtime = MatchingQueueRuntime.create(
            session_factory=session_factory,
            reconcile_interval=timedelta(hours=1),
        )
        try:
            return await runtime.start()
        finally:
            await runtime.stop()

    startup_result = asyncio.run(scenario())

    session.expire_all()
    queue_entry = session.get(MatchQueueEntry, join_result.queue_entry_id)

    assert queue_entry is not None
    assert queue_entry.status == MatchQueueEntryStatus.WAITING
    assert startup_result.rescheduled_reminder_queue_entry_ids == (join_result.queue_entry_id,)
    assert startup_result.rescheduled_expire_queue_entry_ids == (join_result.queue_entry_id,)


def test_runtime_startup_sync_recovers_missing_match_attempt_after_join_commit(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    # 対応するテスト項目:
    # - `join` commit 後にプロセスが落ちてマッチング試行が走らなくても、
    #   起動時再同期の `try_create_matches()` で回収できること
    player_ids = create_players(session, 6, start_discord_user_id=70_100)
    crash_service = JoinWithoutMatchAttemptService(
        session_factory=session_factory,
        task_scheduler=NoopMatchingQueueTaskScheduler(),
    )
    for index, player_id in enumerate(player_ids):
        crash_service.join_queue(
            player_id,
            notification_context=build_notification_context(
                70_100 + index,
                channel_id=900_100,
                guild_id=910_100,
            ),
        )

    publisher = RecordingOutboxPublisher()

    async def scenario() -> StartupSyncResult:
        runtime = MatchingQueueRuntime.create(
            session_factory=session_factory,
            outbox_publisher=publisher,
            reconcile_interval=timedelta(hours=1),
            outbox_dispatcher_poll_interval=timedelta(milliseconds=10),
        )
        try:
            return await runtime.start()
        finally:
            await runtime.stop()

    startup_result = asyncio.run(scenario())

    session.expire_all()
    matches = session.scalars(select(Match).order_by(Match.id)).all()
    queue_entries = session.scalars(select(MatchQueueEntry).order_by(MatchQueueEntry.id)).all()

    assert len(startup_result.created_match_ids) == 1
    assert len(matches) == 1
    assert all(entry.status == MatchQueueEntryStatus.MATCHED for entry in queue_entries)
    assert [event.event_type for event in publisher.events] == [OutboxEventType.MATCH_CREATED]


def test_outbox_dispatcher_publishes_pending_events(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    # 直接対応するテスト項目はありません。
    # runtime の outbox dispatcher が pending event を非同期通知へ渡せることを補助的に確認します。
    session.add_all(
        [
            OutboxEvent(
                event_type=OutboxEventType.PRESENCE_REMINDER,
                dedupe_key="presence-reminder:1:1",
                payload={"queue_entry_id": 1},
            ),
            OutboxEvent(
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match-created:1",
                payload={"match_id": 1},
            ),
        ]
    )
    session.commit()

    publisher = RecordingOutboxPublisher()
    dispatcher = OutboxDispatcher(session_factory=session_factory, publisher=publisher)

    published_event_ids = asyncio.run(dispatcher.dispatch_once())

    session.expire_all()
    events = session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()

    expected_event_ids = tuple(event.id for event in events)
    assert published_event_ids == expected_event_ids
    assert [event.id for event in publisher.events] == list(expected_event_ids)
    assert all(event.published_at is not None for event in events)


def test_outbox_dispatcher_receives_listen_notify_events(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player_id = create_player(session, 75_001)
    queue_entry = MatchQueueEntry(
        player_id=player_id,
        status=MatchQueueEntryStatus.WAITING,
        joined_at=datetime.now(timezone.utc),
        last_present_at=datetime.now(timezone.utc),
        expire_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        revision=1,
        notification_channel_id=900_500,
        notification_guild_id=910_500,
        notification_mention_discord_user_id=75_001,
        notification_recorded_at=datetime.now(timezone.utc),
    )
    session.add(queue_entry)
    session.commit()

    publisher = RecordingOutboxPublisher()

    async def scenario() -> None:
        dispatcher = OutboxDispatcher(
            session_factory=session_factory,
            publisher=publisher,
            poll_interval=timedelta(hours=1),
        )
        service = MatchingQueueService(session_factory=session_factory)

        try:
            await dispatcher.start()
            reminder_result = await asyncio.to_thread(
                service.process_presence_reminder,
                queue_entry.id,
                1,
            )
            assert reminder_result.reminded is True
            await wait_until(lambda: len(publisher.events) == 1, timeout=2.0)
        finally:
            await dispatcher.stop()

    asyncio.run(scenario())

    assert [event.event_type for event in publisher.events] == [OutboxEventType.PRESENCE_REMINDER]


def test_outbox_dispatcher_fallback_poll_logs_warning_when_it_publishes(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = RecordingOutboxPublisher()
    listener_factory = FakeOutboxNotificationListenerFactory()

    async def scenario() -> None:
        dispatcher = OutboxDispatcher(
            session_factory=session_factory,
            publisher=publisher,
            poll_interval=timedelta(milliseconds=20),
            notification_listener_factory=listener_factory,
        )

        try:
            await dispatcher.start()
            session.add(
                OutboxEvent(
                    event_type=OutboxEventType.PRESENCE_REMINDER,
                    dedupe_key="presence-reminder:fallback",
                    payload={"queue_entry_id": 1},
                )
            )
            session.commit()
            await wait_until(
                lambda: (
                    len(publisher.events) == 1
                    and "Fallback outbox poll published events" in caplog.text
                ),
                timeout=1.0,
            )
        finally:
            await dispatcher.stop()

    with caplog.at_level(logging.WARNING, logger="bot.runtime.outbox"):
        asyncio.run(scenario())

    assert "Fallback outbox poll published events" in caplog.text
    assert listener_factory.listener is not None
    assert listener_factory.listener.started is True
    assert listener_factory.listener.stopped is True


def test_outbox_dispatcher_retries_temporary_failures_with_backoff_timer(
    session: Session,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session.add(
        OutboxEvent(
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key="match-created:retry",
            payload={"match_id": 1},
        )
    )
    session.commit()

    monkeypatch.setattr(
        outbox_runtime,
        "retry_delay_for_failure_count",
        lambda failure_count: timedelta(milliseconds=20),
    )
    publisher = FlakyOutboxPublisher(failures_remaining=1)
    listener_factory = FakeOutboxNotificationListenerFactory()

    async def scenario() -> None:
        dispatcher = OutboxDispatcher(
            session_factory=session_factory,
            publisher=publisher,
            poll_interval=timedelta(hours=1),
            notification_listener_factory=listener_factory,
        )
        try:
            await dispatcher.start()
            await wait_until(lambda: len(publisher.events) == 1, timeout=1.0)
        finally:
            await dispatcher.stop()

    asyncio.run(scenario())

    session.expire_all()
    outbox_event = session.scalar(select(OutboxEvent))
    assert outbox_event is not None
    assert publisher.attempts >= 2
    assert outbox_event.failure_count == 1
    assert outbox_event.published_at is not None
    assert outbox_event.last_error is None
    assert outbox_event.last_failed_at is None


def test_outbox_dispatcher_rebuilds_retry_timers_on_start(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    session.add(
        OutboxEvent(
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key="match-created:startup-retry",
            payload={"match_id": 2},
            failure_count=1,
            next_attempt_at=datetime.now(timezone.utc) + timedelta(milliseconds=20),
        )
    )
    session.commit()

    publisher = RecordingOutboxPublisher()
    listener_factory = FakeOutboxNotificationListenerFactory()

    async def scenario() -> None:
        dispatcher = OutboxDispatcher(
            session_factory=session_factory,
            publisher=publisher,
            poll_interval=timedelta(hours=1),
            notification_listener_factory=listener_factory,
        )
        try:
            await dispatcher.start()
            await wait_until(lambda: len(publisher.events) == 1, timeout=1.0)
        finally:
            await dispatcher.stop()

    asyncio.run(scenario())

    session.expire_all()
    outbox_event = session.scalar(select(OutboxEvent))
    assert outbox_event is not None
    assert outbox_event.published_at is not None
    assert [event.event_type for event in publisher.events] == [OutboxEventType.MATCH_CREATED]


def test_discord_outbox_publisher_sends_presence_reminder_to_payload_destination() -> None:
    channel_id = 900_001
    guild_id = 910_001
    mention_discord_user_id = 920_001
    channel = FakeDiscordChannel(id=channel_id, guild=FakeDiscordGuild(id=guild_id))
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    asyncio.run(
        publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=1,
                event_type=OutboxEventType.PRESENCE_REMINDER,
                dedupe_key="presence_reminder:1:1",
                payload={
                    "queue_entry_id": 101,
                    "player_id": 80_001,
                    "revision": 1,
                    "expire_at": datetime.now(timezone.utc).isoformat(),
                    "destination": {
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": mention_discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert channel.sent_messages == [
        f"<@{mention_discord_user_id}> {PRESENCE_REMINDER_NOTIFICATION_MESSAGE}",
    ]
    assert channel.allowed_mentions_history[0].users is True


def test_discord_outbox_publisher_fetches_uncached_channel_for_queue_expired() -> None:
    channel_id = 900_002
    guild_id = 910_002
    mention_discord_user_id = 920_002
    channel = FakeDiscordChannel(id=channel_id, guild=FakeDiscordGuild(id=guild_id))
    client = FakeDiscordClient(
        channels={channel.id: channel},
        uncached_channel_ids={channel.id},
    )
    publisher = DiscordOutboxEventPublisher(client=client)

    asyncio.run(
        publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=2,
                event_type=OutboxEventType.QUEUE_EXPIRED,
                dedupe_key="queue_expired:1:2",
                payload={
                    "queue_entry_id": 102,
                    "player_id": 80_002,
                    "revision": 2,
                    "expire_at": datetime.now(timezone.utc).isoformat(),
                    "destination": {
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": mention_discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert client.fetched_channel_ids == [channel.id]
    assert channel.sent_messages == [
        f"<@{mention_discord_user_id}> {QUEUE_EXPIRED_NOTIFICATION_MESSAGE}",
    ]


def test_discord_outbox_publisher_renders_dummy_prefix_for_dummy_user_id() -> None:
    channel_id = 900_020
    guild_id = 910_020
    dummy_discord_user_id = 777
    channel = FakeDiscordChannel(id=channel_id, guild=FakeDiscordGuild(id=guild_id))
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    asyncio.run(
        publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=20,
                event_type=OutboxEventType.PRESENCE_REMINDER,
                dedupe_key="presence_reminder:20:1",
                payload={
                    "queue_entry_id": 120,
                    "player_id": 80_020,
                    "revision": 1,
                    "expire_at": datetime.now(timezone.utc).isoformat(),
                    "destination": {
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": dummy_discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert channel.sent_messages == [
        f"<dummy_{dummy_discord_user_id}> {PRESENCE_REMINDER_NOTIFICATION_MESSAGE}",
    ]


def test_discord_outbox_publisher_sends_split_match_created_events() -> None:
    team_a_discord_user_ids = [80_100, 777, 80_102]
    team_b_discord_user_ids = [888, 80_104, 80_105]
    first_channel_id = 900_010
    first_guild_id = 910_010
    second_channel_id = 900_011
    second_guild_id = 910_011
    first_channel = FakeDiscordChannel(
        id=first_channel_id,
        guild=FakeDiscordGuild(id=first_guild_id),
    )
    second_channel = FakeDiscordChannel(
        id=second_channel_id,
        guild=FakeDiscordGuild(id=second_guild_id),
    )
    client = FakeDiscordClient(
        channels={
            first_channel.id: first_channel,
            second_channel.id: second_channel,
        }
    )
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            MATCH_CREATED_NOTIFICATION_MESSAGE,
            "Team A",
            f"    <@{team_a_discord_user_ids[0]}>",
            f"    <dummy_{team_a_discord_user_ids[1]}>",
            f"    <@{team_a_discord_user_ids[2]}>",
            "Team B",
            f"    <dummy_{team_b_discord_user_ids[0]}>",
            f"    <@{team_b_discord_user_ids[1]}>",
            f"    <@{team_b_discord_user_ids[2]}>",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=3,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:1:900010",
                payload={
                    "match_id": 1,
                    "destination": {
                        "channel_id": first_channel_id,
                        "guild_id": first_guild_id,
                    },
                    "team_a_discord_user_ids": team_a_discord_user_ids,
                    "team_b_discord_user_ids": team_b_discord_user_ids,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=4,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:1:900011",
                payload={
                    "match_id": 1,
                    "destination": {
                        "channel_id": second_channel_id,
                        "guild_id": second_guild_id,
                    },
                    "team_a_discord_user_ids": team_a_discord_user_ids,
                    "team_b_discord_user_ids": team_b_discord_user_ids,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert first_channel.sent_messages == [
        expected_message,
    ]
    assert second_channel.sent_messages == [
        expected_message,
    ]


def test_discord_outbox_publisher_raises_when_destination_is_missing() -> None:
    publisher = DiscordOutboxEventPublisher(client=FakeDiscordClient())

    with pytest.raises(ValueError, match="destination"):
        asyncio.run(
            publish_with_bound_loop(
                publisher,
                PendingOutboxEvent(
                    id=4,
                    event_type=OutboxEventType.PRESENCE_REMINDER,
                    dedupe_key="presence_reminder:2:1",
                    payload={"mention_discord_user_id": 80_003},
                    created_at=datetime.now(timezone.utc),
                ),
            )
        )
