from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.commands import BotCommandHandlers, register_app_commands
from bot.config import Settings
from bot.models import (
    MatchFormat,
    MatchQueueEntry,
    MatchQueueEntryStatus,
    MatchSpectator,
    PenaltyType,
    Player,
    PlayerAccessRestriction,
    PlayerAccessRestrictionType,
    PlayerFormatStats,
    PlayerPenalty,
)
from bot.runtime import MatchRuntime
from bot.services import (
    MatchFlowService,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    PlayerAccessRestrictionDuration,
    PlayerAccessRestrictionService,
    register_player,
)

DEFAULT_MATCH_FORMAT = MatchFormat.THREE_VS_THREE
DEFAULT_QUEUE_NAME = "low"


@dataclass(frozen=True)
class FakeUser:
    id: int
    name: str | None = None
    global_name: str | None = None
    nick: str | None = None

    def __post_init__(self) -> None:
        if self.name is None:
            object.__setattr__(self, "name", f"user-{self.id}")


@dataclass
class FakeInteractionResponse:
    messages: list[str] = field(default_factory=list)

    async def send_message(self, content: str) -> None:
        self.messages.append(content)


@dataclass
class FakeInteraction:
    user: FakeUser
    channel_id: int | None = 1_001
    guild_id: int | None = 2_001
    response: FakeInteractionResponse = field(default_factory=FakeInteractionResponse)


def as_interaction(fake_interaction: FakeInteraction) -> discord.Interaction[discord.Client]:
    return cast(discord.Interaction[discord.Client], fake_interaction)


def create_settings(*, super_admin_user_ids: frozenset[int] = frozenset()) -> Settings:
    return Settings.model_construct(
        discord_bot_token="discord-token",
        database_url="postgresql+psycopg://user:password@localhost:5432/dxd_rating",
        log_level="INFO",
        super_admin_user_ids=super_admin_user_ids,
    )


def create_handlers(
    session_factory: sessionmaker[Session],
    *,
    super_admin_user_ids: frozenset[int] = frozenset(),
    matching_queue_service: MatchingQueueService | MatchRuntime | None = None,
) -> BotCommandHandlers:
    resolved_matching_queue_service = matching_queue_service
    if isinstance(matching_queue_service, MatchingQueueService):
        resolved_matching_queue_service = MatchRuntime(
            service=matching_queue_service,
            match_service=MatchFlowService(session_factory),
        )

    return BotCommandHandlers(
        settings=create_settings(super_admin_user_ids=super_admin_user_ids),
        session_factory=session_factory,
        matching_queue_service=resolved_matching_queue_service,
    )


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


def format_player_info_message(
    stats_by_format: dict[MatchFormat, tuple[float, int, int, int, int, datetime | None]],
) -> str:
    lines = ["プレイヤー情報"]
    for match_format in (
        MatchFormat.ONE_VS_ONE,
        MatchFormat.TWO_VS_TWO,
        MatchFormat.THREE_VS_THREE,
    ):
        rating, games_played, wins, losses, draws, last_played_at = stats_by_format[match_format]
        lines.extend(
            [
                match_format.value,
                f"rating: {rating:.2f}",
                f"games_played: {games_played}",
                f"wins: {wins}",
                f"losses: {losses}",
                f"draws: {draws}",
                f"last_played_at: {'-' if last_played_at is None else last_played_at.isoformat()}",
            ]
        )
    return "\n".join(lines)


def get_queue_entry(session: Session, player_id: int) -> MatchQueueEntry:
    session.expire_all()
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player_id)
    )
    assert queue_entry is not None
    return queue_entry


def create_match(
    session: Session,
    session_factory: sessionmaker[Session],
    *,
    start_discord_user_id: int,
    channel_id: int,
    guild_id: int,
) -> tuple[int, list[Player]]:
    players = [create_player(session, start_discord_user_id + offset) for offset in range(6)]
    queue_service = MatchingQueueService(session_factory)
    for player in players:
        queue_service.join_queue(
            player.id,
            DEFAULT_MATCH_FORMAT,
            DEFAULT_QUEUE_NAME,
            notification_context=MatchingQueueNotificationContext(
                channel_id=channel_id,
                guild_id=guild_id,
                mention_discord_user_id=player.discord_user_id,
            ),
        )

    created_matches = queue_service.try_create_matches()

    assert len(created_matches) == 1
    return created_matches[0].match_id, players


