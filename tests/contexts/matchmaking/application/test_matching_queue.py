from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

import psycopg
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import (
    InvalidQueueNameError,
    PlayerNotRegisteredError,
    QueueAlreadyJoinedError,
    QueueJoinNotAllowedError,
    QueueJoinRestrictedError,
    QueueNotJoinedError,
    RetryableTaskError,
)
from dxd_rating.contexts.matchmaking.application import (
    MATCH_QUEUE_TTL,
    MatchingQueueNotificationContext,
    MatchingQueueService,
)
from dxd_rating.contexts.players.application import register_player
from dxd_rating.contexts.restrictions.application import (
    PlayerAccessRestrictionDuration,
    PlayerAccessRestrictionService,
)
from dxd_rating.platform.db.models import (
    Match,
    MatchFormat,
    MatchParticipant,
    MatchParticipantTeam,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchQueueRemovalReason,
    OutboxEvent,
    OutboxEventType,
    Player,
    PlayerAccessRestrictionType,
    PlayerFormatStats,
)
from dxd_rating.shared.constants import (
    MatchQueueClassDefinition,
    get_match_queue_class_definition_by_id,
    get_match_queue_class_definition_by_name,
)

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_DEFINITION = get_match_queue_class_definition_by_name(DEFAULT_MATCH_FORMAT, "low")
assert DEFAULT_QUEUE_DEFINITION is not None
DEFAULT_QUEUE_NAME = DEFAULT_QUEUE_DEFINITION.queue_name
DEFAULT_QUEUE_CLASS_ID = DEFAULT_QUEUE_DEFINITION.queue_class_id
SECOND_QUEUE_DEFINITION = get_match_queue_class_definition_by_name(DEFAULT_MATCH_FORMAT, "high")
assert SECOND_QUEUE_DEFINITION is not None
SECOND_QUEUE_CLASS_ID = SECOND_QUEUE_DEFINITION.queue_class_id


def create_matching_queue_service(
    session_factory: sessionmaker[Session],
    *,
    queue_class_definitions: Sequence[MatchQueueClassDefinition] | None = None,
    random_generator: object | None = None,
) -> MatchingQueueService:
    return MatchingQueueService(
        session_factory=session_factory,
        queue_class_definitions=queue_class_definitions,
        random_generator=random_generator,
    )


def get_database_now(session: Session) -> datetime:
    return session.execute(select(func.now())).scalar_one()


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def get_player_format_stats(
    session: Session,
    player_id: int,
    match_format: MatchFormat = DEFAULT_MATCH_FORMAT,
) -> PlayerFormatStats:
    format_stats = session.get(
        PlayerFormatStats,
        {"player_id": player_id, "match_format": match_format},
    )
    assert format_stats is not None
    return format_stats


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
    queue_class_id: str = DEFAULT_QUEUE_CLASS_ID,
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
    player = session.get(Player, player_id)
    if player is None:
        raise ValueError(f"Player is not registered: {player_id}")
    queue_class_definition = get_match_queue_class_definition_by_id(queue_class_id)
    if queue_class_definition is None:
        raise ValueError(f"Unknown queue_class_id: {queue_class_id}")

    resolved_notification_channel_id = notification_channel_id
    resolved_notification_guild_id = notification_guild_id
    resolved_notification_mention_discord_user_id = (
        player.discord_user_id
        if notification_mention_discord_user_id is None
        else notification_mention_discord_user_id
    )
    resolved_notification_recorded_at = notification_recorded_at
    if (
        resolved_notification_channel_id is None
        and resolved_notification_guild_id is None
        and resolved_notification_recorded_at is None
    ):
        resolved_notification_channel_id = 600_000 + player_id
        resolved_notification_guild_id = 700_000 + player_id
        resolved_notification_recorded_at = resolved_last_present_at

    queue_entry = MatchQueueEntry(
        player_id=player_id,
        match_format=queue_class_definition.match_format,
        queue_class_id=queue_class_id,
        status=status,
        joined_at=resolved_joined_at,
        last_present_at=resolved_last_present_at,
        expire_at=resolved_expire_at,
        revision=revision,
        last_reminded_revision=last_reminded_revision,
        notification_channel_id=resolved_notification_channel_id,
        notification_guild_id=resolved_notification_guild_id,
        notification_mention_discord_user_id=resolved_notification_mention_discord_user_id,
        notification_recorded_at=resolved_notification_recorded_at,
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
    queue_class_id: str = DEFAULT_QUEUE_CLASS_ID,
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
            queue_class_id=queue_class_id,
            joined_at=resolved_base_joined_at + timedelta(seconds=index),
            last_present_at=resolved_base_joined_at + timedelta(seconds=index),
            expire_at=resolved_expire_at,
            revision=1,
            commit=False,
        )
        entries.append(entry)
    session.commit()
    return entries


# 未登録プレイヤーの `join` が失敗すること
def test_join_queue_raises_for_unregistered_player(session_factory: sessionmaker[Session]) -> None:
    service = create_matching_queue_service(session_factory)

    with pytest.raises(PlayerNotRegisteredError):
        service.join_queue(
            player_id=9999,
            match_format=DEFAULT_MATCH_FORMAT,
            queue_name=DEFAULT_QUEUE_NAME,
        )


