from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bot.commands import BotCommandHandlers
from bot.config import Settings
from bot.models import MatchQueueEntry, Player
from bot.runtime import MatchRuntime
from bot.services import MatchingQueueNotificationContext, MatchingQueueService, register_player

DEFAULT_QUEUE_NAME = "low"


@dataclass(frozen=True)
class FakeUser:
    id: int


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
        resolved_matching_queue_service = MatchRuntime(service=matching_queue_service)

    return BotCommandHandlers(
        settings=create_settings(super_admin_user_ids=super_admin_user_ids),
        session_factory=session_factory,
        matching_queue_service=resolved_matching_queue_service,
    )


def create_player(session: Session, discord_user_id: int) -> Player:
    player = register_player(session=session, discord_user_id=discord_user_id)
    session.commit()
    return player


def get_queue_entry(session: Session, player_id: int) -> MatchQueueEntry:
    session.expire_all()
    queue_entry = session.scalar(
        select(MatchQueueEntry).where(MatchQueueEntry.player_id == player_id)
    )
    assert queue_entry is not None
    return queue_entry


def test_register_command_registers_requesting_user(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_678))

    asyncio.run(handlers.register(as_interaction(interaction)))

    persisted_player = session.scalar(
        select(Player).where(Player.discord_user_id == interaction.user.id)
    )

    assert interaction.response.messages == ["登録が完了しました。"]
    assert persisted_player is not None


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
        user=FakeUser(id=discord_user_id),
        channel_id=3_001,
        guild_id=4_001,
    )

    asyncio.run(handlers.join(as_interaction(interaction), DEFAULT_QUEUE_NAME))

    queue_entry = get_queue_entry(session, player.id)

    assert interaction.response.messages == ["キューに参加しました。5分間マッチングします。"]
    assert queue_entry.notification_channel_id == 3_001
    assert queue_entry.notification_guild_id == 4_001
    assert queue_entry.notification_mention_discord_user_id == discord_user_id


def test_join_command_requires_registered_player(session_factory: sessionmaker[Session]) -> None:
    handlers = create_handlers(
        session_factory,
        matching_queue_service=MatchingQueueService(session_factory),
    )
    interaction = FakeInteraction(user=FakeUser(id=123_456_789_012_345_681))

    asyncio.run(handlers.join(as_interaction(interaction), DEFAULT_QUEUE_NAME))

    assert interaction.response.messages == [
        "プレイヤー登録が必要です。先に /register を実行してください。"
    ]


def test_present_command_updates_waiting_entry_and_notification_context(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    discord_user_id = 123_456_789_012_345_682
    player = create_player(session, discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(
        player.id,
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
    player.rating = 1512.5
    player.games_played = 8
    player.wins = 5
    player.losses = 2
    player.draws = 1
    session.commit()
    handlers = create_handlers(session_factory)
    interaction = FakeInteraction(user=FakeUser(id=discord_user_id))

    asyncio.run(handlers.player_info(as_interaction(interaction)))

    assert interaction.response.messages == [
        "プレイヤー情報\nrating: 1512.50\ngames_played: 8\nwins: 5\nlosses: 2\ndraws: 1"
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
            str(target_discord_user_id),
            DEFAULT_QUEUE_NAME,
        )
    )

    queue_entry = get_queue_entry(session, player.id)

    assert interaction.response.messages == ["指定したユーザーをキューに参加させました。"]
    assert queue_entry.notification_channel_id == 11_001
    assert queue_entry.notification_guild_id == 12_001
    assert queue_entry.notification_mention_discord_user_id == target_discord_user_id


def test_dev_present_returns_expired_message_for_expired_target(
    session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    executor_discord_user_id = 10
    target_discord_user_id = 123_456_789_012_345_687
    player = create_player(session, target_discord_user_id)
    matching_queue_service = MatchingQueueService(session_factory)
    matching_queue_service.join_queue(player.id, DEFAULT_QUEUE_NAME)
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
    player.rating = 1498.25
    player.games_played = 3
    player.wins = 1
    player.losses = 1
    player.draws = 1
    session.commit()
    handlers = create_handlers(
        session_factory,
        super_admin_user_ids=frozenset({executor_discord_user_id}),
    )
    interaction = FakeInteraction(user=FakeUser(id=executor_discord_user_id))

    asyncio.run(handlers.dev_player_info(as_interaction(interaction), str(target_discord_user_id)))

    assert interaction.response.messages == [
        "プレイヤー情報\nrating: 1498.25\ngames_played: 3\nwins: 1\nlosses: 1\ndraws: 1"
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
