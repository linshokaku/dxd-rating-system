from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from bot.models import (
    Match,
    MatchParticipant,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchQueueRemovalReason,
    OutboxEvent,
    OutboxEventType,
    Player,
)
from bot.services import (
    MATCH_QUEUE_TTL,
    PRESENCE_REMINDER_LEAD_TIME,
    ExpireTask,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    PlayerNotRegisteredError,
    PresenceReminderTask,
    QueueAlreadyJoinedError,
    QueueNotJoinedError,
    RetryableTaskError,
    register_player,
)


@dataclass
class RecordingTaskScheduler:
    presence_reminder_tasks: list[PresenceReminderTask] = field(default_factory=list)
    expire_tasks: list[ExpireTask] = field(default_factory=list)
    cancelled_presence_reminders: list[int] = field(default_factory=list)
    cancelled_expires: list[int] = field(default_factory=list)

    def schedule_presence_reminder(self, task: PresenceReminderTask) -> None:
        self.presence_reminder_tasks.append(task)

    def schedule_expire(self, task: ExpireTask) -> None:
        self.expire_tasks.append(task)

    def cancel_presence_reminder(self, queue_entry_id: int) -> None:
        self.cancelled_presence_reminders.append(queue_entry_id)

    def cancel_expire(self, queue_entry_id: int) -> None:
        self.cancelled_expires.append(queue_entry_id)

    def reset(self) -> None:
        self.presence_reminder_tasks.clear()
        self.expire_tasks.clear()
        self.cancelled_presence_reminders.clear()
        self.cancelled_expires.clear()


def create_matching_queue_service(
    session_factory: sessionmaker[Session],
    scheduler: RecordingTaskScheduler | None = None,
) -> MatchingQueueService:
    return MatchingQueueService(session_factory=session_factory, task_scheduler=scheduler)


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def create_players(
    session: Session,
    count: int,
    *,
    start_discord_user_id: int = 1_000,
) -> list[Player]:
    return [create_player(session, start_discord_user_id + index) for index in range(count)]


def create_queue_entry(
    session: Session,
    *,
    player_id: int,
    status: MatchQueueEntryStatus = MatchQueueEntryStatus.WAITING,
    joined_at: datetime | None = None,
    last_present_at: datetime | None = None,
    expire_at: datetime | None = None,
    revision: int = 1,
    last_reminded_revision: int | None = None,
    notification_channel_id: int | None = None,
    notification_guild_id: int | None = None,
    notification_mention_discord_user_id: int | None = None,
    notification_recorded_at: datetime | None = None,
    removed_at: datetime | None = None,
    removal_reason: MatchQueueRemovalReason | None = None,
    commit: bool = True,
) -> MatchQueueEntry:
    current_time = get_database_now(session)
    resolved_joined_at = joined_at or current_time
    resolved_last_present_at = last_present_at or resolved_joined_at
    resolved_expire_at = expire_at or (current_time + MATCH_QUEUE_TTL)

    queue_entry = MatchQueueEntry(
        player_id=player_id,
        status=status,
        joined_at=resolved_joined_at,
        last_present_at=resolved_last_present_at,
        expire_at=resolved_expire_at,
        revision=revision,
        last_reminded_revision=last_reminded_revision,
        notification_channel_id=notification_channel_id,
        notification_guild_id=notification_guild_id,
        notification_mention_discord_user_id=notification_mention_discord_user_id,
        notification_recorded_at=notification_recorded_at,
        removed_at=removed_at,
        removal_reason=removal_reason,
    )
    session.add(queue_entry)
    session.flush()
    if commit:
        session.commit()
    return queue_entry


def get_queue_entries_for_player(session: Session, player_id: int) -> list[MatchQueueEntry]:
    session.expire_all()
    return session.scalars(
        select(MatchQueueEntry)
        .where(MatchQueueEntry.player_id == player_id)
        .order_by(MatchQueueEntry.id)
    ).all()


def get_outbox_events(session: Session) -> list[OutboxEvent]:
    session.expire_all()
    return session.scalars(select(OutboxEvent).order_by(OutboxEvent.id)).all()


