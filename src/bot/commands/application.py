from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import discord
from discord import app_commands
from sqlalchemy.orm import Session, sessionmaker

from bot.config import Settings
from bot.constants import is_dummy_discord_user_id
from bot.db.session import session_scope
from bot.services import (
    JoinQueueResult,
    LeaveQueueResult,
    MatchingQueueNotificationContext,
    PlayerAlreadyRegisteredError,
    PlayerLookupService,
    PlayerNotRegisteredError,
    PresentQueueResult,
    QueueAlreadyJoinedError,
    QueueNotJoinedError,
    register_player,
)

REGISTER_SUCCESS_MESSAGE = "登録が完了しました。"
REGISTER_ALREADY_REGISTERED_MESSAGE = "すでに登録済みです。"
REGISTER_FAILED_MESSAGE = "登録に失敗しました。管理者に確認してください。"

PLAYER_REGISTRATION_REQUIRED_MESSAGE = (
    "プレイヤー登録が必要です。先に /register を実行してください。"
)
JOIN_ALREADY_JOINED_MESSAGE = "すでにキュー参加中です。"
PRESENT_NOT_JOINED_MESSAGE = "キューに参加していません。"
JOIN_FAILED_MESSAGE = "キュー参加に失敗しました。管理者に確認してください。"
PRESENT_FAILED_MESSAGE = "在席更新に失敗しました。管理者に確認してください。"
LEAVE_FAILED_MESSAGE = "キュー退出に失敗しました。管理者に確認してください。"

ADMIN_ONLY_MESSAGE = "このコマンドは管理者のみ実行できます。"
INVALID_DISCORD_USER_ID_MESSAGE = "discord_user_id が不正です。"

DEV_REGISTER_SUCCESS_MESSAGE = "ダミーユーザーを登録しました。"
DEV_REGISTER_ALREADY_REGISTERED_MESSAGE = "指定したユーザーはすでに登録済みです。"
DEV_REGISTER_FAILED_MESSAGE = "ダミーユーザーの登録に失敗しました。管理者に確認してください。"

DEV_TARGET_NOT_REGISTERED_MESSAGE = "指定したユーザーは未登録です。"
DEV_JOIN_SUCCESS_MESSAGE = "指定したユーザーをキューに参加させました。"
DEV_JOIN_ALREADY_JOINED_MESSAGE = "指定したユーザーはすでにキュー参加中です。"
DEV_JOIN_FAILED_MESSAGE = "ダミーユーザーのキュー参加に失敗しました。管理者に確認してください。"

DEV_PRESENT_SUCCESS_MESSAGE = "指定したユーザーの在席を更新しました。"
DEV_PRESENT_NOT_JOINED_MESSAGE = "指定したユーザーはキューに参加していません。"
DEV_PRESENT_EXPIRED_MESSAGE = "指定したユーザーは期限切れのためキューから外れました。"
DEV_PRESENT_FAILED_MESSAGE = "ダミーユーザーの在席更新に失敗しました。管理者に確認してください。"

DEV_LEAVE_SUCCESS_MESSAGE = "指定したユーザーをキューから退出させました。"
DEV_LEAVE_EXPIRED_MESSAGE = "指定したユーザーはすでに期限切れでキューから外れています。"
DEV_LEAVE_FAILED_MESSAGE = "ダミーユーザーのキュー退出に失敗しました。管理者に確認してください。"

DEV_IS_ADMIN_ERROR_MESSAGE = "エラーが発生しました。管理者に確認してください。"


def is_super_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.super_admin_user_ids


