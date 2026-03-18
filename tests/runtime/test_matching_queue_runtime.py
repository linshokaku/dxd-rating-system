from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from unittest.mock import AsyncMock, Mock, call

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import bot.runtime.matching_queue as matching_queue_runtime
import bot.runtime.outbox as outbox_runtime
from bot.models import Match, MatchQueueEntry, MatchQueueEntryStatus, OutboxEvent, OutboxEventType
from bot.notifications import DiscordOutboxEventPublisher
from bot.runtime import (
    BotRuntime,
    BotRuntimeStartResult,
    MatchingQueueRuntime,
    NoopOutboxDispatcher,
    OutboxDispatcher,
    OutboxStartupResult,
    PendingOutboxEvent,
    StartupSyncResult,
)
from bot.services import (
    MATCH_CREATED_NOTIFICATION_MESSAGE,
    PRESENCE_REMINDER_LEAD_TIME,
    PRESENCE_REMINDER_NOTIFICATION_MESSAGE,
    QUEUE_EXPIRED_NOTIFICATION_MESSAGE,
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
    register_player,
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


def create_waiting_queue_entry(
    session: Session,
    *,
    player_id: int,
    expire_at: datetime,
    revision: int = 1,
    last_reminded_revision: int | None = None,
) -> MatchQueueEntry:
    current_time = datetime.now(timezone.utc)
    queue_entry = MatchQueueEntry(
        player_id=player_id,
        status=MatchQueueEntryStatus.WAITING,
        joined_at=current_time,
        last_present_at=current_time,
        expire_at=expire_at,
        revision=revision,
        last_reminded_revision=last_reminded_revision,
        notification_channel_id=900_000 + player_id,
        notification_guild_id=910_000 + player_id,
        notification_mention_discord_user_id=920_000 + player_id,
        notification_recorded_at=current_time,
    )
    session.add(queue_entry)
    session.commit()
    return queue_entry


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


def test_matching_queue_runtime_join_queue_calls_service_and_schedules_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    notification_context = build_notification_context(60_001)
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    join_result = JoinQueueResult(queue_entry_id=101, revision=3, expire_at=expire_at)
    service.join_queue.return_value = join_result
    runtime = MatchingQueueRuntime(service=service)
    handler_calls: list[dict[str, object]] = []
    scheduled: list[dict[str, object]] = []
    try_create_matches_contexts: list[str] = []

    def fake_handler_call(
        handler: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> Callable[[], Awaitable[object]]:
        handler_calls.append(
            {
                "handler_name": handler.__name__,
                "args": args,
                "kwargs": kwargs,
            }
        )

        async def call_handler() -> object:
            return None

        return call_handler

    def fake_schedule_task(
        *,
        key: object,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[object]],
    ) -> bool:
        scheduled.append(
            {
                "key": key,
                "task_name": task_name,
                "scheduled_at": scheduled_at,
                "deadline": deadline,
                "handler_call": handler_call,
            }
        )
        return True

    monkeypatch.setattr(runtime, "_handler_call", fake_handler_call)
    monkeypatch.setattr(runtime, "_schedule_task", fake_schedule_task)

    async def fake_try_create_matches_safely(*, context: str) -> tuple[CreatedMatchResult, ...]:
        try_create_matches_contexts.append(context)
        return tuple()

    monkeypatch.setattr(runtime, "_try_create_matches_safely", fake_try_create_matches_safely)

    result = asyncio.run(
        runtime.join_queue(
            5001,
            notification_context=notification_context,
        )
    )

    assert result == join_result
    service.join_queue.assert_called_once_with(5001, notification_context=notification_context)
    assert handler_calls == [
        {
            "handler_name": "process_presence_reminder",
            "args": (join_result.queue_entry_id, join_result.revision),
            "kwargs": {},
        },
        {
            "handler_name": "process_expire",
            "args": (join_result.queue_entry_id, join_result.revision),
            "kwargs": {},
        },
    ]
    assert [scheduled_item | {"handler_call": None} for scheduled_item in scheduled] == [
        {
            "key": runtime._presence_reminder_task_key(join_result.queue_entry_id),
            "task_name": "presence reminder",
            "scheduled_at": join_result.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            "deadline": join_result.expire_at,
            "handler_call": None,
        },
        {
            "key": runtime._expire_task_key(join_result.queue_entry_id),
            "task_name": "expire",
            "scheduled_at": join_result.expire_at,
            "deadline": None,
            "handler_call": None,
        },
    ]
    assert try_create_matches_contexts == ["join"]


def test_matching_queue_runtime_present_calls_service_and_replaces_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    notification_context = build_notification_context(60_101)
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    present_result = PresentQueueResult(
        queue_entry_id=201,
        revision=4,
        expire_at=expire_at,
        expired=False,
        message="updated",
    )
    service.present.return_value = present_result
    runtime = MatchingQueueRuntime(service=service)
    cancelled_keys: list[object] = []
    handler_calls: list[dict[str, object]] = []
    scheduled: list[dict[str, object]] = []

    def fake_handler_call(
        handler: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> Callable[[], Awaitable[object]]:
        handler_calls.append(
            {
                "handler_name": handler.__name__,
                "args": args,
                "kwargs": kwargs,
            }
        )

        async def call_handler() -> object:
            return None

        return call_handler

    def fake_schedule_task(
        *,
        key: object,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[object]],
    ) -> bool:
        scheduled.append(
            {
                "key": key,
                "task_name": task_name,
                "scheduled_at": scheduled_at,
                "deadline": deadline,
                "handler_call": handler_call,
            }
        )
        return True

    monkeypatch.setattr(runtime, "_cancel_scheduled_task", lambda key: cancelled_keys.append(key))
    monkeypatch.setattr(runtime, "_handler_call", fake_handler_call)
    monkeypatch.setattr(runtime, "_schedule_task", fake_schedule_task)

    result = asyncio.run(
        runtime.present(
            5002,
            notification_context=notification_context,
        )
    )

    assert result == present_result
    service.present.assert_called_once_with(5002, notification_context=notification_context)
    assert cancelled_keys == [
        runtime._presence_reminder_task_key(present_result.queue_entry_id),
        runtime._expire_task_key(present_result.queue_entry_id),
    ]
    assert handler_calls == [
        {
            "handler_name": "process_presence_reminder",
            "args": (present_result.queue_entry_id, present_result.revision),
            "kwargs": {},
        },
        {
            "handler_name": "process_expire",
            "args": (present_result.queue_entry_id, present_result.revision),
            "kwargs": {},
        },
    ]
    assert [scheduled_item | {"handler_call": None} for scheduled_item in scheduled] == [
        {
            "key": runtime._presence_reminder_task_key(present_result.queue_entry_id),
            "task_name": "presence reminder",
            "scheduled_at": present_result.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            "deadline": present_result.expire_at,
            "handler_call": None,
        },
        {
            "key": runtime._expire_task_key(present_result.queue_entry_id),
            "task_name": "expire",
            "scheduled_at": present_result.expire_at,
            "deadline": None,
            "handler_call": None,
        },
    ]


def test_matching_queue_runtime_present_cancels_timers_when_entry_already_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    present_result = PresentQueueResult(
        queue_entry_id=202,
        revision=None,
        expire_at=None,
        expired=True,
        message="expired",
    )
    service.present.return_value = present_result
    runtime = MatchingQueueRuntime(service=service)
    cancelled_keys: list[object] = []

    monkeypatch.setattr(
        runtime,
        "_cancel_scheduled_task",
        lambda key: cancelled_keys.append(key),
    )

    result = asyncio.run(runtime.present(5003))

    assert result == present_result
    service.present.assert_called_once_with(5003, notification_context=None)
    assert cancelled_keys == [
        runtime._presence_reminder_task_key(present_result.queue_entry_id),
        runtime._expire_task_key(present_result.queue_entry_id),
    ]


def test_matching_queue_runtime_leave_calls_service_and_cancels_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    leave_result = LeaveQueueResult(
        queue_entry_id=301,
        expired=False,
        message="left",
    )
    service.leave.return_value = leave_result
    runtime = MatchingQueueRuntime(service=service)
    cancelled_keys: list[object] = []

    monkeypatch.setattr(
        runtime,
        "_cancel_scheduled_task",
        lambda key: cancelled_keys.append(key),
    )

    result = asyncio.run(runtime.leave(5004))

    assert result == leave_result
    service.leave.assert_called_once_with(5004)
    assert cancelled_keys == [
        runtime._presence_reminder_task_key(leave_result.queue_entry_id),
        runtime._expire_task_key(leave_result.queue_entry_id),
    ]


def test_matching_queue_runtime_process_expire_calls_service_and_cancels_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    expire_result = ExpireQueueEntryResult(queue_entry_id=401, expired=True)
    service.process_expire.return_value = expire_result
    runtime = MatchingQueueRuntime(service=service)
    cancelled_keys: list[object] = []

    monkeypatch.setattr(
        runtime,
        "_cancel_scheduled_task",
        lambda key: cancelled_keys.append(key),
    )

    result = asyncio.run(runtime.process_expire(401, 7))

    assert result == expire_result
    service.process_expire.assert_called_once_with(401, 7)
    assert cancelled_keys == [
        runtime._presence_reminder_task_key(401),
        runtime._expire_task_key(401),
    ]


def test_matching_queue_runtime_run_startup_sync_calls_service_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    snapshot_time = datetime.now(timezone.utc)
    due_entry = WaitingEntryTimerState(
        queue_entry_id=501,
        revision=2,
        expire_at=snapshot_time + timedelta(seconds=30),
        last_reminded_revision=None,
    )
    future_entry = WaitingEntryTimerState(
        queue_entry_id=502,
        revision=3,
        expire_at=snapshot_time + timedelta(minutes=3),
        last_reminded_revision=None,
    )
    reminded_entry = WaitingEntryTimerState(
        queue_entry_id=503,
        revision=4,
        expire_at=snapshot_time + timedelta(minutes=4),
        last_reminded_revision=4,
    )
    created_match = CreatedMatchResult(
        match_id=77,
        queue_entry_ids=(601, 602, 603, 604, 605, 606),
        player_ids=(701, 702, 703, 704, 705, 706),
    )
    service.cleanup_expired_entries.return_value = (401,)
    service.try_create_matches.return_value = (created_match,)
    service.load_waiting_entry_timer_states.return_value = (
        snapshot_time,
        (due_entry, future_entry, reminded_entry),
    )
    service.process_presence_reminder.return_value = PresenceReminderResult(
        queue_entry_id=due_entry.queue_entry_id,
        reminded=True,
    )
    runtime = MatchingQueueRuntime(service=service)
    cancelled_keys: list[object] = []
    handler_calls: list[dict[str, object]] = []
    scheduled_tasks: list[dict[str, object]] = []

    def fake_handler_call(
        handler: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> Callable[[], Awaitable[object]]:
        handler_calls.append(
            {
                "handler_name": handler.__name__,
                "args": args,
                "kwargs": kwargs,
            }
        )

        async def call_handler() -> object:
            return None

        return call_handler

    def fake_schedule_task(
        *,
        key: object,
        task_name: str,
        scheduled_at: datetime,
        deadline: datetime | None,
        handler_call: Callable[[], Awaitable[object]],
    ) -> bool:
        scheduled_tasks.append(
            {
                "key": key,
                "task_name": task_name,
                "scheduled_at": scheduled_at,
                "deadline": deadline,
                "handler_call": handler_call,
            }
        )
        return True

    monkeypatch.setattr(runtime, "_cancel_scheduled_task", lambda key: cancelled_keys.append(key))
    monkeypatch.setattr(runtime, "_handler_call", fake_handler_call)
    monkeypatch.setattr(runtime, "_schedule_task", fake_schedule_task)

    result = asyncio.run(runtime.run_startup_sync())

    service.cleanup_expired_entries.assert_called_once_with(warn_on_cleanup=False)
    service.try_create_matches.assert_called_once_with()
    service.load_waiting_entry_timer_states.assert_called_once_with()
    service.process_presence_reminder.assert_called_once_with(
        due_entry.queue_entry_id,
        due_entry.revision,
    )
    assert result == StartupSyncResult(
        cleaned_up_queue_entry_ids=(401,),
        reminded_queue_entry_ids=(due_entry.queue_entry_id,),
        rescheduled_reminder_queue_entry_ids=(future_entry.queue_entry_id,),
        rescheduled_expire_queue_entry_ids=(
            due_entry.queue_entry_id,
            future_entry.queue_entry_id,
            reminded_entry.queue_entry_id,
        ),
        created_match_ids=(created_match.match_id,),
    )
    assert cancelled_keys == [
        runtime._presence_reminder_task_key(401),
        runtime._expire_task_key(401),
        *[
            key
            for queue_entry_id in created_match.queue_entry_ids
            for key in (
                runtime._presence_reminder_task_key(queue_entry_id),
                runtime._expire_task_key(queue_entry_id),
            )
        ],
        runtime._expire_task_key(due_entry.queue_entry_id),
        runtime._presence_reminder_task_key(future_entry.queue_entry_id),
        runtime._expire_task_key(future_entry.queue_entry_id),
        runtime._expire_task_key(reminded_entry.queue_entry_id),
    ]
    assert handler_calls == [
        {
            "handler_name": "process_expire",
            "args": (due_entry.queue_entry_id, due_entry.revision),
            "kwargs": {},
        },
        {
            "handler_name": "process_presence_reminder",
            "args": (future_entry.queue_entry_id, future_entry.revision),
            "kwargs": {},
        },
        {
            "handler_name": "process_expire",
            "args": (future_entry.queue_entry_id, future_entry.revision),
            "kwargs": {},
        },
        {
            "handler_name": "process_expire",
            "args": (reminded_entry.queue_entry_id, reminded_entry.revision),
            "kwargs": {},
        },
    ]
    assert [scheduled_item | {"handler_call": None} for scheduled_item in scheduled_tasks] == [
        {
            "key": runtime._expire_task_key(due_entry.queue_entry_id),
            "task_name": "expire",
            "scheduled_at": due_entry.expire_at,
            "deadline": None,
            "handler_call": None,
        },
        {
            "key": runtime._presence_reminder_task_key(future_entry.queue_entry_id),
            "task_name": "presence reminder",
            "scheduled_at": future_entry.expire_at - PRESENCE_REMINDER_LEAD_TIME,
            "deadline": future_entry.expire_at,
            "handler_call": None,
        },
        {
            "key": runtime._expire_task_key(future_entry.queue_entry_id),
            "task_name": "expire",
            "scheduled_at": future_entry.expire_at,
            "deadline": None,
            "handler_call": None,
        },
        {
            "key": runtime._expire_task_key(reminded_entry.queue_entry_id),
            "task_name": "expire",
            "scheduled_at": reminded_entry.expire_at,
            "deadline": None,
            "handler_call": None,
        },
    ]


def test_matching_queue_runtime_run_reconcile_cycle_passes_warn_on_cleanup() -> None:
    service = Mock()
    snapshot_time = datetime.now(timezone.utc)
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.return_value = (snapshot_time, tuple())
    runtime = MatchingQueueRuntime(service=service)

    result = asyncio.run(runtime.run_reconcile_cycle())

    service.cleanup_expired_entries.assert_called_once_with(warn_on_cleanup=True)
    service.try_create_matches.assert_called_once_with()
    service.load_waiting_entry_timer_states.assert_called_once_with()
    assert result == StartupSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
    )


def test_matching_queue_runtime_runs_due_scheduled_tasks() -> None:
    reminder_event = threading.Event()
    expire_event = threading.Event()
    service = Mock()

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    def handle_expire(queue_entry_id: int, expected_revision: int) -> ExpireQueueEntryResult:
        expire_event.set()
        return ExpireQueueEntryResult(queue_entry_id=queue_entry_id, expired=True)

    service.process_presence_reminder.side_effect = handle_presence_reminder
    service.process_expire.side_effect = handle_expire
    runtime = MatchingQueueRuntime(service=service)

    async def scenario() -> None:
        runtime.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        remind_at = current_time + timedelta(milliseconds=20)
        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(101),
            task_name="presence reminder",
            scheduled_at=remind_at,
            deadline=remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 101, 3),
        )
        assert runtime._schedule_task(
            key=runtime._expire_task_key(202),
            task_name="expire",
            scheduled_at=current_time + timedelta(milliseconds=30),
            deadline=None,
            handler_call=runtime._handler_call(runtime.process_expire, 202, 4),
        )

        assert await asyncio.to_thread(reminder_event.wait, 1.0)
        assert await asyncio.to_thread(expire_event.wait, 1.0)
        await runtime._aclose_scheduled_tasks()

    asyncio.run(scenario())

    service.process_presence_reminder.assert_called_once_with(101, 3)
    service.process_expire.assert_called_once_with(202, 4)