def test_join_queue_raises_for_invalid_queue_name(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_000)
    service = create_matching_queue_service(session_factory)

    with pytest.raises(InvalidQueueNameError):
        service.join_queue(player.id, DEFAULT_MATCH_FORMAT, "unknown")


def test_join_queue_rejects_player_when_rating_is_outside_allowed_range(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_000_1)
    player_format_stats = get_player_format_stats(session, player.id)
    player_format_stats.rating = 2_100
    session.commit()
    queue_class_definitions = (
        MatchQueueClassDefinition(
            match_format=DEFAULT_MATCH_FORMAT,
            queue_class_id="restricted_low",
            queue_name="low",
            description="low",
            target_rating=1_200,
        ),
        MatchQueueClassDefinition(
            match_format=DEFAULT_MATCH_FORMAT,
            queue_class_id="restricted_mid",
            queue_name="mid",
            description="mid",
            target_rating=1_500,
        ),
        MatchQueueClassDefinition(
            match_format=DEFAULT_MATCH_FORMAT,
            queue_class_id="restricted_high",
            queue_name="high",
            description="high",
            target_rating=1_800,
        ),
    )
    service = create_matching_queue_service(
        session_factory,
        queue_class_definitions=queue_class_definitions,
    )

    with pytest.raises(QueueJoinNotAllowedError):
        service.join_queue(player.id, DEFAULT_MATCH_FORMAT, "mid")


def test_join_queue_raises_when_player_has_active_queue_join_restriction(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_000_2)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=50_001,
    )
    service = create_matching_queue_service(session_factory)

    with pytest.raises(QueueJoinRestrictedError, match="現在キュー参加を制限されています。"):
        service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)