def create_waiting_entries(
    session: Session,
    players: Sequence[Player],
    *,
    base_joined_at: datetime | None = None,
    expire_at: datetime | None = None,
) -> list[MatchQueueEntry]:
    current_time = get_database_now(session)
    resolved_base_joined_at = base_joined_at or current_time
    resolved_expire_at = expire_at or (current_time + MATCH_QUEUE_TTL)

    entries: list[MatchQueueEntry] = []
    for index, player in enumerate(players):
        entry = create_queue_entry(
            session,
            player_id=player.id,
            joined_at=resolved_base_joined_at + timedelta(seconds=index),
            last_present_at=resolved_base_joined_at + timedelta(seconds=index),
            expire_at=resolved_expire_at,
            revision=1,
            commit=False,
        )
        entries.append(entry)
    session.commit()
    return entries


def assert_single_scheduled_timer(
    scheduler: RecordingTaskScheduler,
    *,
    queue_entry_id: int,
    expected_revision: int,
    expire_at: datetime,
) -> None:
    assert len(scheduler.presence_reminder_tasks) == 1
    assert len(scheduler.expire_tasks) == 1

    reminder_task = scheduler.presence_reminder_tasks[0]
    expire_task = scheduler.expire_tasks[0]

    assert reminder_task.queue_entry_id == queue_entry_id
    assert reminder_task.expected_revision == expected_revision
    assert reminder_task.remind_at == expire_at - PRESENCE_REMINDER_LEAD_TIME

    assert expire_task.queue_entry_id == queue_entry_id
    assert expire_task.expected_revision == expected_revision
    assert expire_task.expire_at == expire_at


# 未登録プレイヤーの `join` が失敗すること
def test_join_queue_raises_for_unregistered_player(session_factory: sessionmaker[Session]) -> None:
    service = create_matching_queue_service(session_factory)

    with pytest.raises(PlayerNotRegisteredError):
        service.join_queue(player_id=9999)


# 初回 `join` で `waiting` 行が作成され、`joined_at`、`last_present_at`、
# `expire_at`、`revision = 1`、`last_reminded_revision = NULL` が
# 設定されること
# `join` 後に在席確認リマインドタスクと expire タスクが登録されること
def test_join_queue_creates_waiting_entry_and_schedules_timers(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_001)
    scheduler = RecordingTaskScheduler()
    service = create_matching_queue_service(session_factory, scheduler)

    result = service.join_queue(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entries[0].id
    assert entries[0].status == MatchQueueEntryStatus.WAITING
    assert entries[0].joined_at == entries[0].last_present_at
    assert entries[0].expire_at > entries[0].joined_at
    assert entries[0].revision == 1
    assert entries[0].last_reminded_revision is None
    assert entries[0].removed_at is None
    assert entries[0].removal_reason is None
    assert_single_scheduled_timer(
        scheduler,
        queue_entry_id=entries[0].id,
        expected_revision=1,
        expire_at=entries[0].expire_at,
    )


# `join` 成功時に、新しく作成された `waiting` 行へ通知先コンテキストを保存する
# 保存する `channel_id` は `join` を実行した channel とする
# 保存する `mention_discord_user_id` は `join` を実行した Discord user ID とする
def test_join_queue_stores_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_001_1)
    service = create_matching_queue_service(session_factory)

    result = service.join_queue(
        player.id,
        notification_context=MatchingQueueNotificationContext(
            channel_id=333_001,
            guild_id=444_001,
            mention_discord_user_id=555_001,
        ),
    )

    entries = get_queue_entries_for_player(session, player.id)
    entry = entries[0]

    assert result.queue_entry_id == entry.id
    assert entry.notification_channel_id == 333_001
    assert entry.notification_guild_id == 444_001
    assert entry.notification_mention_discord_user_id == 555_001
    assert entry.notification_recorded_at == entry.joined_at


# 有効な `waiting` 行がある状態での重複 `join` が失敗すること
def test_join_queue_raises_when_player_is_already_waiting(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_002)
    service = create_matching_queue_service(session_factory)
    service.join_queue(player.id)

    with pytest.raises(QueueAlreadyJoinedError):
        service.join_queue(player.id)

    entries = get_queue_entries_for_player(session, player.id)
    assert len(entries) == 1
    assert entries[0].status == MatchQueueEntryStatus.WAITING