def test_register_command_registers_requesting_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_678
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.register(as_interaction(interaction)))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == interaction.user.id)
    )

    assert interaction.response.messages == ["登録が完了しました。"]
    assert persisted_player is not None
    assert persisted_player.display_name == f"user-{discord_user_id}"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_register_command_returns_duplicate_message_for_registered_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_679
    create_player(session, discord_user_id)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.register(as_interaction(interaction)))

    assert interaction.response.messages == ["すでに登録済みです。"]
    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )
    assert persisted_player is not None
    assert persisted_player.display_name == f"user-{discord_user_id}"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_join_command_joins_requesting_player_and_stores_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_680
    player = create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(
            id=discord_user_id,
            name="queue-user",
            global_name="queue-global",
            nick="queue-guild",
        ),
        channel_id=3_001,
        guild_id=4_001,
    )

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    queue_entry = get_queue_entry(session, player.id)
    session.expire_all()
    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == discord_user_id)
    )

    assert interaction.response.messages == ["キューに参加しました。5分間マッチングします。"]
    assert queue_entry.notification_channel_id == 3_001
    assert queue_entry.notification_guild_id == 4_001
    assert queue_entry.notification_mention_discord_user_id == discord_user_id
    assert persisted_player is not None
    assert persisted_player.display_name == "queue-guild"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_join_command_requires_registered_player(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_681))

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert interaction.response.messages == [
        "プレイヤー登録が必要です。先に /register を実行してください。"
    ]


def test_join_command_returns_restricted_message_for_queue_join_restricted_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_681_1
    player = create_player(session, discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=99_001,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(
        handlers.join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
        )
    )

    assert interaction.response.messages == ["現在キュー参加を制限されています。"]


def test_present_command_updates_waiting_entry_and_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_682
    player = create_player(session, discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(
        player.id,
        DEFAULT_MATCH_FORMAT,
        DEFAULT_QUEUE_NAME,
        notification_context=MatchingQueueNotificationContext(
            channel_id=5_001,
            guild_id=6_001,
            mention_discord_user_id=7_001,
        ),
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=matching_queue_service,
    )
    interaction = FakeInteraction(
        user=FakeUser(id=discord_user_id),
        channel_id=8_001,
        guild_id=9_001,
    )

    asyncio.run(handlers.present(as_interaction(interaction)))

    queue_entry = get_queue_entry(session, player.id)

    assert interaction.response.messages == ["在席を更新しました。次の期限は5分後です。"]
    assert queue_entry.notification_channel_id == 8_001
    assert queue_entry.notification_guild_id == 9_001
    assert queue_entry.notification_mention_discord_user_id == discord_user_id


def test_present_command_returns_not_joined_message_for_non_waiting_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_683
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.present(as_interaction(interaction)))

    assert interaction.response.messages == ["キューに参加していません。"]


def test_leave_command_is_idempotent_for_registered_player_without_waiting_entry(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_684
    create_player(session, discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.leave(as_interaction(interaction)))

    assert interaction.response.messages == ["キューから退出しました。"]


def test_player_info_command_returns_requesting_player_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_686
    player = create_player(session, discord_user_id)
    three_vs_three_stats = get_player_format_stats(session, player.id)
    three_vs_three_stats.rating = 1512.5
    three_vs_three_stats.games_played = 8
    three_vs_three_stats.wins = 5
    three_vs_three_stats.losses = 2
    three_vs_three_stats.draws = 1
    three_vs_three_stats.last_played_at = datetime(2026, 3, 20, 12, 34, 56, tzinfo=timezone.utc)
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert interaction.response.messages == [
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (
                    1512.5,
                    8,
                    5,
                    2,
                    1,
                    datetime(2026, 3, 20, 12, 34, 56, tzinfo=timezone.utc),
                ),
            }
        )
    ]