# 初回 `join` で `waiting` 行が作成され、`joined_at`、`last_present_at`、
# `expire_at`、`revision = 1`、`last_reminded_revision = NULL` が
# 設定されること
# `join` 結果に runtime 側がスケジュールに必要な情報が含まれること
def test_join_queue_creates_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_001)
    service = create_matching_queue_service(session_factory)

    result = service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entries[0].id
    assert result.revision == 1
    assert result.expire_at == entries[0].expire_at
    assert result.queue_class_id == DEFAULT_QUEUE_CLASS_ID
    assert entries[0].queue_class_id == DEFAULT_QUEUE_CLASS_ID
    assert entries[0].status == MatchQueueEntryStatus.WAITING
    assert entries[0].joined_at == entries[0].last_present_at
    assert entries[0].expire_at > entries[0].joined_at
    assert entries[0].revision == 1
    assert entries[0].last_reminded_revision is None
    assert entries[0].notification_mention_discord_user_id == player.discord_user_id
    assert entries[0].removed_at is None
    assert entries[0].removal_reason is None


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
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
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
    service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

    with pytest.raises(QueueAlreadyJoinedError):
        service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

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

    result = service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

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
# `present` 結果に runtime 側が再スケジュールに必要な情報が含まれること
def test_present_updates_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_004)
    service = create_matching_queue_service(session_factory)
    joined = service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

    result = service.present(player.id)

    entries = get_queue_entries_for_player(session, player.id)
    entry = entries[0]

    assert result.queue_entry_id == joined.queue_entry_id
    assert result.expired is False
    assert result.revision == 2
    assert result.expire_at == entry.expire_at
    assert entry.status == MatchQueueEntryStatus.WAITING
    assert entry.revision == 2
    assert entry.last_reminded_revision is None
    assert entry.last_present_at >= entry.joined_at
    assert entry.notification_mention_discord_user_id == player.discord_user_id


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
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    result = service.present(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entry.id
    assert result.expired is True
    assert result.revision is None
    assert result.expire_at is None
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert get_outbox_events(session) == []


# 古い `revision` を持つ reminder / expire タスクが起きても no-op になること
def test_stale_revision_tasks_become_noop(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_007)
    service = create_matching_queue_service(session_factory)
    joined = service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

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
def test_leave_marks_waiting_entry_as_left(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    player = create_player(session, 10_008)
    service = create_matching_queue_service(session_factory)
    joined = service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)

    result = service.leave(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == joined.queue_entry_id
    assert result.expired is False
    assert entries[0].status == MatchQueueEntryStatus.LEFT
    assert entries[0].removed_at is not None
    assert entries[0].removal_reason == MatchQueueRemovalReason.USER_LEAVE


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
    now = get_database_now(session)
    entry = create_queue_entry(
        session,
        player_id=player.id,
        expire_at=now - timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    result = service.leave(player.id)

    entries = get_queue_entries_for_player(session, player.id)

    assert result.queue_entry_id == entry.id
    assert result.expired is True
    assert entries[0].status == MatchQueueEntryStatus.EXPIRED
    assert entries[0].removal_reason == MatchQueueRemovalReason.TIMEOUT
    assert get_outbox_events(session) == []


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

    with caplog.at_level(
        logging.INFO,
        logger="dxd_rating.contexts.matchmaking.application.matching_queue",
    ):
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


def test_try_create_matches_does_not_mix_players_from_different_queue_classes(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    low_players = create_players(session, 3, start_discord_user_id=30_051)
    high_players = create_players(session, 3, start_discord_user_id=30_061)
    create_waiting_entries(session, low_players, queue_class_id=DEFAULT_QUEUE_CLASS_ID)
    create_waiting_entries(session, high_players, queue_class_id=SECOND_QUEUE_CLASS_ID)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches()

    session.expire_all()
    waiting_entries = session.scalars(select(MatchQueueEntry).order_by(MatchQueueEntry.id)).all()
    assert created_matches == ()
    assert len(waiting_entries) == 6
    assert all(entry.status == MatchQueueEntryStatus.WAITING for entry in waiting_entries)
    assert {entry.queue_class_id for entry in waiting_entries} == {
        DEFAULT_QUEUE_CLASS_ID,
        SECOND_QUEUE_CLASS_ID,
    }


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
    assert len(outbox_events) == len({entry.notification_channel_id for entry in entries})
    assert all(event.event_type == OutboxEventType.MATCH_CREATED for event in outbox_events)
    assert all("destination" in event.payload for event in outbox_events)


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


def test_try_create_matches_creates_balanced_two_vs_two_match_from_four_players(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 4, start_discord_user_id=30_250)
    ratings_by_player_id = {
        players[0].id: 1800.0,
        players[1].id: 1600.0,
        players[2].id: 1400.0,
        players[3].id: 1200.0,
    }
    for player in players:
        get_player_format_stats(
            session, player.id, MatchFormat.TWO_VS_TWO
        ).rating = ratings_by_player_id[player.id]
    queue_definition = get_match_queue_class_definition_by_name(MatchFormat.TWO_VS_TWO, "low")
    assert queue_definition is not None
    create_waiting_entries(session, players, queue_class_id=queue_definition.queue_class_id)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches(queue_definition.queue_class_id)

    session.expire_all()
    participants = session.scalars(select(MatchParticipant).order_by(MatchParticipant.id)).all()

    assert len(created_matches) == 1
    assert len(participants) == 4
    team_a_player_ids = {
        participant.player_id
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_A
    }
    team_b_player_ids = {
        participant.player_id
        for participant in participants
        if participant.team == MatchParticipantTeam.TEAM_B
    }
    assert {frozenset(team_a_player_ids), frozenset(team_b_player_ids)} == {
        frozenset({players[0].id, players[3].id}),
        frozenset({players[1].id, players[2].id}),
    }


def test_try_create_matches_creates_two_one_vs_one_matches_from_four_players(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    players = create_players(session, 4, start_discord_user_id=30_260)
    ratings_by_player_id = {
        players[0].id: 1800.0,
        players[1].id: 1700.0,
        players[2].id: 1500.0,
        players[3].id: 1400.0,
    }
    for player in players:
        get_player_format_stats(
            session, player.id, MatchFormat.ONE_VS_ONE
        ).rating = ratings_by_player_id[player.id]
    queue_definition = get_match_queue_class_definition_by_name(MatchFormat.ONE_VS_ONE, "low")
    assert queue_definition is not None
    create_waiting_entries(session, players, queue_class_id=queue_definition.queue_class_id)
    service = create_matching_queue_service(session_factory)

    created_matches = service.try_create_matches(queue_definition.queue_class_id)

    session.expire_all()
    matches = session.scalars(select(Match).order_by(Match.id)).all()
    participants = session.scalars(
        select(MatchParticipant).order_by(MatchParticipant.match_id, MatchParticipant.id)
    ).all()
    participants_by_match_id: dict[int, set[int]] = {}
    for participant in participants:
        participants_by_match_id.setdefault(participant.match_id, set()).add(participant.player_id)

    assert len(created_matches) == 2
    assert len(matches) == 2
    assert {frozenset(player_ids) for player_ids in participants_by_match_id.values()} == {
        frozenset({players[0].id, players[1].id}),
        frozenset({players[2].id, players[3].id}),
    }


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
    assert len(outbox_events) == len({entry.notification_channel_id for entry in entries})
    assert all(event.event_type == OutboxEventType.MATCH_CREATED for event in outbox_events)


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
    match_entries = create_waiting_entries(
        session,
        match_players,
        base_joined_at=now + timedelta(seconds=1),
    )
    service = create_matching_queue_service(session_factory)

    service.process_presence_reminder(reminder_entry.id, expected_revision=1)
    service.process_expire(expired_entry.id, expected_revision=1)
    service.try_create_matches()

    event_types = [event.event_type for event in get_outbox_events(session)]

    expected_match_created_event_count = len(
        {entry.notification_channel_id for entry in match_entries}
    )
    assert event_types[:2] == [
        OutboxEventType.PRESENCE_REMINDER,
        OutboxEventType.QUEUE_EXPIRED,
    ]
    assert event_types[2:] == [OutboxEventType.MATCH_CREATED] * expected_match_created_event_count
