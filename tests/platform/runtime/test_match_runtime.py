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

import dxd_rating.platform.runtime.match_runtime as match_runtime_module
import dxd_rating.platform.runtime.outbox as outbox_runtime
from dxd_rating.contexts.common.application import RetryableTaskError
from dxd_rating.contexts.matches.application import (
    MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE,
    MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE,
    MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE,
    MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE,
    MATCH_FINALIZED_NOTIFICATION_MESSAGE,
    MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE,
    MATCH_REPORT_OPENED_NOTIFICATION_MESSAGE,
    ActiveMatchTimerState,
    MatchApprovalResult,
    MatchFinalizationResult,
    MatchParentAssignmentResult,
    MatchReportSubmissionResult,
)
from dxd_rating.contexts.matchmaking.application import (
    MATCH_CREATED_NOTIFICATION_MESSAGE,
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
    WaitingEntryTimerState,
)
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.seasons.application import ensure_active_and_upcoming_seasons
from dxd_rating.platform.db.models import (
    Match,
    MatchApprovalStatus,
    MatchFormat,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchReportInputResult,
    MatchResult,
    MatchState,
    OutboxEvent,
    OutboxEventType,
)
from dxd_rating.platform.discord.rest import DiscordOutboxEventPublisher
from dxd_rating.platform.discord.ui import (
    MATCH_OPERATION_THREAD_APPROVE_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_APPROVE_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_DRAW_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_LOSE_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL,
    MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE,
    MATCH_OPERATION_THREAD_WIN_BUTTON_CUSTOM_ID_PREFIX,
    MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL,
    MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_CUSTOM_ID_PREFIX,
    MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
)
from dxd_rating.platform.runtime import (
    BotRuntime,
    BotRuntimeStartResult,
    MatchRuntime,
    MatchRuntimeSyncResult,
    NonRetryableOutboxPublishError,
    NoopOutboxDispatcher,
    OutboxDispatcher,
    OutboxStartupResult,
    PendingOutboxEvent,
)
from dxd_rating.shared.constants import (
    MATCH_QUEUE_TTL,
    PRESENCE_REMINDER_LEAD_TIME,
    get_match_queue_class_definition_by_name,
)

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_DEFINITION = get_match_queue_class_definition_by_name(
    DEFAULT_MATCH_FORMAT,
    "beginner",
)
assert DEFAULT_QUEUE_DEFINITION is not None
DEFAULT_QUEUE_NAME = DEFAULT_QUEUE_DEFINITION.queue_name
DEFAULT_QUEUE_CLASS_ID = DEFAULT_QUEUE_DEFINITION.queue_class_id


@pytest.fixture(autouse=True)
def prepared_seasons(session: Session) -> None:
    ensure_active_and_upcoming_seasons(session)
    session.commit()


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
class DiscardingOutboxPublisher:
    attempts: int = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        del loop

    def publish(self, event: PendingOutboxEvent) -> None:
        self.attempts += 1
        raise NonRetryableOutboxPublishError(f"Discord channel not found: {event.id}")


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


@dataclass(frozen=True)
class FakeHttpResponse:
    status: int
    reason: str
    text: str


def make_not_found() -> discord.NotFound:
    return discord.NotFound(
        FakeHttpResponse(status=404, reason="Not Found", text="Not Found"),
        "Not Found",
    )


@dataclass
class FakeDiscordHttpClientState:
    _HTTPClient__session: object = field(default_factory=object)
    proxy_auth: object | None = None
    proxy: str | None = None
    token: str = "bot-token"


@dataclass
class FakeDiscordConnectionState:
    http: FakeDiscordHttpClientState = field(default_factory=FakeDiscordHttpClientState)


@dataclass
class FakeDiscordChannel:
    id: int
    name: str = ""
    guild: FakeDiscordGuild | None = None
    parent: object | None = None
    sent_messages: list[str] = field(default_factory=list)
    allowed_mentions_history: list[discord.AllowedMentions] = field(default_factory=list)
    sent_views: list[discord.ui.View | None] = field(default_factory=list)
    created_threads: list["FakeDiscordThread"] = field(default_factory=list)

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions,
        view: discord.ui.View | None = None,
    ) -> None:
        self.sent_messages.append(content)
        self.allowed_mentions_history.append(allowed_mentions)
        self.sent_views.append(view)

    async def create_thread(
        self,
        *,
        name: str,
        **_: object,
    ) -> object:
        thread = FakeDiscordThread(
            id=(self.id * 1000) + len(self.created_threads) + 1,
            name=name,
            guild=self.guild,
            parent=self,
        )
        self.created_threads.append(thread)
        return thread


@dataclass
class FakeDiscordThread:
    id: int
    name: str
    guild: FakeDiscordGuild | None = None
    parent: object | None = None
    sent_messages: list[str] = field(default_factory=list)
    allowed_mentions_history: list[discord.AllowedMentions] = field(default_factory=list)
    sent_views: list[discord.ui.View | None] = field(default_factory=list)
    added_user_ids: list[int] = field(default_factory=list)

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: discord.AllowedMentions,
        view: discord.ui.View | None = None,
    ) -> None:
        self.sent_messages.append(content)
        self.allowed_mentions_history.append(allowed_mentions)
        self.sent_views.append(view)

    async def add_user(self, user: object) -> None:
        user_id = getattr(user, "id", None)
        if not isinstance(user_id, int):
            raise TypeError(f"Unsupported fake Discord user: {user!r}")
        self.added_user_ids.append(user_id)


@dataclass
class FakeDiscordUser:
    id: int
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
class FakeMatchmakingPresenceInteractionHandler:
    async def present_from_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        del interaction

    async def leave_from_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[discord.Client],
    ) -> None:
        del interaction