# 期限切れの `waiting` 行が残っている状態で `join` すると、古い行が
# `expired` になり、新しい `waiting` 行が作られること
# `join` 時の内部 cleanup では通知イベントを作らないこと
def test_join_queue_expires_stale_waiting_entry_and_creates_new_entry_without_outbox(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_003)
    now = get_database_now(session)
    stale_entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    result = service.join_queue(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert len(entries) == 2
    assert stale_entry.id == entries[0].id
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removed_at is not None
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert entries[1].id == result.queue_entry_id
    assert entries[1].status == MatchQueueEntryStatus.WAITING
    assert get_outbox_events(session) == []


# 有効な `waiting` 行に対する `present` で `last_present_at` と `expire_at` が
# 更新され、`revision` が増加し、`last_reminded_revision = NULL` に戻ること
# `present` 後に新しい在席確認リマインドタスクと expire タスクが
# 登録されること
def test_present_updates_waiting_entry_and_reschedules_timers(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_004)
    scheduler = RecordingTaskScheduler()
    service = create_matching_queue_service(session_factory, scheduler)
    joined = service.join_queue(player.id)
    scheduler.reset()

    result = service.present(player.id)

    entries = get_queue_entries_for_player(session, player.id)
    entry = entries[0]

    assert result.queue_entry_id == joined.queue_entry_id
    assert result.expired is False
    assert result.expire_at == entry.expire_at
    assert entry.status == MatchQueueEntryStatus.WAITING
    assert entry.revision == 2
    assert entry.last_reminded_revision is None
    assert entry.last_present_at >= entry.joined_at
    assert scheduler.cancelled_presence_reminders == [entry.id]
    assert scheduler.cancelled_expires == [entry.id]
    assert_single_scheduled_timer(
        scheduler,
        queue_entry_id=entry.id,
        expected_revision=2,
        expire_at=entry.expire_at,
    )


# `present` 成功時に、対象の `waiting` 行の通知先コンテキストを上書きする
# 上書き後は、新しい reminder / expire はその最新コンテキストを使う
def test_present_overwrites_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_004_1)
    initial_recorded_at = get_database_now(session) - timedelta(minutes=3)
    create_queue_entry(
        session,
        player_id=player.id,
        notification_channel_id=333_010,
        notification_guild_id=444_010,
        notification_mention_discord_user_id=555_010,
        notification_recorded_at=initial_recorded_at,
    )
    service = create_matching_queue_service(session_factory)

    result = service.present(
        player.id,
        notification_context=MatchingQueueNotificationContext(
            channel_id=333_011,
            guild_id=444_011,
            mention_discord_user_id=555_011,
        ),
    )

    entries = get_queue_entries_for_player(session, player.id)
    entry = entries[0]

    assert result.queue_entry_id == entry.id
    assert entry.notification_channel_id == 333_011
    assert entry.notification_guild_id == 444_011
    assert entry.notification_mention_discord_user_id == 555_011
    assert entry.notification_recorded_at == entry.last_present_at
    assert entry.notification_recorded_at != initial_recorded_at


# `waiting` 行が存在しない場合の `present` が失敗すること
def test_present_raises_when_player_has_no_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_005)
    service = create_matching_queue_service(session_factory)

    with pytest.raises(QueueNotJoinedError):
        service.present(player.id)