def test_matching_queue_runtime_cancels_pending_tasks() -> None:
    reminder_event = threading.Event()
    expire_event = threading.Event()
    service = Mock()

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

    service.process_presence_reminder.side_effect = handle_presence_reminder
    service.process_expire.side_effect = handle_expire
    runtime = MatchingQueueRuntime(service=service)

    async def scenario() -> None:
        runtime.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        remind_at = current_time + timedelta(milliseconds=50)
        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(1),
            task_name="presence reminder",
            scheduled_at=remind_at,
            deadline=remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 1, 1),
        )
        assert runtime._schedule_task(
            key=runtime._expire_task_key(2),
            task_name="expire",
            scheduled_at=current_time + timedelta(milliseconds=50),
            deadline=None,
            handler_call=runtime._handler_call(runtime.process_expire, 2, 1),
        )
        runtime._cancel_scheduled_task(runtime._presence_reminder_task_key(1))
        runtime._cancel_scheduled_task(runtime._expire_task_key(2))

        await asyncio.sleep(0.1)
        await runtime._aclose_scheduled_tasks()

    asyncio.run(scenario())

    assert not reminder_event.is_set()
    assert not expire_event.is_set()
    service.process_presence_reminder.assert_not_called()
    service.process_expire.assert_not_called()


