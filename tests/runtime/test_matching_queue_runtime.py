from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.models import Match, MatchQueueEntry, MatchQueueEntryStatus, OutboxEvent, OutboxEventType
from bot.runtime import (
    AsyncioMatchingQueueTaskScheduler,
    MatchingQueueRuntime,
    OutboxDispatcher,
    PendingOutboxEvent,
)
from bot.services import (
    ExpireQueueEntryResult,
    ExpireTask,
    MatchingQueueService,
    NoopMatchingQueueTaskScheduler,
    PresenceReminderResult,
    PresenceReminderTask,
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

    def publish(self, event: PendingOutboxEvent) -> None:
        self.events.append(event)


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
    join_result = crash_service.join_queue(player_id)

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
    for player_id in player_ids:
        crash_service.join_queue(player_id)

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