# `expire_at <= now()` の行に対する `present` は `expired` に遷移して
# timeout 応答になること
# `present` / `leave` が遅すぎて同期的に `expired` になった場合、
# 非同期通知イベントを作らないこと
def test_present_expires_stale_entry_and_does_not_create_outbox_event(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_006)
    scheduler = RecordingTaskScheduler()
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory, scheduler)

    result = service.present(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entry.id
    assert result.expired is True
    assert result.expire_at is None
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert get_outbox_events(session) == []
    assert scheduler.cancelled_presence_reminders == [entry.id]
    assert scheduler.cancelled_expires == [entry.id]


# 古い `revision` を持つ reminder / expire タスクが起きても no-op になること
def test_stale_revision_tasks_become_noop(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_007)
    service = create_matching_queue_service(session_factory)
    joined = service.join_queue(player.id)

    reminder_result = service.process_presence_reminder(joined.queue_entry_id, expected_revision=0)
    expire_result = service.process_expire(joined.queue_entry_id, expected_revision=0)

    entries = get_queue_entries_for_player(session, player.id)

    assert reminder_result.reminded is False
    assert expire_result.expired is False
    assert entries[0].status == MatchQueueEntryStatus.WAITING
    assert entries[0].last_reminded_revision is None
    assert get_outbox_events(session) == []


@pytest.mark.parametrize("handler_name", ["process_presence_reminder", "process_expire"])
def test_task_handlers_wrap_transient_db_errors_as_retryable(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
) -> None:
    service = create_matching_queue_service(session_factory)
    transient_error = OperationalError(
        "SELECT 1",
        {},
        psycopg.OperationalError("temporary db disconnect"),
        connection_invalidated=True,
    )

    def raise_transient_error(session: Session, queue_entry_id: int) -> MatchQueueEntry | None:
        del session, queue_entry_id
        raise transient_error

    monkeypatch.setattr(service, "_get_queue_entry_for_update", raise_transient_error)
    handler = getattr(service, handler_name)

    with pytest.raises(RetryableTaskError) as excinfo:
        handler(101, expected_revision=1)

    assert excinfo.value.__cause__ is transient_error


# 有効な `waiting` 行に対する `leave` で `left` に遷移し、`removed_at` と
# `removal_reason = 'user_leave'` が設定されること
# `leave` 後にローカルの在席確認リマインドタスクと expire タスクが
# cancel されること
def test_leave_marks_waiting_entry_as_left_and_cancels_timers(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_008)
    scheduler = RecordingTaskScheduler()
    service = create_matching_queue_service(session_factory, scheduler)
    joined = service.join_queue(player.id)
    scheduler.reset()

    result = service.leave(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == joined.queue_entry_id
    assert result.expired is False
    assert entries[0].status == MatchQueueEntryStatus.LEFT
    assert entries[0].removed_at is not None
    assert entries[0].removal_reason == MatchQueueRemovalReason.USER_LEAVE
    assert scheduler.cancelled_presence_reminders == [entries[0].id]
    assert scheduler.cancelled_expires == [entries[0].id]


# `waiting` 行がない場合の `leave` が冪等に成功扱いできること
def test_leave_is_idempotent_when_player_has_no_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_009)
    service = create_matching_queue_service(session_factory)

    result = service.leave(player.id)

    assert result.queue_entry_id is None
    assert result.expired is False


# `expire_at <= now()` の行に対する `leave` は `left` ではなく `expired` になること
# `present` / `leave` が遅すぎて同期的に `expired` になった場合、
# 非同期通知イベントを作らないこと
def test_leave_expires_stale_waiting_entry_without_creating_outbox_event(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_010)
    scheduler = RecordingTaskScheduler()
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory, scheduler)

    result = service.leave(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entry.id
    assert result.expired is True
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert get_outbox_events(session) == []
    assert scheduler.cancelled_presence_reminders == [entry.id]
    assert scheduler.cancelled_expires == [entry.id]


# `expire_at - 1分` に達した `waiting` 行に対して在席確認リマインドが
# 1 回だけ送られること
# 同じ `revision` に対して reminder タスクが複数回起きても、実際の通知は
# 1 回だけであること
# 同一事象に対して outbox イベントが重複生成されないこと
def test_process_presence_reminder_marks_revision_once_and_creates_single_outbox_event(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_011)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now + timedelta(seconds=30),
        revision=3,
    )
    service = create_matching_queue_service(session_factory)

    first_result = service.process_presence_reminder(entry.id, expected_revision=3)
    second_result = service.process_presence_reminder(entry.id, expected_revision=3)

    entries = get_queue_entries_for_player(session, player.id)
    outbox_events = get_outbox_events(session)

    assert first_result.reminded is True
    assert second_result.reminded is False
    assert entries[0].last_reminded_revision == 3
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == OutboxEventType.PRESENCE_REMINDER