def test_matching_queue_runtime_retries_retryable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_event = threading.Event()
    expire_event = threading.Event()
    reminder_attempts = 0
    expire_attempts = 0
    service = Mock()

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

    service.process_presence_reminder.side_effect = handle_presence_reminder
    service.process_expire.side_effect = handle_expire
    runtime = MatchingQueueRuntime(service=service)
    monkeypatch.setattr(
        matching_queue_runtime,
        "retry_delay_for_failure_count",
        lambda failure_count: timedelta(milliseconds=10),
    )

    async def scenario() -> None:
        runtime.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        remind_at = current_time + timedelta(milliseconds=10)
        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(301),
            task_name="presence reminder",
            scheduled_at=remind_at,
            deadline=remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 301, 7),
        )
        assert runtime._schedule_task(
            key=runtime._expire_task_key(302),
            task_name="expire",
            scheduled_at=current_time + timedelta(milliseconds=10),
            deadline=None,
            handler_call=runtime._handler_call(runtime.process_expire, 302, 8),
        )

        assert await asyncio.to_thread(reminder_event.wait, 1.0)
        assert await asyncio.to_thread(expire_event.wait, 1.0)
        await runtime._aclose_scheduled_tasks()

    asyncio.run(scenario())

    assert reminder_attempts == 2
    assert expire_attempts == 2
    assert service.process_presence_reminder.call_args_list == [call(301, 7), call(301, 7)]
    assert service.process_expire.call_args_list == [call(302, 8), call(302, 8)]