def test_player_info_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_687))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert interaction.response.messages == [
        "プレイヤー登録が必要です。先に /register を実行してください。"
    ]


def test_match_spectate_command_registers_requesting_player_as_spectator(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_700,
        channel_id=13_001,
        guild_id=14_001,
    )
    spectator_discord_user_id = 123_456_789_012_345_706
    spectator = create_player(session, spectator_discord_user_id)
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=spectator_discord_user_id))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), match_id))

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert interaction.response.messages == ["観戦応募を受け付けました。現在 1 / 6 人です。"]
    assert persisted_spectator is not None


def test_match_spectate_command_requires_registered_player(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_707))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), 1))

    assert interaction.response.messages == [
        "プレイヤー登録が必要です。先に /register を実行してください。"
    ]


def test_match_spectate_command_returns_restricted_message_for_restricted_player(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_707_1,
        channel_id=13_002,
        guild_id=14_002,
    )
    spectator_discord_user_id = 123_456_789_012_345_713
    spectator = create_player(session, spectator_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=99_002,
    )
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=spectator_discord_user_id))

    asyncio.run(handlers.match_spectate(as_interaction(interaction), match_id))

    assert interaction.response.messages == ["現在観戦を制限されています。"]


def test_dev_match_spectate_registers_target_dummy_user_as_spectator(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_710,
        channel_id=13_010,
        guild_id=14_010,
    )
    spectator = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_match_spectate(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    persisted_spectator = session.scalar(
        select(MatchSpectator).where(
            MatchSpectator.match_id == match_id,
            MatchSpectator.player_id == spectator.id,
        )
    )

    assert interaction.response.messages == ["指定したユーザーの観戦応募を受け付けました。"]
    assert persisted_spectator is not None


def test_dev_match_spectate_returns_restricted_message_for_restricted_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 778
    match_id, _ = create_match(
        session,
        session_factory,
        start_discord_user_id=123_456_789_012_345_711,
        channel_id=13_011,
        guild_id=14_011,
    )
    spectator = create_player(session, target_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        spectator.id,
        PlayerAccessRestrictionType.SPECTATE,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=10,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_match_spectate(
            as_interaction(interaction),
            match_id,
            str(target_discord_user_id),
        )
    )

    assert interaction.response.messages == ["指定したユーザーは現在観戦を制限されています。"]


def test_dev_register_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "123456789012345685"))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == 123_456_789_012_345_685)
    )

    assert interaction.response.messages == ["このコマンドは管理者のみ実行できます。"]
    assert persisted_player is None


def test_dev_register_sets_fixed_dummy_display_name(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "777"))

    persisted_player = session.scalar(select(Player).where(Player.discord_user_id == 777))

    assert interaction.response.messages == ["ダミーユーザーを登録しました。"]
    assert persisted_player is not None
    assert persisted_player.display_name == "<dummy_777>"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at


def test_dev_register_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "not-a-number"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_register_rejects_non_dummy_discord_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_register(as_interaction(interaction), "1001"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_join_targets_provided_user_and_uses_target_for_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(
        user=FakeUser(id=executor_discord_user_id),
        channel_id=11_001,
        guild_id=12_001,
    )

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    queue_entry = get_queue_entry(session, player.id)

    assert interaction.response.messages == ["指定したユーザーをキューに参加させました。"]
    assert queue_entry.notification_channel_id == 11_001
    assert queue_entry.notification_guild_id == 12_001
    assert queue_entry.notification_mention_discord_user_id == target_discord_user_id


def test_dev_join_returns_restricted_message_for_restricted_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 779
    player = create_player(session, target_discord_user_id)
    restriction_service = PlayerAccessRestrictionService(session_factory)
    restriction_service.restrict_player_access(
        player.id,
        PlayerAccessRestrictionType.QUEUE_JOIN,
        PlayerAccessRestrictionDuration.PERMANENT,
        admin_discord_user_id=10,
    )
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.dev_join(
            as_interaction(interaction),
            DEFAULT_MATCH_FORMAT.value,
            DEFAULT_QUEUE_NAME,
            str(target_discord_user_id),
        )
    )

    assert interaction.response.messages == ["指定したユーザーは現在キュー参加を制限されています。"]