# `matched`、`left`、`expired` の行にはリマインドが送られないこと
@pytest.mark.parametrize(
    "status",
    [
        MatchQueueEntryStatus.MATCHED,
        MatchQueueEntryStatus.LEFT,
        MatchQueueEntryStatus.EXPIRED,
    ],
)
def test_process_presence_reminder_is_noop_for_non_waiting_entries(
    session: Session,
    session_factory: sessionmaker[Session],
    status: MatchQueueEntryStatus,
) -> None:
    player = create_player(session, 10_100 + len(get_outbox_events(session)))
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        status=status,
        expire_at=now + timedelta(seconds=30),
        removed_at=now if status != MatchQueueEntryStatus.MATCHED else None,
        removal_reason=(
            MatchQueueRemovalReason.USER_LEAVE
            if status == MatchQueueEntryStatus.LEFT
            else MatchQueueRemovalReason.TIMEOUT
            if status == MatchQueueEntryStatus.EXPIRED
            else None
        ),
    )
    service = create_matching_queue_service(session_factory)

    result = service.process_presence_reminder(entry.id, expected_revision=1)

    assert result.reminded is False
    assert get_outbox_events(session) == []


# `present` で `revision` が進んだあとは、新しい 5 分サイクルで再度
# 1 回だけリマインド可能になること
def test_present_advances_revision_and_allows_reminder_in_next_cycle(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_012)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now + timedelta(seconds=30),
        revision=1,
    )
    service = create_matching_queue_service(session_factory)

    first_reminder = service.process_presence_reminder(entry.id, expected_revision=1)
    present_result = service.present(player.id)
    session.expire_all()
    refreshed_entry = session.scalar(select(MatchQueueEntry).where(MatchQueueEntry.id == entry.id))
    assert refreshed_entry is not None
    refreshed_entry.expire_at = get_database_now(session) + timedelta(seconds=30)
    session.commit()

    second_reminder = service.process_presence_reminder(
        refreshed_entry.id,
        expected_revision=refreshed_entry.revision,
    )

    outbox_events = get_outbox_events(session)

    assert first_reminder.reminded is True
    assert present_result.expired is False
    assert refreshed_entry.revision == 2
    assert second_reminder.reminded is True
    assert [event.event_type for event in outbox_events] == [
        OutboxEventType.PRESENCE_REMINDER,
        OutboxEventType.PRESENCE_REMINDER,
    ]