def test_matching_queue_runtime_stops_presence_retry_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_attempts = 0
    service = Mock()

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        nonlocal reminder_attempts
        del queue_entry_id, expected_revision
        reminder_attempts += 1
        raise RetryableTaskError("temporary presence reminder failure")

    service.process_presence_reminder.side_effect = handle_presence_reminder
    service.process_expire.return_value = ExpireQueueEntryResult(queue_entry_id=2, expired=False)
    runtime = MatchingQueueRuntime(service=service)
    monkeypatch.setattr(
        matching_queue_runtime,
        "retry_delay_for_failure_count",
        lambda failure_count: timedelta(milliseconds=20),
    )

    async def scenario() -> None:
        runtime.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)
        remind_at = current_time - PRESENCE_REMINDER_LEAD_TIME + timedelta(milliseconds=5)

        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(401),
            task_name="presence reminder",
            scheduled_at=remind_at,
            deadline=remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 401, 2),
        )
        await asyncio.sleep(0.05)
        await runtime._aclose_scheduled_tasks()

    asyncio.run(scenario())

    assert reminder_attempts == 1


def test_matching_queue_runtime_replaces_pending_retry_when_rescheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reminder_event = threading.Event()
    calls: list[tuple[int, int]] = []
    service = Mock()

    def handle_presence_reminder(
        queue_entry_id: int, expected_revision: int
    ) -> PresenceReminderResult:
        calls.append((queue_entry_id, expected_revision))
        if expected_revision == 1:
            raise RetryableTaskError("temporary presence reminder failure")
        reminder_event.set()
        return PresenceReminderResult(queue_entry_id=queue_entry_id, reminded=True)

    service.process_presence_reminder.side_effect = handle_presence_reminder
    service.process_expire.return_value = ExpireQueueEntryResult(queue_entry_id=2, expired=False)
    runtime = MatchingQueueRuntime(service=service)
    monkeypatch.setattr(
        matching_queue_runtime,
        "retry_delay_for_failure_count",
        lambda failure_count: timedelta(milliseconds=50),
    )

    async def scenario() -> None:
        runtime.bind_loop(asyncio.get_running_loop())
        current_time = datetime.now(timezone.utc)

        first_remind_at = current_time + timedelta(milliseconds=10)
        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(501),
            task_name="presence reminder",
            scheduled_at=first_remind_at,
            deadline=first_remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 501, 1),
        )
        await wait_until(lambda: calls == [(501, 1)], timeout=1.0)

        second_remind_at = datetime.now(timezone.utc) + timedelta(milliseconds=10)
        assert runtime._schedule_task(
            key=runtime._presence_reminder_task_key(501),
            task_name="presence reminder",
            scheduled_at=second_remind_at,
            deadline=second_remind_at + PRESENCE_REMINDER_LEAD_TIME,
            handler_call=runtime._handler_call(runtime.process_presence_reminder, 501, 2),
        )

        assert await asyncio.to_thread(reminder_event.wait, 1.0)
        await asyncio.sleep(0.08)
        await runtime._aclose_scheduled_tasks()

    asyncio.run(scenario())

    assert calls == [(501, 1), (501, 2)]