def test_dev_present_returns_expired_message_for_expired_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_687
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)
    queue_entry = get_queue_entry(session, player.id)
    queue_entry.expire_at = queue_entry.joined_at - timedelta(seconds=1)
    session.commit()

    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_present(as_interaction(interaction), str(target_discord_user_id)))

    assert interaction.response.messages == [
        "指定したユーザーは期限切れのためキューから外れました。"
    ]


def test_dev_leave_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_leave(as_interaction(interaction), "123456789012345688"))

    assert interaction.response.messages == ["指定したユーザーは未登録です。"]


def test_dev_player_info_requires_admin(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    create_player(session, 123_456_789_012_345_689)
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345689"))

    assert interaction.response.messages == ["このコマンドは管理者のみ実行できます。"]


def test_dev_player_info_validates_discord_user_id(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "not-a-number"))

    assert interaction.response.messages == ["discord_user_id が不正です。"]


def test_dev_player_info_returns_target_player_stats(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_690
    player = create_player(session, target_discord_user_id)
    three_vs_three_stats = get_player_format_stats(session, player.id)
    three_vs_three_stats.rating = 1498.25
    three_vs_three_stats.games_played = 3
    three_vs_three_stats.wins = 1
    three_vs_three_stats.losses = 1
    three_vs_three_stats.draws = 1
    three_vs_three_stats.last_played_at = datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc)
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert interaction.response.messages == [
        format_player_info_message(
            {
                MatchFormat.ONE_VS_ONE: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.TWO_VS_TWO: (1500.0, 0, 0, 0, 0, None),
                MatchFormat.THREE_VS_THREE: (
                    1498.25,
                    3,
                    1,
                    1,
                    1,
                    datetime(2026, 3, 20, 14, 0, 0, tzinfo=timezone.utc),
                ),
            }
        )
    ]


def test_dev_player_info_returns_target_not_registered_message(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), "123456789012345691"))

    assert interaction.response.messages == ["指定したユーザーは未登録です。"]


def test_dev_is_admin_returns_yes_or_no(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(session_factory, super_admin_user_ids=frozenset({10}))
    admin_interaction = FakeInteraction(user=FakeUser(id=10))
    non_admin_interaction = FakeInteraction(user=FakeUser(id=20))

    asyncio.run(handlers.dev_is_admin(as_interaction(admin_interaction)))
    asyncio.run(handlers.dev_is_admin(as_interaction(non_admin_interaction)))

    assert admin_interaction.response.messages == ["はい"]
    assert non_admin_interaction.response.messages == ["いいえ"]


def test_admin_restrict_and_unrestrict_user_commands_manage_restrictions(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_695
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(player.id, DEFAULT_MATCH_FORMAT, DEFAULT_QUEUE_NAME)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=matching_queue_service,
    )
    restrict_interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))
    unrestrict_interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(restrict_interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            PlayerAccessRestrictionDuration.SEVEN_DAYS.value,
            target_user=FakeUser(
                id=target_discord_user_id,
                name="target-user",
                global_name="target-global",
                nick="target-guild",
            ),
            reason="test reason",
        )
    )
    asyncio.run(
        handlers.admin_unrestrict_user(
            as_interaction(unrestrict_interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            target_user=FakeUser(
                id=target_discord_user_id,
                name="target-user",
                global_name="target-global",
                nick="target-guild",
            ),
        )
    )

    session.expire_all()
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == player.id)
    )
    persisted_player = session.scalar(select(Player).where(Player.id == player.id))
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player.id)
    )

    assert restrict_interaction.response.messages == [
        "指定したユーザーのキュー参加を7日制限しました。"
    ]
    assert unrestrict_interaction.response.messages == [
        "指定したユーザーのキュー参加制限を解除しました。"
    ]
    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.QUEUE_JOIN
    assert restriction.reason == "test reason"
    assert restriction.revoked_at is not None
    assert persisted_player is not None
    assert persisted_player.display_name == "target-guild"
    assert persisted_player.display_name_updated_at is not None
    assert persisted_player.last_seen_at == persisted_player.display_name_updated_at
    assert queue_entry is not None
    assert queue_entry.status == MatchQueueEntryStatus.WAITING