class MatchingQueueCommandService(Protocol):
    async def join_queue(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> JoinQueueResult: ...

    async def present(
        self,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> PresentQueueResult: ...

    async def leave(self, player_id: int) -> LeaveQueueResult: ...


class BotCommandHandlers:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        matching_queue_service: MatchingQueueCommandService | None = None,
        player_lookup_service: PlayerLookupService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._matching_queue_service = matching_queue_service
        self.player_lookup_service = player_lookup_service or PlayerLookupService(session_factory)
        self.logger = logger or logging.getLogger(__name__)

    @property
    def matching_queue_service(self) -> MatchingQueueCommandService | None:
        return self._matching_queue_service

    @matching_queue_service.setter
    def matching_queue_service(self, service: MatchingQueueCommandService | None) -> None:
        self._matching_queue_service = service

    async def register(self, interaction: discord.Interaction[Any]) -> None:
        try:
            await asyncio.to_thread(self._register_player, interaction.user.id)
        except PlayerAlreadyRegisteredError:
            await self._send_message(interaction, REGISTER_ALREADY_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /register command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(interaction, REGISTER_FAILED_MESSAGE)
            return

        await self._send_message(interaction, REGISTER_SUCCESS_MESSAGE)

    async def join(self, interaction: discord.Interaction[Any]) -> None:
        try:
            notification_context = self._build_notification_context(interaction)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()
            result = await service.join_queue(
                player_id,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except QueueAlreadyJoinedError:
            await self._send_message(interaction, JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /join command discord_user_id=%s channel_id=%s guild_id=%s",
                interaction.user.id,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_message(interaction, JOIN_FAILED_MESSAGE)
            return

        await self._send_message(interaction, result.message)

    async def present(self, interaction: discord.Interaction[Any]) -> None:
        try:
            notification_context = self._build_notification_context(interaction)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()
            result = await service.present(
                player_id,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except QueueNotJoinedError:
            await self._send_message(interaction, PRESENT_NOT_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /present command discord_user_id=%s channel_id=%s guild_id=%s",
                interaction.user.id,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_message(interaction, PRESENT_FAILED_MESSAGE)
            return

        await self._send_message(interaction, result.message)

    async def leave(self, interaction: discord.Interaction[Any]) -> None:
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()
            result = await service.leave(player_id)
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /leave command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(interaction, LEAVE_FAILED_MESSAGE)
            return

        await self._send_message(interaction, result.message)

    async def dev_register(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
            await asyncio.to_thread(self._register_player, target_discord_user_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerAlreadyRegisteredError:
            await self._send_message(interaction, DEV_REGISTER_ALREADY_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_register command "
                "executor_discord_user_id=%s target_discord_user_id=%s",
                interaction.user.id,
                discord_user_id,
            )
            await self._send_message(interaction, DEV_REGISTER_FAILED_MESSAGE)
            return

        await self._send_message(interaction, DEV_REGISTER_SUCCESS_MESSAGE)

    async def dev_join(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=target_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_matching_queue_service()
            await service.join_queue(
                player_id,
                notification_context=notification_context,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except QueueAlreadyJoinedError:
            await self._send_message(interaction, DEV_JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_join command "
                "executor_discord_user_id=%s target_discord_user_id=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                discord_user_id,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_message(interaction, DEV_JOIN_FAILED_MESSAGE)
            return

        await self._send_message(interaction, DEV_JOIN_SUCCESS_MESSAGE)

    async def dev_present(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=target_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_matching_queue_service()
            result = await service.present(
                player_id,
                notification_context=notification_context,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except QueueNotJoinedError:
            await self._send_message(interaction, DEV_PRESENT_NOT_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_present command "
                "executor_discord_user_id=%s target_discord_user_id=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                discord_user_id,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_message(interaction, DEV_PRESENT_FAILED_MESSAGE)
            return

        if result.expired:
            await self._send_message(interaction, DEV_PRESENT_EXPIRED_MESSAGE)
            return

        await self._send_message(interaction, DEV_PRESENT_SUCCESS_MESSAGE)

    async def dev_leave(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_matching_queue_service()
            result = await service.leave(player_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_leave command "
                "executor_discord_user_id=%s target_discord_user_id=%s",
                interaction.user.id,
                discord_user_id,
            )
            await self._send_message(interaction, DEV_LEAVE_FAILED_MESSAGE)
            return

        if result.expired:
            await self._send_message(interaction, DEV_LEAVE_EXPIRED_MESSAGE)
            return

        await self._send_message(interaction, DEV_LEAVE_SUCCESS_MESSAGE)

    async def dev_is_admin(self, interaction: discord.Interaction[Any]) -> None:
        try:
            message = "はい" if is_super_admin(interaction.user.id, self.settings) else "いいえ"
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_is_admin command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(interaction, DEV_IS_ADMIN_ERROR_MESSAGE)
            return

        await self._send_message(interaction, message)

    def _register_player(self, discord_user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            register_player(session=session, discord_user_id=discord_user_id)

    def _lookup_player_id(self, discord_user_id: int) -> int:
        return self.player_lookup_service.get_player_id_by_discord_user_id(discord_user_id)

    def _require_matching_queue_service(self) -> MatchingQueueCommandService:
        if self._matching_queue_service is None:
            raise RuntimeError("MatchingQueueService is not configured")
        return self._matching_queue_service

    def _build_notification_context(
        self,
        interaction: discord.Interaction[Any],
        *,
        mention_discord_user_id: int | None = None,
    ) -> MatchingQueueNotificationContext:
        if interaction.channel_id is None:
            raise ValueError("interaction.channel_id is required")

        return MatchingQueueNotificationContext(
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id,
            mention_discord_user_id=(
                interaction.user.id if mention_discord_user_id is None else mention_discord_user_id
            ),
        )

    async def _ensure_admin(self, interaction: discord.Interaction[Any]) -> bool:
        if is_super_admin(interaction.user.id, self.settings):
            return True

        await self._send_message(interaction, ADMIN_ONLY_MESSAGE)
        return False

    def _parse_discord_user_id(self, value: str) -> int:
        normalized_value = value.strip()
        if not normalized_value.isdigit():
            raise ValueError("discord_user_id must contain only digits")

        discord_user_id = int(normalized_value)
        if discord_user_id <= 0:
            raise ValueError("discord_user_id must be a positive integer")

        return discord_user_id

    def _parse_dummy_discord_user_id(self, value: str) -> int:
        discord_user_id = self._parse_discord_user_id(value)
        if not is_dummy_discord_user_id(discord_user_id):
            raise ValueError("dummy discord_user_id must be between 1 and 1000")
        return discord_user_id

    async def _send_message(
        self,
        interaction: discord.Interaction[Any],
        message: str,
    ) -> None:
        await interaction.response.send_message(message)


def register_app_commands(
    tree: app_commands.CommandTree[Any],
    handlers: BotCommandHandlers,
) -> None:
    @tree.command(name="register", description="プレイヤー登録を行います")
    async def register_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.register(interaction)

    @tree.command(name="join", description="マッチングキューに参加します")
    async def join_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.join(interaction)

    @tree.command(name="present", description="在席を更新して期限を延長します")
    async def present_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.present(interaction)

    @tree.command(name="leave", description="マッチングキューから退出します")
    async def leave_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.leave(interaction)

    @tree.command(name="dev_register", description="任意の Discord user ID を登録します")
    @app_commands.describe(discord_user_id="登録したい Discord user ID")
    async def dev_register_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_register(interaction, discord_user_id)

    @tree.command(name="dev_join", description="任意の Discord user ID をキュー参加させます")
    @app_commands.describe(discord_user_id="キュー参加させたい Discord user ID")
    async def dev_join_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_join(interaction, discord_user_id)

    @tree.command(name="dev_present", description="任意の Discord user ID の在席を更新します")
    @app_commands.describe(discord_user_id="在席を更新したい Discord user ID")
    async def dev_present_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_present(interaction, discord_user_id)

    @tree.command(name="dev_leave", description="任意の Discord user ID をキューから退出させます")
    @app_commands.describe(discord_user_id="キューから退出させたい Discord user ID")
    async def dev_leave_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_leave(interaction, discord_user_id)

    @tree.command(name="dev_is_admin", description="実行者が admin かどうかを確認します")
    async def dev_is_admin_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.dev_is_admin(interaction)