def test_matching_queue_runtime_runs_startup_sync_and_reconcile_loop() -> None:
    service = Mock()
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.side_effect = lambda: (
        datetime.now(timezone.utc),
        tuple(),
    )
    runtime = MatchingQueueRuntime(
        service=service,
        reconcile_interval=timedelta(milliseconds=20),
    )

    async def scenario() -> None:
        await runtime.start()
        await asyncio.sleep(0.08)
        await runtime.stop()

    asyncio.run(scenario())

    assert service.cleanup_expired_entries.call_args_list[0] == call(warn_on_cleanup=False)
    assert any(
        cleanup_call == call(warn_on_cleanup=True)
        for cleanup_call in service.cleanup_expired_entries.call_args_list[1:]
    )
    assert service.try_create_matches.call_count >= 2
    assert service.load_waiting_entry_timer_states.call_count >= 2


def test_bot_runtime_starts_and_stops_matching_queue_and_outbox() -> None:
    startup_result = StartupSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=(1,),
    )
    matching_queue_runtime = Mock()
    matching_queue_runtime.start = AsyncMock(return_value=startup_result)
    matching_queue_runtime.stop = AsyncMock(return_value=None)
    outbox_dispatcher = Mock()
    outbox_dispatcher.start = AsyncMock(return_value=OutboxStartupResult(published_event_ids=(99,)))
    outbox_dispatcher.stop = AsyncMock(return_value=None)
    runtime = BotRuntime(
        matching_queue_runtime=matching_queue_runtime,
        outbox_dispatcher=outbox_dispatcher,
    )

    async def scenario() -> tuple[BotRuntimeStartResult, asyncio.AbstractEventLoop]:
        result = await runtime.start()
        loop = asyncio.get_running_loop()
        await runtime.stop()
        return result, loop

    result, loop = asyncio.run(scenario())

    assert result == BotRuntimeStartResult(
        matching_queue=startup_result,
        outbox=OutboxStartupResult(published_event_ids=(99,)),
    )
    matching_queue_runtime.start.assert_awaited_once_with()
    matching_queue_runtime.stop.assert_awaited_once_with()
    outbox_dispatcher.bind_loop.assert_called_once()
    assert outbox_dispatcher.bind_loop.call_args.args[0] is loop
    outbox_dispatcher.start.assert_awaited_once_with()
    outbox_dispatcher.stop.assert_awaited_once_with()