def test_admin_restrict_user_accepts_dummy_user_reference(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 777
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(interaction),
            PlayerAccessRestrictionType.SPECTATE.value,
            PlayerAccessRestrictionDuration.ONE_DAY.value,
            dummy_user=f"<dummy_{target_discord_user_id}>",
        )
    )

    session.expire_all()
    restriction = session.scalar(
        select(PlayerAccessRestriction).where(PlayerAccessRestriction.player_id == player.id)
    )

    assert interaction.response.messages == ["指定したユーザーの観戦を1日制限しました。"]
    assert restriction is not None
    assert restriction.restriction_type == PlayerAccessRestrictionType.SPECTATE


def test_admin_restrict_user_requires_target_selection(
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({10}),
    )
    interaction = FakeInteraction(user=FakeUser(id=10))

    asyncio.run(
        handlers.admin_restrict_user(
            as_interaction(interaction),
            PlayerAccessRestrictionType.QUEUE_JOIN.value,
            PlayerAccessRestrictionDuration.SEVEN_DAYS.value,
        )
    )

    assert interaction.response.messages == ["対象ユーザーの指定が不正です。"]


def test_admin_add_penalty_accepts_dummy_user_reference(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 778
    player = create_player(session, target_discord_user_id)
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(
        handlers.admin_add_penalty(
            as_interaction(interaction),
            PenaltyType.LATE,
            dummy_user=f"<dummy_{target_discord_user_id}>",
        )
    )

    session.expire_all()
    penalty = session.get(
        PlayerPenalty,
        {"player_id": player.id, "penalty_type": PenaltyType.LATE},
    )

    assert interaction.response.messages == ["ペナルティを加算しました。"]
    assert penalty is not None
    assert penalty.count == 1


def test_dev_commands_register_discord_user_id_as_last_option() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    expected_parameters = {
        "dev_register": ["discord_user_id"],
        "dev_join": ["match_format", "queue_name", "discord_user_id"],
        "dev_present": ["discord_user_id"],
        "dev_leave": ["discord_user_id"],
        "dev_player_info": ["discord_user_id"],
        "dev_match_parent": ["match_id", "discord_user_id"],
        "dev_match_spectate": ["match_id", "discord_user_id"],
        "dev_match_win": ["match_id", "discord_user_id"],
        "dev_match_lose": ["match_id", "discord_user_id"],
        "dev_match_draw": ["match_id", "discord_user_id"],
        "dev_match_void": ["match_id", "discord_user_id"],
        "dev_match_approve": ["match_id", "discord_user_id"],
    }

    for command_name, expected in expected_parameters.items():
        command = tree.get_command(command_name)
        assert command is not None
        assert [parameter.name for parameter in command.parameters] == expected


def test_admin_restriction_commands_are_registered_with_expected_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    restrict_command = tree.get_command("admin_restrict_user")
    unrestrict_command = tree.get_command("admin_unrestrict_user")

    assert restrict_command is not None
    assert unrestrict_command is not None
    assert [parameter.name for parameter in restrict_command.parameters] == [
        "restriction_type",
        "duration",
        "user",
        "dummy_user",
        "reason",
    ]
    assert [parameter.name for parameter in unrestrict_command.parameters] == [
        "restriction_type",
        "user",
        "dummy_user",
    ]


def test_admin_penalty_commands_are_registered_with_expected_parameters() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    add_command = tree.get_command("admin_add_late")
    sub_command = tree.get_command("admin_sub_late")

    assert add_command is not None
    assert sub_command is not None
    assert [parameter.name for parameter in add_command.parameters] == ["user", "dummy_user"]
    assert [parameter.name for parameter in sub_command.parameters] == ["user", "dummy_user"]


def test_match_spectate_command_is_registered_with_match_id_parameter() -> None:
    handlers = BotCommandHandlers(
        settings=create_settings(),
        session_factory=sessionmaker(),
    )
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)

    register_app_commands(tree, handlers)

    command = tree.get_command("match_spectate")
    assert command is not None
    assert [parameter.name for parameter in command.parameters] == ["match_id"]