@dataclass
class FakeMatchmakingNewsMatchAnnouncementInteractionHandler:
    async def spectate_from_matchmaking_news_match_announcement(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id


@dataclass
class FakeMatchOperationThreadInteractionHandler:
    async def win_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id

    async def draw_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id

    async def lose_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id

    async def parent_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id

    async def void_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id

    async def approve_from_match_operation_thread(
        self,
        interaction: discord.Interaction[discord.Client],
        match_id: int,
    ) -> None:
        del interaction, match_id


@dataclass
class FakeDiscordClient:
    channels: dict[int, FakeDiscordChannel] = field(default_factory=dict)
    uncached_channel_ids: set[int] = field(default_factory=set)
    fetched_channel_ids: list[int] = field(default_factory=list)
    fetch_errors: dict[int, Exception] = field(default_factory=dict)
    users: dict[int, FakeDiscordUser] = field(default_factory=dict)
    uncached_user_ids: set[int] = field(default_factory=set)
    fetched_user_ids: list[int] = field(default_factory=list)
    user_fetch_errors: dict[int, Exception] = field(default_factory=dict)
    _connection: FakeDiscordConnectionState = field(default_factory=FakeDiscordConnectionState)

    def get_channel(self, channel_id: int) -> object | None:
        if channel_id in self.uncached_channel_ids:
            return None
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> object:
        self.fetched_channel_ids.append(channel_id)
        fetch_error = self.fetch_errors.get(channel_id)
        if fetch_error is not None:
            raise fetch_error
        channel = self.channels.get(channel_id)
        if channel is None:
            raise LookupError(f"Unknown channel: {channel_id}")
        return channel

    def get_user(self, user_id: int) -> object | None:
        if user_id in self.uncached_user_ids:
            return None
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int) -> object:
        self.fetched_user_ids.append(user_id)
        fetch_error = self.user_fetch_errors.get(user_id)
        if fetch_error is not None:
            raise fetch_error
        user = self.users.get(user_id)
        if user is None:
            raise LookupError(f"Unknown user: {user_id}")
        return user


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
    queue_class_id: str = DEFAULT_QUEUE_CLASS_ID,
    revision: int = 1,
    last_reminded_revision: int | None = None,
) -> MatchQueueEntry:
    current_time = datetime.now(timezone.utc)
    queue_entry = MatchQueueEntry(
        player_id=player_id,
        match_format=DEFAULT_MATCH_FORMAT,
        queue_class_id=queue_class_id,
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


def test_match_runtime_join_queue_calls_service_and_schedules_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    notification_context = build_notification_context(60_001)
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    join_result = JoinQueueResult(
        queue_entry_id=101,
        revision=3,
        expire_at=expire_at,
        queue_class_id=DEFAULT_QUEUE_CLASS_ID,
    )
    service.join_queue.return_value = join_result
    runtime = MatchRuntime(service=service)
    handler_calls: list[dict[str, object]] = []
    scheduled: list[dict[str, object]] = []
    try_create_matches_calls: list[tuple[str, str | None]] = []
    execution_order: list[tuple[str, object]] = []

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

    async def fake_try_create_matches_safely(
        *,
        context: str,
        queue_class_id: str | None = None,
    ) -> tuple[CreatedMatchResult, ...]:
        execution_order.append(("try_create_matches", queue_class_id))
        try_create_matches_calls.append((context, queue_class_id))
        return tuple()

    monkeypatch.setattr(runtime, "_try_create_matches_safely", fake_try_create_matches_safely)

    async def after_join(result: JoinQueueResult) -> None:
        execution_order.append(("after_join", result.queue_entry_id))

    result = asyncio.run(
        runtime.join_queue(
            5001,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=notification_context,
            after_join=after_join,
        )
    )

    assert result == join_result
    service.join_queue.assert_called_once_with(
        5001,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
        notification_context=notification_context,
    )
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
    assert try_create_matches_calls == [("join", DEFAULT_QUEUE_CLASS_ID)]
    assert execution_order == [
        ("after_join", join_result.queue_entry_id),
        ("try_create_matches", DEFAULT_QUEUE_CLASS_ID),
    ]


def test_match_runtime_present_calls_service_and_replaces_timers(
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
    runtime = MatchRuntime(service=service)
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


def test_match_runtime_present_updates_database_clock_offset_from_expire_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    expected_database_now = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)
    sampled_at = expected_database_now + timedelta(milliseconds=120)
    present_result = PresentQueueResult(
        queue_entry_id=211,
        revision=4,
        expire_at=expected_database_now + MATCH_QUEUE_TTL,
        expired=False,
        message="updated",
    )
    service.present.return_value = present_result
    runtime = MatchRuntime(service=service)

    monkeypatch.setattr(runtime, "_app_now_for_reference", lambda reference: sampled_at)
    monkeypatch.setattr(runtime, "_cancel_scheduled_task", lambda key: None)
    monkeypatch.setattr(runtime, "_schedule_task", lambda **kwargs: True)

    asyncio.run(runtime.present(5005))

    assert runtime._database_clock_offset == expected_database_now - sampled_at


def test_match_runtime_present_cancels_timers_when_entry_already_expired(
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
    runtime = MatchRuntime(service=service)
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


def test_match_runtime_leave_calls_service_and_cancels_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    leave_result = LeaveQueueResult(
        queue_entry_id=301,
        expired=False,
        message="left",
    )
    service.leave.return_value = leave_result
    runtime = MatchRuntime(service=service)
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


def test_match_runtime_process_expire_calls_service_and_cancels_timers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    expire_result = ExpireQueueEntryResult(queue_entry_id=401, expired=True)
    service.process_expire.return_value = expire_result
    runtime = MatchRuntime(service=service)
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


def test_match_runtime_volunteer_parent_cancels_match_tasks_when_assignment_immediately_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    match_service = Mock()
    current_time = datetime.now(timezone.utc)
    assignment_result = MatchParentAssignmentResult(
        match_id=701,
        parent_player_id=901,
        parent_decided_at=current_time,
        report_open_at=current_time + timedelta(minutes=7),
        report_deadline_at=current_time + timedelta(minutes=27),
        assigned=True,
        finalized=True,
        approval_deadline_at=None,
    )
    match_service.volunteer_parent.return_value = assignment_result
    runtime = MatchRuntime(service=service, match_service=match_service)
    cancel_all_match_tasks = Mock()
    schedule_match_reporting_tasks = Mock()
    schedule_match_approval_task = Mock()
    monkeypatch.setattr(runtime, "_cancel_all_match_tasks", cancel_all_match_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_reporting_tasks", schedule_match_reporting_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_approval_task", schedule_match_approval_task)

    result = asyncio.run(runtime.volunteer_parent(701, 901))

    assert result == assignment_result
    match_service.volunteer_parent.assert_called_once_with(
        701,
        901,
        notification_context=None,
    )
    cancel_all_match_tasks.assert_called_once_with(701)
    schedule_match_reporting_tasks.assert_not_called()
    schedule_match_approval_task.assert_not_called()


def test_match_runtime_process_parent_deadline_cancels_match_tasks_when_assignment_immediately_finalizes(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    match_service = Mock()
    current_time = datetime.now(timezone.utc)
    assignment_result = MatchParentAssignmentResult(
        match_id=702,
        parent_player_id=902,
        parent_decided_at=current_time,
        report_open_at=current_time + timedelta(minutes=7),
        report_deadline_at=current_time + timedelta(minutes=27),
        assigned=True,
        finalized=True,
        approval_deadline_at=None,
    )
    match_service.process_parent_deadline.return_value = assignment_result
    runtime = MatchRuntime(service=service, match_service=match_service)
    cancel_all_match_tasks = Mock()
    schedule_match_reporting_tasks = Mock()
    schedule_match_approval_task = Mock()
    monkeypatch.setattr(runtime, "_cancel_all_match_tasks", cancel_all_match_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_reporting_tasks", schedule_match_reporting_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_approval_task", schedule_match_approval_task)

    result = asyncio.run(runtime.process_parent_deadline(702))

    assert result == assignment_result
    match_service.process_parent_deadline.assert_called_once_with(702)
    cancel_all_match_tasks.assert_called_once_with(702)
    schedule_match_reporting_tasks.assert_not_called()
    schedule_match_approval_task.assert_not_called()


def test_match_runtime_submit_match_report_cancels_match_tasks_when_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    match_service = Mock()
    report_result = MatchReportSubmissionResult(
        match_id=701,
        report_id=801,
        finalized=True,
        approval_started=False,
        approval_deadline_at=None,
    )
    match_service.submit_report.return_value = report_result
    runtime = MatchRuntime(service=service, match_service=match_service)
    cancel_all_match_tasks = Mock()
    schedule_match_approval_task = Mock()
    monkeypatch.setattr(runtime, "_cancel_all_match_tasks", cancel_all_match_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_approval_task", schedule_match_approval_task)

    result = asyncio.run(runtime.submit_match_report(701, 901, MatchReportInputResult.WIN))

    assert result == report_result
    match_service.submit_report.assert_called_once_with(
        701,
        901,
        MatchReportInputResult.WIN,
        notification_context=None,
    )
    cancel_all_match_tasks.assert_called_once_with(701)
    schedule_match_approval_task.assert_not_called()


def test_match_runtime_approve_match_result_cancels_match_tasks_when_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    match_service = Mock()
    approval_result = MatchApprovalResult(
        match_id=702,
        approval_status=MatchApprovalStatus.APPROVED,
        finalized=True,
        finalized_at=datetime.now(timezone.utc),
        final_result=MatchResult.TEAM_A_WIN,
    )
    match_service.approve_provisional_result.return_value = approval_result
    runtime = MatchRuntime(service=service, match_service=match_service)
    cancel_all_match_tasks = Mock()
    monkeypatch.setattr(runtime, "_cancel_all_match_tasks", cancel_all_match_tasks)

    result = asyncio.run(runtime.approve_match_result(702, 902))

    assert result == approval_result
    match_service.approve_provisional_result.assert_called_once_with(
        702,
        902,
        notification_context=None,
    )
    cancel_all_match_tasks.assert_called_once_with(702)


def test_match_runtime_process_report_deadline_cancels_match_tasks_when_finalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    match_service = Mock()
    report_deadline_result = MatchFinalizationResult(
        match_id=702,
        final_result=MatchResult.TEAM_A_WIN,
        finalized=True,
        finalized_at=datetime.now(timezone.utc),
        approval_deadline_at=None,
        admin_review_required=False,
    )
    match_service.process_report_deadline.return_value = report_deadline_result
    runtime = MatchRuntime(service=service, match_service=match_service)
    cancel_all_match_tasks = Mock()
    schedule_match_approval_task = Mock()
    monkeypatch.setattr(runtime, "_cancel_all_match_tasks", cancel_all_match_tasks)
    monkeypatch.setattr(runtime, "_schedule_match_approval_task", schedule_match_approval_task)

    result = asyncio.run(runtime.process_report_deadline(702))

    assert result == report_deadline_result
    match_service.process_report_deadline.assert_called_once_with(702)
    cancel_all_match_tasks.assert_called_once_with(702)
    schedule_match_approval_task.assert_not_called()


def test_match_runtime_run_startup_sync_calls_service_and_reschedules(
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
        match_format=DEFAULT_MATCH_FORMAT,
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
    runtime = MatchRuntime(service=service)
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
    assert result == MatchRuntimeSyncResult(
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


def test_match_runtime_run_startup_sync_updates_database_clock_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Mock()
    snapshot_time = datetime(2026, 3, 24, 12, 10, tzinfo=timezone.utc)
    sampled_at = snapshot_time + timedelta(milliseconds=90)
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.return_value = (snapshot_time, tuple())
    runtime = MatchRuntime(service=service)

    monkeypatch.setattr(runtime, "_app_now_for_reference", lambda reference: sampled_at)
    result = asyncio.run(runtime.run_startup_sync())

    assert result == MatchRuntimeSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
    )
    assert runtime._database_clock_offset == snapshot_time - sampled_at


def test_match_runtime_run_reconcile_cycle_passes_warn_on_cleanup() -> None:
    service = Mock()
    snapshot_time = datetime.now(timezone.utc)
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.return_value = (snapshot_time, tuple())
    runtime = MatchRuntime(service=service)

    result = asyncio.run(runtime.run_reconcile_cycle())

    service.cleanup_expired_entries.assert_called_once_with(warn_on_cleanup=True)
    service.try_create_matches.assert_called_once_with()
    service.load_waiting_entry_timer_states.assert_called_once_with()
    assert result == MatchRuntimeSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
    )


def test_match_runtime_run_reconcile_cycle_records_finalized_match_on_report_deadline() -> None:
    service = Mock()
    match_service = Mock()
    snapshot_time = datetime.now(timezone.utc)
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.return_value = (snapshot_time, tuple())
    match_service.load_active_match_timer_states.return_value = (
        snapshot_time,
        (
            ActiveMatchTimerState(
                match_id=801,
                state=MatchState.WAITING_FOR_RESULT_REPORTS,
                parent_deadline_at=snapshot_time - timedelta(minutes=10),
                report_open_at=snapshot_time - timedelta(minutes=5),
                reporting_opened_at=snapshot_time - timedelta(minutes=5),
                report_deadline_at=snapshot_time - timedelta(seconds=1),
                approval_deadline_at=None,
            ),
        ),
    )
    match_service.process_report_deadline.return_value = MatchFinalizationResult(
        match_id=801,
        final_result=MatchResult.TEAM_A_WIN,
        finalized=True,
        finalized_at=snapshot_time,
        approval_deadline_at=None,
        admin_review_required=False,
    )
    runtime = MatchRuntime(service=service, match_service=match_service)

    result = asyncio.run(runtime.run_reconcile_cycle())

    assert result == MatchRuntimeSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
        auto_assigned_parent_match_ids=tuple(),
        opened_report_match_ids=tuple(),
        started_approval_match_ids=tuple(),
        finalized_match_ids=(801,),
        rescheduled_parent_deadline_match_ids=tuple(),
        rescheduled_report_open_match_ids=tuple(),
        rescheduled_report_deadline_match_ids=tuple(),
        rescheduled_approval_deadline_match_ids=tuple(),
    )
    match_service.process_report_deadline.assert_called_once_with(801)


def test_match_runtime_runs_due_scheduled_tasks() -> None:
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
    runtime = MatchRuntime(service=service)

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


def test_match_runtime_cancels_pending_tasks() -> None:
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
    runtime = MatchRuntime(service=service)

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


def test_match_runtime_retries_retryable_failures(
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
    runtime = MatchRuntime(service=service)
    monkeypatch.setattr(
        match_runtime_module,
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


def test_match_runtime_stops_presence_retry_after_deadline(
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
    runtime = MatchRuntime(service=service)
    monkeypatch.setattr(
        match_runtime_module,
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


def test_match_runtime_replaces_pending_retry_when_rescheduled(
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
    runtime = MatchRuntime(service=service)
    monkeypatch.setattr(
        match_runtime_module,
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


def test_match_runtime_seconds_until_uses_database_clock_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MatchRuntime(service=Mock())
    app_now = datetime(2026, 3, 24, 12, 20, tzinfo=timezone.utc)
    scheduled_at = datetime(2026, 3, 24, 12, 20, 10, tzinfo=timezone.utc)

    monkeypatch.setattr(runtime, "_app_now_for_reference", lambda reference: app_now)
    runtime._database_clock_offset = timedelta(seconds=-30)

    assert runtime._seconds_until(scheduled_at) == 40.0
    assert runtime._current_time_for(scheduled_at) == app_now - timedelta(seconds=30)


def test_match_runtime_runs_startup_sync_and_reconcile_loop() -> None:
    service = Mock()
    service.cleanup_expired_entries.return_value = tuple()
    service.try_create_matches.return_value = tuple()
    service.load_waiting_entry_timer_states.side_effect = lambda: (
        datetime.now(timezone.utc),
        tuple(),
    )
    runtime = MatchRuntime(
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


def test_bot_runtime_starts_and_stops_match_runtime_and_outbox() -> None:
    startup_result = MatchRuntimeSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=(1,),
    )
    match_runtime = Mock()
    match_runtime.start = AsyncMock(return_value=startup_result)
    match_runtime.stop = AsyncMock(return_value=None)
    outbox_dispatcher = Mock()
    outbox_dispatcher.start = AsyncMock(return_value=OutboxStartupResult(published_event_ids=(99,)))
    outbox_dispatcher.stop = AsyncMock(return_value=None)
    runtime = BotRuntime(
        match_runtime=match_runtime,
        outbox_dispatcher=outbox_dispatcher,
    )

    async def scenario() -> tuple[BotRuntimeStartResult, asyncio.AbstractEventLoop]:
        result = await runtime.start()
        loop = asyncio.get_running_loop()
        await runtime.stop()
        return result, loop

    result, loop = asyncio.run(scenario())

    assert result == BotRuntimeStartResult(
        match_runtime=startup_result,
        outbox=OutboxStartupResult(published_event_ids=(99,)),
    )
    match_runtime.start.assert_awaited_once_with()
    match_runtime.stop.assert_awaited_once_with()
    outbox_dispatcher.bind_loop.assert_called_once()
    assert outbox_dispatcher.bind_loop.call_args.args[0] is loop
    outbox_dispatcher.start.assert_awaited_once_with()
    outbox_dispatcher.stop.assert_awaited_once_with()


def test_bot_runtime_rolls_back_match_runtime_when_outbox_start_fails() -> None:
    startup_result = MatchRuntimeSyncResult(
        cleaned_up_queue_entry_ids=tuple(),
        reminded_queue_entry_ids=tuple(),
        rescheduled_reminder_queue_entry_ids=tuple(),
        rescheduled_expire_queue_entry_ids=tuple(),
        created_match_ids=tuple(),
    )
    match_runtime = Mock()
    match_runtime.start = AsyncMock(return_value=startup_result)
    match_runtime.stop = AsyncMock(return_value=None)
    outbox_dispatcher = Mock()
    outbox_dispatcher.start = AsyncMock(side_effect=RuntimeError("outbox start failed"))
    outbox_dispatcher.stop = AsyncMock(return_value=None)
    runtime = BotRuntime(
        match_runtime=match_runtime,
        outbox_dispatcher=outbox_dispatcher,
    )

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="outbox start failed"):
            await runtime.start()

    asyncio.run(scenario())

    match_runtime.start.assert_awaited_once_with()
    match_runtime.stop.assert_awaited_once_with()
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
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
        notification_context=build_notification_context(70_001),
    )

    async def scenario() -> BotRuntimeStartResult:
        runtime = BotRuntime(
            match_runtime=MatchRuntime.create(
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
    assert startup_result.match_runtime.rescheduled_reminder_queue_entry_ids == (
        join_result.queue_entry_id,
    )
    assert startup_result.match_runtime.rescheduled_expire_queue_entry_ids == (
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
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=build_notification_context(
                70_100 + index,
                channel_id=900_100,
                guild_id=910_100,
            ),
        )

    publisher = RecordingOutboxPublisher()

    async def scenario() -> BotRuntimeStartResult:
        runtime = BotRuntime(
            match_runtime=MatchRuntime.create(
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

    assert len(startup_result.match_runtime.created_match_ids) == 1
    assert len(matches) == 1
    assert all(entry.status == MatchQueueEntryStatus.MATCHED for entry in queue_entries)
    assert publisher.events == []


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
        match_format=DEFAULT_MATCH_FORMAT,
        queue_class_id=DEFAULT_QUEUE_CLASS_ID,
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

    with caplog.at_level(logging.WARNING, logger="dxd_rating.platform.runtime.outbox"):
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


def test_outbox_dispatcher_discards_non_retryable_failures(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    session.add(
        OutboxEvent(
            event_type=OutboxEventType.MATCH_CREATED,
            dedupe_key="match-created:discard",
            payload={"match_id": 99},
        )
    )
    session.commit()

    publisher = DiscardingOutboxPublisher()
    dispatcher = OutboxDispatcher(session_factory=session_factory, publisher=publisher)

    published_event_ids = asyncio.run(dispatcher.dispatch_once())

    session.expire_all()
    outbox_event = session.scalar(select(OutboxEvent))

    assert published_event_ids == tuple()
    assert outbox_event is not None
    assert publisher.attempts == 1
    assert outbox_event.failure_count == 0
    assert outbox_event.published_at is None
    assert outbox_event.discarded_at is not None
    assert outbox_event.last_error == "NonRetryableOutboxPublishError: Discord channel not found: 1"
    assert outbox_event.last_failed_at is not None


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


def test_discord_outbox_publisher_sends_presence_reminder_to_channel_destination() -> None:
    discord_user_id = 930_001
    channel_id = 900_001
    guild_id = 910_001
    parent_channel = FakeDiscordChannel(id=899_999, guild=FakeDiscordGuild(id=guild_id))
    channel = FakeDiscordChannel(
        id=channel_id,
        guild=FakeDiscordGuild(id=guild_id),
        parent=parent_channel,
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(
        client=client,
        matchmaking_presence_interaction_handler=FakeMatchmakingPresenceInteractionHandler(),
    )

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
                        "kind": "channel",
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert channel.sent_messages == [
        f"<@{discord_user_id}> {PRESENCE_REMINDER_NOTIFICATION_MESSAGE}"
    ]
    allowed_mentions = channel.allowed_mentions_history[0]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.users is True
    view = channel.sent_views[0]
    assert view is not None
    button_labels = [child.label for child in view.children if isinstance(child, discord.ui.Button)]
    assert button_labels == [
        MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL,
        MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL,
    ]


def test_discord_outbox_publisher_fetches_uncached_channel_for_queue_expired() -> None:
    discord_user_id = 930_011
    channel_id = 900_011
    guild_id = 910_011
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
                id=11,
                event_type=OutboxEventType.QUEUE_EXPIRED,
                dedupe_key="queue_expired:11:1",
                payload={
                    "queue_entry_id": 111,
                    "player_id": 80_011,
                    "revision": 1,
                    "expire_at": datetime.now(timezone.utc).isoformat(),
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert client.fetched_channel_ids == [channel.id]
    assert channel.sent_messages == [f"<@{discord_user_id}> {QUEUE_EXPIRED_NOTIFICATION_MESSAGE}"]
    allowed_mentions = channel.allowed_mentions_history[0]
    assert isinstance(allowed_mentions, discord.AllowedMentions)
    assert allowed_mentions.users is True
    assert channel.sent_views == [None]


def test_discord_outbox_publisher_skips_presence_controls_for_non_thread_channel() -> None:
    discord_user_id = 930_021
    channel_id = 900_021
    guild_id = 910_021
    channel = FakeDiscordChannel(id=channel_id, guild=FakeDiscordGuild(id=guild_id))
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(
        client=client,
        matchmaking_presence_interaction_handler=FakeMatchmakingPresenceInteractionHandler(),
    )

    asyncio.run(
        publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=21,
                event_type=OutboxEventType.PRESENCE_REMINDER,
                dedupe_key="presence_reminder:21:1",
                payload={
                    "queue_entry_id": 121,
                    "player_id": 80_021,
                    "revision": 1,
                    "expire_at": datetime.now(timezone.utc).isoformat(),
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert channel.sent_views == [None]


def test_discord_outbox_publisher_raises_non_retryable_error_when_channel_is_missing() -> None:
    channel_id = 900_099
    client = FakeDiscordClient(
        uncached_channel_ids={channel_id},
        fetch_errors={channel_id: make_not_found()},
    )
    publisher = DiscordOutboxEventPublisher(client=client)

    with pytest.raises(
        NonRetryableOutboxPublishError,
        match=f"Discord channel not found: {channel_id}",
    ):
        asyncio.run(
            publish_with_bound_loop(
                publisher,
                PendingOutboxEvent(
                    id=99,
                    event_type=OutboxEventType.QUEUE_EXPIRED,
                    dedupe_key="queue_expired:missing-channel",
                    payload={
                        "queue_entry_id": 199,
                        "player_id": 80_099,
                        "revision": 1,
                        "expire_at": datetime.now(timezone.utc).isoformat(),
                        "destination": {
                            "channel_id": channel_id,
                            "guild_id": 910_099,
                        },
                        "mention_discord_user_id": 920_099,
                    },
                    created_at=datetime.now(timezone.utc),
                ),
            )
        )


def test_discord_outbox_publisher_prefixes_channel_reminder_with_target_user_mention() -> None:
    channel_id = 900_020
    guild_id = 910_020
    discord_user_id = 930_020
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
                        "kind": "channel",
                        "channel_id": channel_id,
                        "guild_id": guild_id,
                    },
                    "mention_discord_user_id": discord_user_id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
    )

    assert channel.sent_messages == [
        f"<@{discord_user_id}> {PRESENCE_REMINDER_NOTIFICATION_MESSAGE}"
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


def test_discord_outbox_publisher_renders_matchmaking_news_match_announcement() -> None:
    channel = FakeDiscordChannel(
        id=900_012,
        guild=FakeDiscordGuild(id=910_012),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            MATCH_CREATED_NOTIFICATION_MESSAGE,
            "試合形式: 3v3",
            "試合階級: beginner",
            "Team A",
            "    Player A",
            "    Player B",
            "    Player C",
            "Team B",
            "    Player D",
            "    Player E",
            "    Player F",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=5,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:1:900012",
                payload={
                    "match_id": 1,
                    "match_format": "3v3",
                    "queue_name": "beginner",
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                    "team_a_player_display_names": [
                        "Player A",
                        "Player B",
                        "Player C",
                    ],
                    "team_b_player_display_names": [
                        "Player D",
                        "Player E",
                        "Player F",
                    ],
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]
    assert channel.sent_views == [None]


def test_discord_outbox_publisher_adds_matchmaking_news_spectate_button_for_match_created() -> None:
    guild = FakeDiscordGuild(id=910_012_1)
    channel = FakeDiscordChannel(
        id=900_012_1,
        guild=guild,
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(
        client=client,
        matchmaking_news_match_announcement_interaction_handler=(
            FakeMatchmakingNewsMatchAnnouncementInteractionHandler()
        ),
    )

    expected_message = "\n".join(
        [
            MATCH_CREATED_NOTIFICATION_MESSAGE,
            "試合形式: 3v3",
            "試合階級: beginner",
            "Team A",
            "    Player A",
            "    Player B",
            "    Player C",
            "Team B",
            "    Player D",
            "    Player E",
            "    Player F",
            "観戦希望者は下の「観戦する」ボタンから応募してください。",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=5_1,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:12:9000121",
                payload={
                    "match_id": 12,
                    "match_format": "3v3",
                    "queue_name": "beginner",
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                    "team_a_player_display_names": [
                        "Player A",
                        "Player B",
                        "Player C",
                    ],
                    "team_b_player_display_names": [
                        "Player D",
                        "Player E",
                        "Player F",
                    ],
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]
    assert len(channel.sent_views) == 1
    view = channel.sent_views[0]
    assert view is not None
    labels = [getattr(child, "label", None) for child in view.children]
    assert labels == [MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_LABEL]
    custom_ids = [getattr(child, "custom_id", None) for child in view.children]
    assert custom_ids == [
        f"{MATCHMAKING_NEWS_MATCH_ANNOUNCEMENT_SPECTATE_BUTTON_CUSTOM_ID_PREFIX}:12"
    ]


def test_discord_outbox_publisher_creates_match_operation_thread_for_match_created() -> None:
    guild = FakeDiscordGuild(id=910_013)
    announcement_channel = FakeDiscordChannel(
        id=900_013,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_113,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            announcement_channel.id: announcement_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            81_100: FakeDiscordUser(id=81_100),
            81_101: FakeDiscordUser(id=81_101),
            81_102: FakeDiscordUser(id=81_102),
            81_103: FakeDiscordUser(id=81_103),
            81_104: FakeDiscordUser(id=81_104),
            81_105: FakeDiscordUser(id=81_105),
            89_001: FakeDiscordUser(id=89_001),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        admin_discord_user_ids=frozenset({89_001}),
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    expected_announcement_message = "\n".join(
        [
            MATCH_CREATED_NOTIFICATION_MESSAGE,
            "試合形式: 3v3",
            "試合階級: regular",
            "Team A",
            "    Player A",
            "    Player B",
            "    Player C",
            "Team B",
            "    Player D",
            "    Player E",
            "    Player F",
        ]
    )
    expected_thread_messages = [
        "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                "試合形式: 3v3",
                "試合階級: regular",
                "Team A",
                "    <@81100>",
                "    <@81101>",
                "    <@81102>",
                "Team B",
                "    <@81103>",
                "    <@81104>",
                "    <@81105>",
                MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE,
            ]
        ),
        "\n".join(
            [
                "まず初めに、部屋立てと試合の進行を行う親を募集します。",
                "親募集期間は5分です。",
                "5分以内に立候補がない場合は Bot が参加メンバーからランダムに決定します。",
            ]
        ),
        "\n".join(
            [
                "試合参加者はゲーム内のプレイヤー名を報告してください。",
            ]
        ),
    ]

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=6,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:2:900013",
                payload={
                    "match_id": 2,
                    "match_format": "3v3",
                    "queue_name": "regular",
                    "destination": {
                        "channel_id": announcement_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [81_100, 81_101, 81_102],
                    "team_b_discord_user_ids": [81_103, 81_104, 81_105],
                    "team_a_player_display_names": ["Player A", "Player B", "Player C"],
                    "team_b_player_display_names": ["Player D", "Player E", "Player F"],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                    "create_match_operation_thread": True,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert announcement_channel.sent_messages == [expected_announcement_message]
    assert len(matchmaking_channel.created_threads) == 1

    created_thread = matchmaking_channel.created_threads[0]
    assert created_thread.name == "試合-2"
    assert created_thread.added_user_ids == [
        81_100,
        81_101,
        81_102,
        81_103,
        81_104,
        81_105,
        89_001,
    ]
    assert created_thread.sent_messages == expected_thread_messages
    assert len(created_thread.sent_views) == 3
    initial_view = created_thread.sent_views[0]
    assert initial_view is not None
    assert [getattr(child, "label", None) for child in initial_view.children] == [
        MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL
    ]
    assert [getattr(child, "custom_id", None) for child in initial_view.children] == [
        f"{MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX}:2"
    ]
    parent_recruitment_view = created_thread.sent_views[1]
    assert parent_recruitment_view is not None
    assert [getattr(child, "label", None) for child in parent_recruitment_view.children] == [
        MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL
    ]
    assert [getattr(child, "custom_id", None) for child in parent_recruitment_view.children] == [
        f"{MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX}:2"
    ]
    assert created_thread.sent_views[2] is None


def test_discord_outbox_publisher_links_presence_thread_to_match_operation_thread() -> None:
    guild = FakeDiscordGuild(id=910_013_1)
    presence_thread = FakeDiscordChannel(
        id=900_213_001,
        guild=guild,
        parent=object(),
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_113_1,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            presence_thread.id: presence_thread,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            81_110: FakeDiscordUser(id=81_110),
            81_111: FakeDiscordUser(id=81_111),
            81_112: FakeDiscordUser(id=81_112),
            81_113: FakeDiscordUser(id=81_113),
            81_114: FakeDiscordUser(id=81_114),
            81_115: FakeDiscordUser(id=81_115),
            89_011: FakeDiscordUser(id=89_011),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        admin_discord_user_ids=frozenset({89_011}),
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    expected_presence_message = "\n".join(
        [
            "<@81110> マッチ成立です。",
            "試合運営は <#9001131001> で行ってください。",
        ]
    )
    expected_thread_messages = [
        "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                "試合形式: 3v3",
                "試合階級: regular",
                "Team A",
                "    <@81110>",
                "    <@81111>",
                "    <@81112>",
                "Team B",
                "    <@81113>",
                "    <@81114>",
                "    <@81115>",
                MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE,
            ]
        ),
        "\n".join(
            [
                "まず初めに、部屋立てと試合の進行を行う親を募集します。",
                "親募集期間は5分です。",
                "5分以内に立候補がない場合は Bot が参加メンバーからランダムに決定します。",
            ]
        ),
        "\n".join(
            [
                "試合参加者はゲーム内のプレイヤー名を報告してください。",
            ]
        ),
    ]

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=6_1,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:12:900213001",
                payload={
                    "match_id": 12,
                    "match_format": "3v3",
                    "queue_name": "regular",
                    "destination": {
                        "channel_id": presence_thread.id,
                        "guild_id": guild.id,
                    },
                    "mention_discord_user_id": 81_110,
                    "team_a_discord_user_ids": [81_110, 81_111, 81_112],
                    "team_b_discord_user_ids": [81_113, 81_114, 81_115],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                    "create_match_operation_thread": True,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert presence_thread.sent_messages == [expected_presence_message]
    assert presence_thread.sent_views == [None]
    assert len(matchmaking_channel.created_threads) == 1
    created_thread = matchmaking_channel.created_threads[0]
    assert created_thread.name == "試合-12"
    assert created_thread.added_user_ids == [
        81_110,
        81_111,
        81_112,
        81_113,
        81_114,
        81_115,
        89_011,
    ]
    assert created_thread.sent_messages == expected_thread_messages


def test_discord_outbox_publisher_reuses_existing_match_operation_thread_for_same_match() -> None:
    guild = FakeDiscordGuild(id=910_014)
    announcement_channel = FakeDiscordChannel(
        id=900_014,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_114,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            announcement_channel.id: announcement_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            82_100: FakeDiscordUser(id=82_100),
            82_101: FakeDiscordUser(id=82_101),
            82_102: FakeDiscordUser(id=82_102),
            82_103: FakeDiscordUser(id=82_103),
            82_104: FakeDiscordUser(id=82_104),
            82_105: FakeDiscordUser(id=82_105),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )
    event = PendingOutboxEvent(
        id=7,
        event_type=OutboxEventType.MATCH_CREATED,
        dedupe_key="match_created:3:900014",
        payload={
            "match_id": 3,
            "match_format": "3v3",
            "queue_name": "master",
            "destination": {
                "channel_id": announcement_channel.id,
                "guild_id": guild.id,
            },
            "team_a_discord_user_ids": [82_100, 82_101, 82_102],
            "team_b_discord_user_ids": [82_103, 82_104, 82_105],
            "team_a_player_display_names": ["Player A", "Player B", "Player C"],
            "team_b_player_display_names": ["Player D", "Player E", "Player F"],
            "match_operation_thread_parent_channel_id": matchmaking_channel.id,
            "create_match_operation_thread": True,
        },
        created_at=datetime.now(timezone.utc),
    )

    async def scenario() -> None:
        await publish_with_bound_loop(publisher, event)
        await publish_with_bound_loop(publisher, event)

    asyncio.run(scenario())

    assert len(matchmaking_channel.created_threads) == 1
    assert matchmaking_channel.created_threads[0].sent_messages == [
        "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                "試合形式: 3v3",
                "試合階級: master",
                "Team A",
                "    <@82100>",
                "    <@82101>",
                "    <@82102>",
                "Team B",
                "    <@82103>",
                "    <@82104>",
                "    <@82105>",
                MATCH_OPERATION_THREAD_VOID_GUIDE_MESSAGE,
            ]
        ),
        "\n".join(
            [
                "まず初めに、部屋立てと試合の進行を行う親を募集します。",
                "親募集期間は5分です。",
                "5分以内に立候補がない場合は Bot が参加メンバーからランダムに決定します。",
            ]
        ),
        "\n".join(
            [
                "試合参加者はゲーム内のプレイヤー名を報告してください。",
            ]
        ),
    ]
    initial_view = matchmaking_channel.created_threads[0].sent_views[0]
    assert initial_view is not None
    assert [getattr(child, "label", None) for child in initial_view.children] == [
        MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL
    ]
    assert [getattr(child, "custom_id", None) for child in initial_view.children] == [
        f"{MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX}:3"
    ]
    parent_recruitment_view = matchmaking_channel.created_threads[0].sent_views[1]
    assert parent_recruitment_view is not None
    assert [getattr(child, "label", None) for child in parent_recruitment_view.children] == [
        MATCH_OPERATION_THREAD_PARENT_BUTTON_LABEL
    ]
    assert [getattr(child, "custom_id", None) for child in parent_recruitment_view.children] == [
        f"{MATCH_OPERATION_THREAD_PARENT_BUTTON_CUSTOM_ID_PREFIX}:3"
    ]
    assert len(announcement_channel.sent_messages) == 2


def test_discord_outbox_publisher_routes_approval_request_to_match_operation_thread() -> None:
    guild = FakeDiscordGuild(id=910_015)
    match_channel = FakeDiscordChannel(
        id=900_015,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_115,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            83_001: FakeDiscordUser(id=83_001),
            83_002: FakeDiscordUser(id=83_002),
            89_003: FakeDiscordUser(id=89_003),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        admin_discord_user_ids=frozenset({89_003}),
    )

    expected_message = "\n".join(
        [
            f"<@83001> <@83002> {MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE}",
            "仮決定結果: チーム B の勝ち",
            "承認締切: 2026-03-20T12:44:56+00:00",
            "承認できない場合は証拠を提示したうえで admin へ連絡してください。",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:21:thread",
                payload={
                    "match_id": 21,
                    "provisional_result": "team_b_win",
                    "approval_deadline_at": "2026-03-20T12:44:56+00:00",
                    "phase_started": False,
                    "approval_target_discord_user_ids": [83_001, 83_002],
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [83_001],
                    "team_b_discord_user_ids": [83_002],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert match_channel.sent_messages == []
    assert len(matchmaking_channel.created_threads) == 1
    created_thread = matchmaking_channel.created_threads[0]
    assert created_thread.added_user_ids == [83_001, 83_002, 89_003]
    assert created_thread.sent_messages == [expected_message]
    assert created_thread.sent_views == [None]


def test_discord_outbox_publisher_adds_approval_button_to_match_operation_thread_request() -> None:
    guild = FakeDiscordGuild(id=910_015_3)
    match_channel = FakeDiscordChannel(
        id=900_015_3,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_115_3,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            83_201: FakeDiscordUser(id=83_201),
            83_202: FakeDiscordUser(id=83_202),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9_01,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:41:thread",
                payload={
                    "match_id": 41,
                    "provisional_result": "team_b_win",
                    "approval_deadline_at": "2026-03-20T12:44:56+00:00",
                    "phase_started": False,
                    "approval_target_discord_user_ids": [83_201, 83_202],
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [83_201],
                    "team_b_discord_user_ids": [83_202],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert match_channel.sent_messages == []
    assert len(matchmaking_channel.created_threads) == 1
    created_thread = matchmaking_channel.created_threads[0]
    assert len(created_thread.sent_messages) == 1
    approval_view = created_thread.sent_views[0]
    assert approval_view is not None
    assert [getattr(child, "label", None) for child in approval_view.children] == [
        MATCH_OPERATION_THREAD_APPROVE_BUTTON_LABEL
    ]
    assert [getattr(child, "custom_id", None) for child in approval_view.children] == [
        f"{MATCH_OPERATION_THREAD_APPROVE_BUTTON_CUSTOM_ID_PREFIX}:41"
    ]
    assert [
        getattr(getattr(child, "item", None), "style", None) for child in approval_view.children
    ] == [discord.ButtonStyle.danger]


def test_discord_outbox_publisher_does_not_add_approval_button_for_phase_started_message() -> None:
    guild = FakeDiscordGuild(id=910_015_4)
    match_channel = FakeDiscordChannel(
        id=900_015_4,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_115_4,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            83_301: FakeDiscordUser(id=83_301),
            83_302: FakeDiscordUser(id=83_302),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9_02,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:phase_started:42:thread",
                payload={
                    "match_id": 42,
                    "provisional_result": "team_a_win",
                    "approval_deadline_at": "2026-03-20T12:34:56+00:00",
                    "phase_started": True,
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [83_301],
                    "team_b_discord_user_ids": [83_302],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert match_channel.sent_messages == []
    assert len(matchmaking_channel.created_threads) == 1
    created_thread = matchmaking_channel.created_threads[0]
    assert len(created_thread.sent_messages) == 1
    assert created_thread.sent_views == [None]


def test_discord_outbox_publisher_routes_report_opened_to_match_operation_thread() -> None:
    guild = FakeDiscordGuild(id=910_015_1)
    match_channel = FakeDiscordChannel(
        id=900_015_1,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_115_1,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            83_101: FakeDiscordUser(id=83_101),
            83_102: FakeDiscordUser(id=83_102),
            83_103: FakeDiscordUser(id=83_103),
            83_104: FakeDiscordUser(id=83_104),
            83_105: FakeDiscordUser(id=83_105),
            83_106: FakeDiscordUser(id=83_106),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    expected_message = "\n".join(
        [
            MATCH_REPORT_OPENED_NOTIFICATION_MESSAGE,
            "自分視点で「勝ち」「引き分け」「負け」を選んでください。",
            "無効試合にすべき場合は「無効試合申請」を押してください。",
            "勝敗報告締切: 2026-03-20T12:20:00+00:00",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9_1,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:21:thread-init",
                payload={
                    "match_id": 21,
                    "match_format": "3v3",
                    "queue_name": "regular",
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [83_101, 83_102, 83_103],
                    "team_b_discord_user_ids": [83_104, 83_105, 83_106],
                    "team_a_player_display_names": ["A", "B", "C"],
                    "team_b_player_display_names": ["D", "E", "F"],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                    "create_match_operation_thread": True,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9_2,
                event_type=OutboxEventType.MATCH_REPORT_OPENED,
                dedupe_key="match_report_opened:21:thread",
                payload={
                    "match_id": 21,
                    "report_deadline_at": "2026-03-20T12:20:00+00:00",
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [83_101, 83_102, 83_103],
                    "team_b_discord_user_ids": [83_104, 83_105, 83_106],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert match_channel.sent_messages == [
        "\n".join(
            [
                MATCH_CREATED_NOTIFICATION_MESSAGE,
                "試合形式: 3v3",
                "試合階級: regular",
                "Team A",
                "    A",
                "    B",
                "    C",
                "Team B",
                "    D",
                "    E",
                "    F",
            ]
        )
    ]
    assert len(matchmaking_channel.created_threads) == 1
    created_thread = matchmaking_channel.created_threads[0]
    assert created_thread.sent_messages[3] == expected_message
    report_view = created_thread.sent_views[3]
    assert report_view is not None
    assert [getattr(child, "label", None) for child in report_view.children] == [
        MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL,
    ]
    assert [getattr(child, "custom_id", None) for child in report_view.children] == [
        f"{MATCH_OPERATION_THREAD_WIN_BUTTON_CUSTOM_ID_PREFIX}:21",
        f"{MATCH_OPERATION_THREAD_DRAW_BUTTON_CUSTOM_ID_PREFIX}:21",
        f"{MATCH_OPERATION_THREAD_LOSE_BUTTON_CUSTOM_ID_PREFIX}:21",
        f"{MATCH_OPERATION_THREAD_VOID_BUTTON_CUSTOM_ID_PREFIX}:21",
    ]
    assert [
        getattr(getattr(child, "item", None), "style", None) for child in report_view.children
    ] == [
        discord.ButtonStyle.success,
        discord.ButtonStyle.primary,
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.danger,
    ]


def test_discord_outbox_publisher_reuses_existing_thread_for_report_opened_after_fetch() -> None:
    guild = FakeDiscordGuild(id=910_015_2)
    match_channel = FakeDiscordChannel(
        id=900_015_2,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_115_2,
        guild=guild,
    )
    existing_thread = FakeDiscordThread(
        id=900_115_2001,
        name="試合-31",
        guild=guild,
        parent=matchmaking_channel,
    )
    object.__setattr__(guild, "threads", [existing_thread])
    client = FakeDiscordClient(
        channels={
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        uncached_channel_ids={matchmaking_channel.id},
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        match_operation_thread_interaction_handler=FakeMatchOperationThreadInteractionHandler(),
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9_3,
                event_type=OutboxEventType.MATCH_REPORT_OPENED,
                dedupe_key="match_report_opened:31:thread",
                payload={
                    "match_id": 31,
                    "report_deadline_at": "2026-03-20T12:45:00+00:00",
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [93_101, 93_102, 93_103],
                    "team_b_discord_user_ids": [93_104, 93_105, 93_106],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert client.fetched_channel_ids == [matchmaking_channel.id]
    assert matchmaking_channel.created_threads == []
    assert existing_thread.sent_messages == [
        "\n".join(
            [
                MATCH_REPORT_OPENED_NOTIFICATION_MESSAGE,
                "自分視点で「勝ち」「引き分け」「負け」を選んでください。",
                "無効試合にすべき場合は「無効試合申請」を押してください。",
                "勝敗報告締切: 2026-03-20T12:45:00+00:00",
            ]
        )
    ]
    report_view = existing_thread.sent_views[0]
    assert report_view is not None
    assert [getattr(child, "label", None) for child in report_view.children] == [
        MATCH_OPERATION_THREAD_WIN_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_DRAW_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_LOSE_BUTTON_LABEL,
        MATCH_OPERATION_THREAD_VOID_BUTTON_LABEL,
    ]


def test_discord_outbox_publisher_routes_match_notifications_to_existing_thread() -> None:
    guild = FakeDiscordGuild(id=910_016)
    announcement_channel = FakeDiscordChannel(
        id=900_016,
        guild=guild,
    )
    match_channel = FakeDiscordChannel(
        id=900_017,
        guild=guild,
    )
    matchmaking_channel = FakeDiscordChannel(
        id=900_116,
        guild=guild,
    )
    client = FakeDiscordClient(
        channels={
            announcement_channel.id: announcement_channel,
            match_channel.id: match_channel,
            matchmaking_channel.id: matchmaking_channel,
        },
        users={
            84_100: FakeDiscordUser(id=84_100),
            84_101: FakeDiscordUser(id=84_101),
            84_102: FakeDiscordUser(id=84_102),
            84_103: FakeDiscordUser(id=84_103),
            84_104: FakeDiscordUser(id=84_104),
            84_105: FakeDiscordUser(id=84_105),
            89_004: FakeDiscordUser(id=89_004),
        },
    )
    publisher = DiscordOutboxEventPublisher(
        client=client,
        admin_discord_user_ids=frozenset({89_004}),
    )

    expected_announcement_message = "\n".join(
        [
            MATCH_CREATED_NOTIFICATION_MESSAGE,
            "試合形式: 3v3",
            "試合階級: expert",
            "Team A",
            "    Player A",
            "    Player B",
            "    Player C",
            "Team B",
            "    Player D",
            "    Player E",
            "    Player F",
        ]
    )
    expected_parent_message = "\n".join(
        [
            MATCH_PARENT_ASSIGNED_NOTIFICATION_MESSAGE,
            "親: <@84100>",
            "勝敗報告開始: 2026-03-20T12:00:00+00:00",
            "勝敗報告締切: 2026-03-20T12:20:00+00:00",
        ]
    )
    expected_phase_started_message = "\n".join(
        [
            MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE,
            "仮決定結果: チーム A の勝ち",
            "承認締切: 2026-03-20T12:34:56+00:00",
        ]
    )
    expected_finalized_message = "\n".join(
        [
            MATCH_FINALIZED_NOTIFICATION_MESSAGE,
            "結果: チーム A の勝ち",
        ]
    )
    expected_auto_penalty_message = "\n".join(
        [
            f"<@84105> {MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE}",
            "結果: チーム A の勝ち",
            "ペナルティ: 未報告",
            "現在の累積: 2",
        ]
    )
    expected_admin_review_message = "\n".join(
        [
            "<@89004>",
            MATCH_ADMIN_REVIEW_REQUIRED_NOTIFICATION_MESSAGE,
            "結果: チーム A の勝ち",
            "理由: 同票が解消できませんでした",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=10,
                event_type=OutboxEventType.MATCH_CREATED,
                dedupe_key="match_created:22:900016",
                payload={
                    "match_id": 22,
                    "match_format": "3v3",
                    "queue_name": "expert",
                    "destination": {
                        "channel_id": announcement_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "team_a_player_display_names": ["Player A", "Player B", "Player C"],
                    "team_b_player_display_names": ["Player D", "Player E", "Player F"],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                    "create_match_operation_thread": True,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=11,
                event_type=OutboxEventType.MATCH_PARENT_ASSIGNED,
                dedupe_key="match_parent_assigned:22:thread:manual",
                payload={
                    "match_id": 22,
                    "parent_discord_user_id": 84_100,
                    "report_open_at": "2026-03-20T12:00:00+00:00",
                    "report_deadline_at": "2026-03-20T12:20:00+00:00",
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=12,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:phase_started:22:thread",
                payload={
                    "match_id": 22,
                    "provisional_result": "team_a_win",
                    "approval_deadline_at": "2026-03-20T12:34:56+00:00",
                    "phase_started": True,
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=13,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key="match_finalized:22:thread:automatic",
                payload={
                    "match_id": 22,
                    "final_result": "team_a_win",
                    "finalized_at": "2026-03-20T12:40:00+00:00",
                    "finalized_by_admin": False,
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=14,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key="match_finalized:auto_penalty:22:105:automatic",
                payload={
                    "match_id": 22,
                    "final_result": "team_a_win",
                    "finalized_at": "2026-03-20T12:40:00+00:00",
                    "finalized_by_admin": False,
                    "auto_penalty_applied": True,
                    "mention_discord_user_id": 84_105,
                    "penalty_type": "no_report",
                    "penalty_count": 2,
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=15,
                event_type=OutboxEventType.MATCH_ADMIN_REVIEW_REQUIRED,
                dedupe_key="match_admin_review_required:22:thread",
                payload={
                    "match_id": 22,
                    "final_result": "team_a_win",
                    "admin_review_reasons": ["unresolved_tie"],
                    "admin_discord_user_ids": [89_004],
                    "destination": {
                        "channel_id": match_channel.id,
                        "guild_id": guild.id,
                    },
                    "team_a_discord_user_ids": [84_100, 84_101, 84_102],
                    "team_b_discord_user_ids": [84_103, 84_104, 84_105],
                    "match_operation_thread_parent_channel_id": matchmaking_channel.id,
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert announcement_channel.sent_messages == [expected_announcement_message]
    assert match_channel.sent_messages == []
    assert len(matchmaking_channel.created_threads) == 1
    assert matchmaking_channel.created_threads[0].sent_messages[3:] == [
        expected_parent_message,
        expected_phase_started_message,
        expected_finalized_message,
        expected_auto_penalty_message,
        expected_admin_review_message,
    ]


def test_discord_outbox_publisher_renders_match_approval_phase_started_message() -> None:
    channel = FakeDiscordChannel(
        id=900_020,
        guild=FakeDiscordGuild(id=910_020),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            MATCH_APPROVAL_STARTED_NOTIFICATION_MESSAGE,
            "仮決定結果: チーム A の勝ち",
            "承認締切: 2026-03-20T12:34:56+00:00",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=5,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:phase_started:11:900020",
                payload={
                    "match_id": 11,
                    "provisional_result": "team_a_win",
                    "approval_deadline_at": "2026-03-20T12:34:56+00:00",
                    "phase_started": True,
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_match_approval_request_message() -> None:
    channel = FakeDiscordChannel(
        id=900_021,
        guild=FakeDiscordGuild(id=910_021),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            f"<@80021> {MATCH_APPROVAL_REQUESTED_NOTIFICATION_MESSAGE}",
            "仮決定結果: チーム B の勝ち",
            "承認締切: 2026-03-20T12:44:56+00:00",
            "承認できない場合は証拠を提示したうえで admin へ連絡してください。",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=6,
                event_type=OutboxEventType.MATCH_APPROVAL_REQUESTED,
                dedupe_key="match_approval_requested:12:1001",
                payload={
                    "match_id": 12,
                    "provisional_result": "team_b_win",
                    "approval_deadline_at": "2026-03-20T12:44:56+00:00",
                    "phase_started": False,
                    "mention_discord_user_id": 80_021,
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_match_auto_penalty_message() -> None:
    channel = FakeDiscordChannel(
        id=900_022,
        guild=FakeDiscordGuild(id=910_022),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            f"<@80022> {MATCH_AUTO_PENALTY_APPLIED_NOTIFICATION_MESSAGE}",
            "結果: チーム A の勝ち",
            "ペナルティ: 誤報告",
            "現在の累積: 2",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=7,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key="match_finalized:auto_penalty:13:1002:automatic",
                payload={
                    "match_id": 13,
                    "final_result": "team_a_win",
                    "finalized_at": "2026-03-20T13:44:56+00:00",
                    "finalized_by_admin": False,
                    "auto_penalty_applied": True,
                    "mention_discord_user_id": 80_022,
                    "penalty_type": "incorrect_report",
                    "penalty_count": 2,
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_match_finalized_message_with_ratings() -> None:
    channel = FakeDiscordChannel(
        id=900_023,
        guild=FakeDiscordGuild(id=910_023),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            MATCH_FINALIZED_NOTIFICATION_MESSAGE,
            "結果: 引き分け",
            "更新後レート",
            "Team A",
            "    <@80031>: 1517",
            "    <@80032>: 1504",
            "    <@80033>: 1498",
            "Team B",
            "    <@80034>: 1502",
            "    <@80035>: 1495",
            "    <@80036>: 1488",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=8,
                event_type=OutboxEventType.MATCH_FINALIZED,
                dedupe_key="match_finalized:14:900023:automatic",
                payload={
                    "match_id": 14,
                    "final_result": "draw",
                    "finalized_at": "2026-03-20T14:44:56+00:00",
                    "finalized_by_admin": False,
                    "team_a_rating_entries": [
                        {"discord_user_id": 80_031, "rating": 1516.7},
                        {"discord_user_id": 80_032, "rating": 1504.2},
                        {"discord_user_id": 80_033, "rating": 1498.4},
                    ],
                    "team_b_rating_entries": [
                        {"discord_user_id": 80_034, "rating": 1501.8},
                        {"discord_user_id": 80_035, "rating": 1495.1},
                        {"discord_user_id": 80_036, "rating": 1488.3},
                    ],
                    "destination": {
                        "channel_id": channel.id,
                        "guild_id": channel.guild.id,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_daily_worker_started_admin_notification() -> None:
    channel = FakeDiscordChannel(
        id=900_024,
        guild=FakeDiscordGuild(id=910_024),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            "daily worker が起動しました。",
            "開始時刻: 2026-04-05 09:00 JST",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=9,
                event_type=OutboxEventType.ADMIN_OPERATIONS_NOTIFICATION,
                dedupe_key="admin_operations_notification:daily_worker_started:daily_worker:2026-04-05T00:00:00+00:00",
                payload={
                    "notification_kind": "daily_worker_started",
                    "worker_name": "daily_worker",
                    "occurred_at": "2026-04-05T00:00:00+00:00",
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel.id,
                        "guild_id": None,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_season_completed_notification() -> None:
    channel = FakeDiscordChannel(
        id=900_026,
        guild=FakeDiscordGuild(id=910_026),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            "シーズンの全試合が完了しました。",
            "season_id: 12",
            "season_name: 202603delta",
            "完了時刻: 2026-04-05 09:00 JST",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=11,
                event_type=OutboxEventType.SEASON_COMPLETED,
                dedupe_key="season_completed:12",
                payload={
                    "season_id": 12,
                    "season_name": "202603delta",
                    "completed_at": "2026-04-05T00:00:00+00:00",
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel.id,
                        "guild_id": None,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_season_top_rankings_notification() -> None:
    channel = FakeDiscordChannel(
        id=900_027,
        guild=FakeDiscordGuild(id=910_027),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            "シーズン最終順位表",
            "season_id: 12",
            "season_name: 202603delta",
            "match_format: 3v3",
            "items: 1-2",
            "",
            "1 / Bob / 1610.00",
            "2 / Alice / 1600.00",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=12,
                event_type=OutboxEventType.SEASON_TOP_RANKINGS,
                dedupe_key="season_top_rankings:12:3v3",
                payload={
                    "season_id": 12,
                    "season_name": "202603delta",
                    "completed_at": "2026-04-05T00:00:00+00:00",
                    "match_format": "3v3",
                    "entries": [
                        {
                            "rank": 1,
                            "display_name": "Bob",
                            "rating": 1610,
                        },
                        {
                            "rank": 2,
                            "display_name": "Alice",
                            "rating": 1600,
                        },
                    ],
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel.id,
                        "guild_id": None,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_renders_empty_season_top_rankings_notification() -> None:
    channel = FakeDiscordChannel(
        id=900_028,
        guild=FakeDiscordGuild(id=910_028),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    expected_message = "\n".join(
        [
            "シーズン最終順位表",
            "season_id: 12",
            "season_name: 202603delta",
            "match_format: 1v1",
            "",
            "対象者なし",
        ]
    )

    async def scenario() -> None:
        await publish_with_bound_loop(
            publisher,
            PendingOutboxEvent(
                id=13,
                event_type=OutboxEventType.SEASON_TOP_RANKINGS,
                dedupe_key="season_top_rankings:12:1v1",
                payload={
                    "season_id": 12,
                    "season_name": "202603delta",
                    "completed_at": "2026-04-05T00:00:00+00:00",
                    "match_format": "1v1",
                    "entries": [],
                    "destination": {
                        "kind": "channel",
                        "channel_id": channel.id,
                        "guild_id": None,
                    },
                },
                created_at=datetime.now(timezone.utc),
            ),
        )

    asyncio.run(scenario())

    assert channel.sent_messages == [expected_message]


def test_discord_outbox_publisher_raises_for_unknown_admin_operations_notification_kind() -> None:
    channel = FakeDiscordChannel(
        id=900_025,
        guild=FakeDiscordGuild(id=910_025),
    )
    client = FakeDiscordClient(channels={channel.id: channel})
    publisher = DiscordOutboxEventPublisher(client=client)

    with pytest.raises(ValueError, match="Unsupported admin operations notification kind"):
        asyncio.run(
            publish_with_bound_loop(
                publisher,
                PendingOutboxEvent(
                    id=10,
                    event_type=OutboxEventType.ADMIN_OPERATIONS_NOTIFICATION,
                    dedupe_key="admin_operations_notification:unknown:daily_worker:2026-04-05T00:00:00+00:00",
                    payload={
                        "notification_kind": "unknown",
                        "worker_name": "daily_worker",
                        "occurred_at": "2026-04-05T00:00:00+00:00",
                        "destination": {
                            "kind": "channel",
                            "channel_id": channel.id,
                            "guild_id": None,
                        },
                    },
                    created_at=datetime.now(timezone.utc),
                ),
            )
        )


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