def test_bot_runtime_rolls_back_matching_queue_when_outbox_start_fails() -> None:
    startup_result = StartupSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
    )
    matching_queue_runtime = Mock()
    matching_queue_runtime.start = AsyncMock(return_value=startup_result)
    matching_queue_runtime.stop = AsyncMock(return_value=None)
    outbox_dispatcher = Mock()
    outbox_dispatcher.start = AsyncMock(side_effect=RuntimeError("outbox start failed"))
    outbox_dispatcher.stop = AsyncMock(return_value=None)
    runtime = BotRuntime(
        matching_queue_runtime=matching_queue_runtime,
        outbox_dispatcher=outbox_dispatcher,
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="outbox start failed"):
            await runtime.start()

    asyncio.run(scenario())

    matching_queue_runtime.start.assert_awaited_once_with()
    matching_queue_runtime.stop.assert_awaited_once_with()
    outbox_dispatcher.start.assert_awaited_once_with()
    outbox_dispatcher.stop.assert_not_awaited()


def test_runtime_startup_sync_recovers_missing_tasks_after_join_commit(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    # 対応するテスト項目:
    # - `join` commit 後にプロセスが落ちてタスク未登録になっても、
    #   起動時再同期で復旧できること
    player_id = create_player(session, 70_001)
    crash_service = MatchingQueueService(session_factory=session_factory)
    join_result = crash_service.join_queue(
        player_id,
        notification_context=build_notification_context(70_001),
    )

    async def scenario() -> BotRuntimeStartResult:
        runtime = BotRuntime(
            matching_queue_runtime=MatchingQueueRuntime.create(
                session_factory=session_factory,
                reconcile_interval=timedelta(hours=1),
            ),
            outbox_dispatcher=NoopOutboxDispatcher(),
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
    assert startup_result.matching_queue.rescheduled_reminder_queue_entry_ids == (
        join_result.queue_entry_id,
    )
    assert startup_result.matching_queue.rescheduled_expire_queue_entry_ids == (
        join_result.queue_entry_id,
    )


def test_runtime_startup_sync_recovers_missing_match_attempt_after_join_commit(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    # 対応するテスト項目:
    # - `join` commit 後にプロセスが落ちてマッチング試行が走らなくても、
    #   起動時再同期の `try_create_matches()` で回収できること
    player_ids = create_players(session, 6, start_discord_user_id=70_100)
    crash_service = MatchingQueueService(session_factory=session_factory)
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

    async def scenario() -> BotRuntimeStartResult:
        runtime = BotRuntime(
            matching_queue_runtime=MatchingQueueRuntime.create(
                session_factory=session_factory,
                reconcile_interval=timedelta(hours=1),
            ),
            outbox_dispatcher=OutboxDispatcher(
                session_factory=session_factory,
                publisher=publisher,
                poll_interval=timedelta(milliseconds=10),
            ),
        )
        try:
            return await runtime.start()
        finally:
            await runtime.stop()

    startup_result = asyncio.run(scenario())

    session.expire_all()
    matches = session.scalars(select(Match).order_by(Match.id)).all()
    queue_entries = session.scalars(select(MatchQueueEntry).order_by(MatchQueueEntry.id)).all()

    assert len(startup_result.matching_queue.created_match_ids) == 1
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