# `expire_at <= now()` の `waiting` 行が `expired` に遷移し、`removed_at` と
# `removal_reason = 'timeout'` が設定されること
# 通常の expire が info log を出すこと
# 同一事象に対して outbox イベントが重複生成されないこと
def test_process_expire_marks_waiting_entry_expired_creates_outbox_and_logs(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    player = create_player(session, 10_013)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    with caplog.at_level(logging.INFO, logger="bot.services.matching_queue"):
        first_result = service.process_expire(entry.id, expected_revision=1)
        second_result = service.process_expire(entry.id, expected_revision=1)

    entries = get_queue_entries_for_player(session, player.id)
    outbox_events = get_outbox_events(session)

    assert first_result.expired is True
    assert second_result.expired is False
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removed_at is not None
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == OutboxEventType.QUEUE_EXPIRED
    assert "Expired queue entry" in caplog.text


# `status != 'waiting'`、`revision` 不一致、`expire_at > now()` の場合に
# expire が no-op になること
def test_process_expire_is_noop_when_entry_is_not_due_or_not_waiting(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 3, start_discord_user_id=20_001)
    now = get_database_now(session)
    future_entry = create_queue_entry(
        session,
        player_id=players[0].id,
        expire_at=now + timedelta(minutes=1),
    )
    mismatched_revision_entry = create_queue_entry(
        session,
        player_id=players[1].id,
        expire_at=now - timedelta(seconds=1),
        revision=2,
    )
    matched_entry = create_queue_entry(
        session,
        player_id=players[2].id,
        status=MatchQueueEntryStatus.MATCHED,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    future_result = service.process_expire(future_entry.id, expected_revision=1)
    mismatch_result = service.process_expire(mismatched_revision_entry.id, expected_revision=1)
    matched_result = service.process_expire(matched_entry.id, expected_revision=1)

    session.expire_all()
    assert future_result.expired is False
    assert mismatch_result.expired is False
    assert matched_result.expired is False
    assert session.get(MatchQueueEntry, future_entry.id).status == MatchQueueEntryStatus.WAITING
    assert (
        session.get(MatchQueueEntry, mismatched_revision_entry.id).status
        == MatchQueueEntryStatus.WAITING
    )
    assert session.get(MatchQueueEntry, matched_entry.id).status == MatchQueueEntryStatus.MATCHED
    assert get_outbox_events(session) == []


# 待機人数が 6 人未満のとき、`try_create_matches()` が no-op で終了すること
def test_try_create_matches_is_noop_when_fewer_than_six_players_are_waiting(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 5, start_discord_user_id=30_001)
    create_waiting_entries(session, players)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches()

    session.expire_all()
    waiting_entries = session.scalars(select(MatchQueueEntry).order_by(MatchQueueEntry.id)).all()
    assert created_matches == ()
    assert len(waiting_entries) == 5
    assert all(entry.status == MatchQueueEntryStatus.WAITING for entry in waiting_entries)
    assert get_outbox_events(session) == []


# 6 人ちょうどの待機で 1 マッチが作成され、対象のキュー行が `matched` になること
def test_try_create_matches_creates_single_match_and_marks_entries_matched(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 6, start_discord_user_id=30_101)
    queue_entries = create_waiting_entries(session, players)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches()

    session.expire_all()
    match = session.scalar(select(Match))
    participants = session.scalars(select(MatchParticipant).order_by(MatchParticipant.id)).all()
    entries = session.scalars(select(MatchQueueEntry).order_by(MatchQueueEntry.id)).all()
    outbox_events = get_outbox_events(session)

    assert len(created_matches) == 1
    assert match is not None
    assert created_matches[0].match_id == match.id
    assert created_matches[0].queue_entry_ids == tuple(entry.id for entry in queue_entries)
    assert len(participants) == 6
    assert all(entry.status == MatchQueueEntryStatus.MATCHED for entry in entries)
    assert len(outbox_events) == 1
    assert outbox_events[0].event_type == OutboxEventType.MATCH_CREATED


# 12 人以上の待機で 1 回の `try_create_matches()` が複数マッチを連続生成できること
def test_try_create_matches_creates_multiple_matches_when_twelve_players_are_waiting(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 12, start_discord_user_id=30_201)
    create_waiting_entries(session, players)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches()

    session.expire_all()
    matches = session.scalars(select(Match).order_by(Match.id)).all()
    participants = session.scalars(select(MatchParticipant)).all()
    entries = session.scalars(select(MatchQueueEntry)).all()

    assert len(created_matches) == 2
    assert len(matches) == 2
    assert len(participants) == 12
    assert all(entry.status == MatchQueueEntryStatus.MATCHED for entry in entries)


# 候補抽出が `joined_at, id` の古い順で行われること
# `expire_at <= now()` の行が候補から除外されること
def test_try_create_matches_uses_join_order_and_excludes_expired_entries(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 7, start_discord_user_id=30_301)
    now = get_database_now(session)
    active_entries = create_waiting_entries(session, players, base_joined_at=now)
    active_entries[0].expire_at = now - timedelta(seconds=1)
    session.commit()
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches()

    assert len(created_matches) == 1
    assert created_matches[0].queue_entry_ids == tuple(entry.id for entry in active_entries[1:])


# `matched` になった行に対して後から reminder / expire タスクが起きても
# no-op になること
def test_matched_entries_make_reminder_and_expire_tasks_noop(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 6, start_discord_user_id=30_401)
    entries = create_waiting_entries(session, players)
    service = create_matching_queue_service(session_factory)
    service.try_create_matches()

    reminder_result = service.process_presence_reminder(entries[0].id, expected_revision=1)
    expire_result = service.process_expire(entries[0].id, expected_revision=1)

    outbox_events = get_outbox_events(session)

    assert reminder_result.reminded is False
    assert expire_result.expired is False
    assert [event.event_type for event in outbox_events] == [OutboxEventType.MATCH_CREATED]


# 起動時に期限切れ行の cleanup が行われること
def test_run_startup_sync_cleans_up_expired_entries(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 40_001)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    result = service.run_startup_sync()

    session.expire_all()
    refreshed_entry = session.get(MatchQueueEntry, entry.id)
    assert refreshed_entry is not None
    assert result.cleaned_up_queue_entry_ids == (entry.id,)
    assert refreshed_entry.status == MatchQueueEntryStatus.EXPIRED


# 起動時に `try_create_matches()` が実行され、
# すでに 6 人以上待機しているケースを回収できること
def test_run_startup_sync_creates_matches_for_existing_waiting_entries(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 6, start_discord_user_id=40_101)
    create_waiting_entries(session, players)
    service = create_matching_queue_service(session_factory)

    result = service.run_startup_sync()

    assert len(result.created_match_ids) == 1


# 起動時に reminder 対象の行へ即時リマインドできること
def test_run_startup_sync_immediately_processes_due_presence_reminders(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 40_201)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now + timedelta(seconds=30),
    )
    service = create_matching_queue_service(session_factory)

    result = service.run_startup_sync()

    session.expire_all()
    refreshed_entry = session.get(MatchQueueEntry, entry.id)
    outbox_events = get_outbox_events(session)

    assert result.reminded_queue_entry_ids == (entry.id,)
    assert refreshed_entry is not None
    assert refreshed_entry.last_reminded_revision == refreshed_entry.revision
    assert [event.event_type for event in outbox_events] == [OutboxEventType.PRESENCE_REMINDER]


# 起動時に将来期限の `waiting` 行へ reminder タスクと expire タスクが
# 再登録されること
# 起動時再同期で `last_reminded_revision = revision` の行には
# reminder タスクを再登録しないこと
def test_run_startup_sync_reschedules_future_tasks_and_skips_already_reminded_entries(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 2, start_discord_user_id=40_301)
    scheduler = RecordingTaskScheduler()
    now = get_database_now(session)
    first_entry = create_queue_entry(
        session,
        player_id=players[0].id,
        expire_at=now + timedelta(minutes=3),
        revision=2,
        last_reminded_revision=None,
    )
    second_entry = create_queue_entry(
        session,
        player_id=players[1].id,
        expire_at=now + timedelta(minutes=3),
        revision=4,
        last_reminded_revision=4,
    )
    service = create_matching_queue_service(session_factory, scheduler)

    result = service.run_startup_sync()

    assert result.rescheduled_reminder_queue_entry_ids == (first_entry.id,)
    assert result.rescheduled_expire_queue_entry_ids == (first_entry.id, second_entry.id)
    assert [task.queue_entry_id for task in scheduler.presence_reminder_tasks] == [first_entry.id]
    assert sorted(task.queue_entry_id for task in scheduler.expire_tasks) == [
        first_entry.id,
        second_entry.id,
    ]


# reconcile による cleanup が発生した場合に warning log を出すこと
def test_run_reconcile_cycle_logs_warning_when_cleanup_occurs(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    player = create_player(session, 40_401)
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    with caplog.at_level(logging.WARNING, logger="bot.services.matching_queue"):
        result = service.run_reconcile_cycle()

    assert result.cleaned_up_queue_entry_ids == (entry.id,)
    assert "Cleanup expired queue entries" in caplog.text


# `presence_reminder`、`queue_expired`、`match_created` の
# イベント種別が正しく生成されること
def test_matching_queue_outbox_event_types_are_generated_for_supported_flows(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    reminder_player = create_player(session, 50_001)
    expired_player = create_player(session, 50_002)
    match_players = create_players(session, 6, start_discord_user_id=50_100)
    now = get_database_now(session)
    reminder_entry = create_queue_entry(
        session,
        player_id=reminder_player.id,
        expire_at=now + timedelta(seconds=30),
    )
    expired_entry = create_queue_entry(
        session,
        player_id=expired_player.id,
        expire_at=now - timedelta(seconds=1),
    )
    create_waiting_entries(session, match_players, base_joined_at=now + timedelta(seconds=1))
    service = create_matching_queue_service(session_factory)

    service.process_presence_reminder(reminder_entry.id, expected_revision=1)
    service.process_expire(expired_entry.id, expected_revision=1)
    service.try_create_matches()

    event_types = [event.event_type for event in get_outbox_events(session)]

    assert event_types == [
        OutboxEventType.PRESENCE_REMINDER,
        OutboxEventType.QUEUE_EXPIRED,
        OutboxEventType.MATCH_CREATED,
    ]
