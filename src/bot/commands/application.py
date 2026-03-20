from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, cast

import discord
from discord import app_commands
from sqlalchemy.orm import Session, sessionmaker

from bot.config import Settings
from bot.constants import MATCH_QUEUE_NAME_CHOICES, is_dummy_discord_user_id
from bot.db.session import session_scope
from bot.models import MatchReportInputResult, MatchResult, PenaltyType
from bot.services import (
    InvalidQueueNameError,
    JoinQueueResult,
    LeaveQueueResult,
    MatchFlowError,
    MatchingQueueNotificationContext,
    MatchReportSubmissionResult,
    PlayerAlreadyRegisteredError,
    PlayerInfo,
    PlayerLookupService,
    PlayerNotRegisteredError,
    PlayerPenaltyAdjustmentResult,
    PresentQueueResult,
    QueueAlreadyJoinedError,
    QueueJoinNotAllowedError,
    QueueNotJoinedError,
    register_player,
)

REGISTER_SUCCESS_MESSAGE = "登録が完了しました。"
REGISTER_ALREADY_REGISTERED_MESSAGE = "すでに登録済みです。"
REGISTER_FAILED_MESSAGE = "登録に失敗しました。管理者に確認してください。"

PLAYER_REGISTRATION_REQUIRED_MESSAGE = (
    "プレイヤー登録が必要です。先に /register を実行してください。"
)
INVALID_QUEUE_NAME_MESSAGE = "指定したキューは存在しません。"
QUEUE_JOIN_NOT_ALLOWED_MESSAGE = "現在のレーティングではそのキューに参加できません。"
JOIN_ALREADY_JOINED_MESSAGE = "すでにキュー参加中です。"
PRESENT_NOT_JOINED_MESSAGE = "キューに参加していません。"
JOIN_FAILED_MESSAGE = "キュー参加に失敗しました。管理者に確認してください。"
PRESENT_FAILED_MESSAGE = "在席更新に失敗しました。管理者に確認してください。"
LEAVE_FAILED_MESSAGE = "キュー退出に失敗しました。管理者に確認してください。"
PLAYER_INFO_FAILED_MESSAGE = "プレイヤー情報の取得に失敗しました。管理者に確認してください。"

MATCH_PARENT_SUCCESS_MESSAGE = "親に立候補しました。"
MATCH_REPORT_SUCCESS_MESSAGE = "勝敗報告を受け付けました。"
MATCH_APPROVE_SUCCESS_MESSAGE = "仮決定結果を承認しました。"
MATCH_ACTION_FAILED_MESSAGE = "試合操作に失敗しました。管理者に確認してください。"

ADMIN_ONLY_MESSAGE = "このコマンドは管理者のみ実行できます。"
INVALID_DISCORD_USER_ID_MESSAGE = "discord_user_id が不正です。"
ADMIN_MATCH_RESULT_SUCCESS_MESSAGE = "試合結果を上書きしました。"
ADMIN_MATCH_RESULT_FAILED_MESSAGE = "試合結果の上書きに失敗しました。管理者に確認してください。"
ADMIN_TARGET_NOT_REGISTERED_MESSAGE = "指定したユーザーは未登録です。"
ADMIN_PENALTY_ADD_SUCCESS_MESSAGE = "ペナルティを加算しました。"
ADMIN_PENALTY_SUB_SUCCESS_MESSAGE = "ペナルティを減算しました。"
ADMIN_PENALTY_FAILED_MESSAGE = "ペナルティ操作に失敗しました。管理者に確認してください。"

DEV_REGISTER_SUCCESS_MESSAGE = "ダミーユーザーを登録しました。"
DEV_REGISTER_ALREADY_REGISTERED_MESSAGE = "指定したユーザーはすでに登録済みです。"
DEV_REGISTER_FAILED_MESSAGE = "ダミーユーザーの登録に失敗しました。管理者に確認してください。"

DEV_TARGET_NOT_REGISTERED_MESSAGE = "指定したユーザーは未登録です。"
DEV_JOIN_SUCCESS_MESSAGE = "指定したユーザーをキューに参加させました。"
DEV_INVALID_QUEUE_NAME_MESSAGE = "指定したキューは存在しません。"
DEV_JOIN_ALREADY_JOINED_MESSAGE = "指定したユーザーはすでにキュー参加中です。"
DEV_JOIN_NOT_ALLOWED_MESSAGE = (
    "指定したユーザーは現在のレーティングではそのキューに参加できません。"
)
DEV_JOIN_FAILED_MESSAGE = "ダミーユーザーのキュー参加に失敗しました。管理者に確認してください。"

DEV_PRESENT_SUCCESS_MESSAGE = "指定したユーザーの在席を更新しました。"
DEV_PRESENT_NOT_JOINED_MESSAGE = "指定したユーザーはキューに参加していません。"
DEV_PRESENT_EXPIRED_MESSAGE = "指定したユーザーは期限切れのためキューから外れました。"
DEV_PRESENT_FAILED_MESSAGE = "ダミーユーザーの在席更新に失敗しました。管理者に確認してください。"

DEV_LEAVE_SUCCESS_MESSAGE = "指定したユーザーをキューから退出させました。"
DEV_LEAVE_EXPIRED_MESSAGE = "指定したユーザーはすでに期限切れでキューから外れています。"
DEV_LEAVE_FAILED_MESSAGE = "ダミーユーザーのキュー退出に失敗しました。管理者に確認してください。"
DEV_PLAYER_INFO_FAILED_MESSAGE = (
    "指定したユーザーのプレイヤー情報取得に失敗しました。管理者に確認してください。"
)

DEV_MATCH_PARENT_SUCCESS_MESSAGE = "指定したユーザーを親に立候補させました。"
DEV_MATCH_REPORT_SUCCESS_MESSAGE = "指定したユーザーの勝敗報告を受け付けました。"
DEV_MATCH_APPROVE_SUCCESS_MESSAGE = "指定したユーザーが仮決定結果を承認しました。"
DEV_MATCH_ACTION_FAILED_MESSAGE = (
    "ダミーユーザーの試合操作に失敗しました。管理者に確認してください。"
)

DEV_IS_ADMIN_ERROR_MESSAGE = "エラーが発生しました。管理者に確認してください。"


def is_super_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.super_admin_user_ids


class MatchingQueueCommandService(Protocol):
    async def join_queue(
        self,
        player_id: int,
        queue_name: str,
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


class MatchCommandService(Protocol):
    async def volunteer_parent(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> object: ...

    async def submit_match_report(
        self,
        match_id: int,
        player_id: int,
        input_result: MatchReportInputResult,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> MatchReportSubmissionResult: ...

    async def approve_match_result(
        self,
        match_id: int,
        player_id: int,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
    ) -> object: ...

    async def admin_override_match_result(
        self,
        match_id: int,
        final_result: MatchResult,
        *,
        admin_discord_user_id: int,
    ) -> object: ...

    async def adjust_penalty(
        self,
        player_id: int,
        penalty_type: PenaltyType,
        delta: int,
        *,
        admin_discord_user_id: int,
    ) -> PlayerPenaltyAdjustmentResult: ...


class BotCommandHandlers:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        matching_queue_service: MatchingQueueCommandService | None = None,
        match_service: MatchCommandService | None = None,
        player_lookup_service: PlayerLookupService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._matching_queue_service = matching_queue_service
        self._match_service = match_service
        if (
            self._match_service is None
            and matching_queue_service is not None
            and hasattr(
                matching_queue_service,
                "volunteer_parent",
            )
        ):
            self._match_service = cast(MatchCommandService, matching_queue_service)
        self.player_lookup_service = player_lookup_service or PlayerLookupService(session_factory)
        self.logger = logger or logging.getLogger(__name__)

    @property
    def matching_queue_service(self) -> MatchingQueueCommandService | None:
        return self._matching_queue_service

    @matching_queue_service.setter
    def matching_queue_service(self, service: MatchingQueueCommandService | None) -> None:
        self._matching_queue_service = service
        if service is not None and hasattr(service, "volunteer_parent"):
            self._match_service = cast(MatchCommandService, service)

    @property
    def match_service(self) -> MatchCommandService | None:
        return self._match_service

    @match_service.setter
    def match_service(self, service: MatchCommandService | None) -> None:
        self._match_service = service

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

    async def join(self, interaction: discord.Interaction[Any], queue_name: str) -> None:
        try:
            notification_context = self._build_notification_context(interaction)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()
            result = await service.join_queue(
                player_id,
                queue_name,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except InvalidQueueNameError:
            await self._send_message(interaction, INVALID_QUEUE_NAME_MESSAGE)
            return
        except QueueJoinNotAllowedError:
            await self._send_message(interaction, QUEUE_JOIN_NOT_ALLOWED_MESSAGE)
            return
        except QueueAlreadyJoinedError:
            await self._send_message(interaction, JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /join command discord_user_id=%s queue_name=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                queue_name,
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

    async def player_info(self, interaction: discord.Interaction[Any]) -> None:
        try:
            player_info = await asyncio.to_thread(
                self._lookup_player_info,
                interaction.user.id,
            )
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /player_info command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(interaction, PLAYER_INFO_FAILED_MESSAGE)
            return

        await self._send_message(interaction, self._format_player_info_message(player_info))

    async def match_parent(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._run_match_parent(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def match_win(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.WIN,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def match_lose(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.LOSE,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def match_draw(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.DRAW,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def match_void(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.VOID,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def match_approve(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        try:
            notification_context = self._build_notification_context(interaction)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_match_service()
            await service.approve_match_result(
                match_id,
                player_id,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            await self._send_message(interaction, PLAYER_REGISTRATION_REQUIRED_MESSAGE)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /match_approve command discord_user_id=%s match_id=%s",
                interaction.user.id,
                match_id,
            )
            await self._send_message(interaction, MATCH_ACTION_FAILED_MESSAGE)
            return

        await self._send_message(interaction, MATCH_APPROVE_SUCCESS_MESSAGE)

    async def admin_match_result(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        result: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            service = self._require_match_service()
            await service.admin_override_match_result(
                match_id,
                self._parse_match_result(result),
                admin_discord_user_id=interaction.user.id,
            )
        except ValueError:
            await self._send_message(interaction, "result が不正です。")
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_match_result command "
                "executor_discord_user_id=%s match_id=%s result=%s",
                interaction.user.id,
                match_id,
                result,
            )
            await self._send_message(interaction, ADMIN_MATCH_RESULT_FAILED_MESSAGE)
            return

        await self._send_message(interaction, ADMIN_MATCH_RESULT_SUCCESS_MESSAGE)

    async def admin_add_penalty(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        penalty_type: PenaltyType,
    ) -> None:
        await self._run_admin_penalty(
            interaction=interaction,
            discord_user_id=discord_user_id,
            penalty_type=penalty_type,
            delta=1,
            success_message=ADMIN_PENALTY_ADD_SUCCESS_MESSAGE,
        )

    async def admin_sub_penalty(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        penalty_type: PenaltyType,
    ) -> None:
        await self._run_admin_penalty(
            interaction=interaction,
            discord_user_id=discord_user_id,
            penalty_type=penalty_type,
            delta=-1,
            success_message=ADMIN_PENALTY_SUB_SUCCESS_MESSAGE,
        )

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
        queue_name: str,
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
                queue_name,
                notification_context=notification_context,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except InvalidQueueNameError:
            await self._send_message(interaction, DEV_INVALID_QUEUE_NAME_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except QueueJoinNotAllowedError:
            await self._send_message(interaction, DEV_JOIN_NOT_ALLOWED_MESSAGE)
            return
        except QueueAlreadyJoinedError:
            await self._send_message(interaction, DEV_JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_join command "
                "executor_discord_user_id=%s target_discord_user_id=%s queue_name=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                discord_user_id,
                queue_name,
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

    async def dev_player_info(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            player_info = await asyncio.to_thread(
                self._lookup_player_info,
                target_discord_user_id,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_player_info command "
                "executor_discord_user_id=%s target_discord_user_id=%s",
                interaction.user.id,
                discord_user_id,
            )
            await self._send_message(interaction, DEV_PLAYER_INFO_FAILED_MESSAGE)
            return

        await self._send_message(interaction, self._format_player_info_message(player_info))

    async def dev_match_parent(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return
        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        await self._run_match_parent(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=target_discord_user_id,
            success_message=DEV_MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_ACTION_FAILED_MESSAGE,
        )

    async def dev_match_win(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInputResult.WIN,
        )

    async def dev_match_lose(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInputResult.LOSE,
        )

    async def dev_match_draw(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInputResult.DRAW,
        )

    async def dev_match_void(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInputResult.VOID,
        )

    async def dev_match_approve(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=target_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_match_service()
            await service.approve_match_result(
                match_id,
                player_id,
                notification_context=notification_context,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_match_approve command "
                "executor_discord_user_id=%s target_discord_user_id=%s match_id=%s",
                interaction.user.id,
                discord_user_id,
                match_id,
            )
            await self._send_message(interaction, DEV_MATCH_ACTION_FAILED_MESSAGE)
            return

        await self._send_message(interaction, DEV_MATCH_APPROVE_SUCCESS_MESSAGE)

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

    async def _run_match_parent(
        self,
        *,
        interaction: discord.Interaction[Any],
        match_id: int,
        executor_discord_user_id: int | None,
        success_message: str,
        failure_message: str,
    ) -> None:
        if executor_discord_user_id is None:
            return

        try:
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=executor_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, executor_discord_user_id)
            service = self._require_match_service()
            await service.volunteer_parent(
                match_id,
                player_id,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            message = (
                PLAYER_REGISTRATION_REQUIRED_MESSAGE
                if executor_discord_user_id == interaction.user.id
                else DEV_TARGET_NOT_REGISTERED_MESSAGE
            )
            await self._send_message(interaction, message)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_parent command executor_discord_user_id=%s match_id=%s",
                executor_discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message)
            return

        await self._send_message(interaction, success_message)

    async def _run_match_report(
        self,
        *,
        interaction: discord.Interaction[Any],
        match_id: int,
        executor_discord_user_id: int,
        input_result: MatchReportInputResult,
        success_message: str,
        failure_message: str,
    ) -> None:
        try:
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=executor_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, executor_discord_user_id)
            service = self._require_match_service()
            await service.submit_match_report(
                match_id,
                player_id,
                input_result,
                notification_context=notification_context,
            )
        except PlayerNotRegisteredError:
            message = (
                PLAYER_REGISTRATION_REQUIRED_MESSAGE
                if executor_discord_user_id == interaction.user.id
                else DEV_TARGET_NOT_REGISTERED_MESSAGE
            )
            await self._send_message(interaction, message)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_report command "
                "executor_discord_user_id=%s match_id=%s input_result=%s",
                executor_discord_user_id,
                match_id,
                input_result.value,
            )
            await self._send_message(interaction, failure_message)
            return

        await self._send_message(interaction, success_message)

    async def _run_dev_match_report(
        self,
        *,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
        input_result: MatchReportInputResult,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return

        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=target_discord_user_id,
            input_result=input_result,
            success_message=DEV_MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_ACTION_FAILED_MESSAGE,
        )

    async def _run_admin_penalty(
        self,
        *,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        penalty_type: PenaltyType,
        delta: int,
        success_message: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_match_service()
            await service.adjust_penalty(
                player_id,
                penalty_type,
                delta,
                admin_discord_user_id=interaction.user.id,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, ADMIN_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute admin penalty command executor_discord_user_id=%s "
                "target_discord_user_id=%s penalty_type=%s delta=%s",
                interaction.user.id,
                discord_user_id,
                penalty_type.value,
                delta,
            )
            await self._send_message(interaction, ADMIN_PENALTY_FAILED_MESSAGE)
            return

        await self._send_message(interaction, success_message)

    def _register_player(self, discord_user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            register_player(session=session, discord_user_id=discord_user_id)

    def _lookup_player_id(self, discord_user_id: int) -> int:
        return self.player_lookup_service.get_player_id_by_discord_user_id(discord_user_id)

    def _lookup_player_info(self, discord_user_id: int) -> PlayerInfo:
        return self.player_lookup_service.get_player_info_by_discord_user_id(discord_user_id)

    def _require_matching_queue_service(self) -> MatchingQueueCommandService:
        if self._matching_queue_service is None:
            raise RuntimeError("MatchingQueueService is not configured")
        return self._matching_queue_service

    def _require_match_service(self) -> MatchCommandService:
        if self._match_service is None:
            raise RuntimeError("MatchService is not configured")
        return self._match_service

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

    def _parse_match_result(self, value: str) -> MatchResult:
        return MatchResult(value)

    def _format_player_info_message(self, player_info: PlayerInfo) -> str:
        return (
            "プレイヤー情報\n"
            f"rating: {player_info.rating:.2f}\n"
            f"games_played: {player_info.games_played}\n"
            f"wins: {player_info.wins}\n"
            f"losses: {player_info.losses}\n"
            f"draws: {player_info.draws}"
        )

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
    queue_name_choices = [
        app_commands.Choice(name=queue_name, value=queue_name)
        for queue_name in MATCH_QUEUE_NAME_CHOICES
    ]

    @tree.command(name="register", description="プレイヤー登録を行います")
    async def register_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.register(interaction)

    @tree.command(name="join", description="マッチングキューに参加します")
    @app_commands.describe(queue_name="参加したいキュー名")
    @app_commands.choices(queue_name=queue_name_choices)
    async def join_command(interaction: discord.Interaction[Any], queue_name: str) -> None:
        await handlers.join(interaction, queue_name)

    @tree.command(name="present", description="在席を更新して期限を延長します")
    async def present_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.present(interaction)

    @tree.command(name="leave", description="マッチングキューから退出します")
    async def leave_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.leave(interaction)

    @tree.command(name="player_info", description="自分のプレイヤー情報を表示します")
    async def player_info_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.player_info(interaction)

    @tree.command(name="match_parent", description="試合の親に立候補します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_parent_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_parent(interaction, match_id)

    @tree.command(name="match_win", description="自分視点で勝ちを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_win_command(interaction: discord.Interaction[Any], match_id: int) -> None:
        await handlers.match_win(interaction, match_id)

    @tree.command(name="match_lose", description="自分視点で負けを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_lose_command(interaction: discord.Interaction[Any], match_id: int) -> None:
        await handlers.match_lose(interaction, match_id)

    @tree.command(name="match_draw", description="引き分けを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_draw_command(interaction: discord.Interaction[Any], match_id: int) -> None:
        await handlers.match_draw(interaction, match_id)

    @tree.command(name="match_void", description="無効試合を報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_void_command(interaction: discord.Interaction[Any], match_id: int) -> None:
        await handlers.match_void(interaction, match_id)

    @tree.command(name="match_approve", description="仮決定結果を承認します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_approve_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_approve(interaction, match_id)

    @tree.command(name="admin_match_result", description="試合結果を上書きします")
    @app_commands.describe(match_id="対象の match_id", result="上書きする結果")
    @app_commands.choices(
        result=[
            app_commands.Choice(name="チーム A の勝ち", value=MatchResult.TEAM_A_WIN.value),
            app_commands.Choice(name="チーム B の勝ち", value=MatchResult.TEAM_B_WIN.value),
            app_commands.Choice(name="引き分け", value=MatchResult.DRAW.value),
            app_commands.Choice(name="無効試合", value=MatchResult.VOID.value),
        ]
    )
    async def admin_match_result_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        result: str,
    ) -> None:
        await handlers.admin_match_result(interaction, match_id, result)

    def register_penalty_commands(
        *,
        add_name: str,
        sub_name: str,
        description: str,
        penalty_type: PenaltyType,
    ) -> None:
        @tree.command(name=add_name, description=f"{description} を +1 します")
        @app_commands.describe(discord_user_id="対象の Discord user ID")
        async def add_command(
            interaction: discord.Interaction[Any],
            discord_user_id: str,
        ) -> None:
            await handlers.admin_add_penalty(interaction, discord_user_id, penalty_type)

        @tree.command(name=sub_name, description=f"{description} を -1 します")
        @app_commands.describe(discord_user_id="対象の Discord user ID")
        async def sub_command(
            interaction: discord.Interaction[Any],
            discord_user_id: str,
        ) -> None:
            await handlers.admin_sub_penalty(interaction, discord_user_id, penalty_type)

        del add_command, sub_command

    register_penalty_commands(
        add_name="admin_add_incorrect_report",
        sub_name="admin_sub_incorrect_report",
        description="勝敗誤報告ペナルティ",
        penalty_type=PenaltyType.INCORRECT_REPORT,
    )
    register_penalty_commands(
        add_name="admin_add_no_report",
        sub_name="admin_sub_no_report",
        description="勝敗無報告ペナルティ",
        penalty_type=PenaltyType.NO_REPORT,
    )
    register_penalty_commands(
        add_name="admin_add_room_setup_delay",
        sub_name="admin_sub_room_setup_delay",
        description="部屋立て遅延ペナルティ",
        penalty_type=PenaltyType.ROOM_SETUP_DELAY,
    )
    register_penalty_commands(
        add_name="admin_add_match_mistake",
        sub_name="admin_sub_match_mistake",
        description="試合ミスペナルティ",
        penalty_type=PenaltyType.MATCH_MISTAKE,
    )
    register_penalty_commands(
        add_name="admin_add_late",
        sub_name="admin_sub_late",
        description="遅刻ペナルティ",
        penalty_type=PenaltyType.LATE,
    )
    register_penalty_commands(
        add_name="admin_add_disconnect",
        sub_name="admin_sub_disconnect",
        description="切断ペナルティ",
        penalty_type=PenaltyType.DISCONNECT,
    )

    @tree.command(name="dev_register", description="任意の Discord user ID を登録します")
    @app_commands.describe(discord_user_id="登録したい Discord user ID")
    async def dev_register_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_register(interaction, discord_user_id)

    @tree.command(name="dev_join", description="任意の Discord user ID をキュー参加させます")
    @app_commands.describe(
        discord_user_id="キュー参加させたい Discord user ID",
        queue_name="参加させたいキュー名",
    )
    @app_commands.choices(queue_name=queue_name_choices)
    async def dev_join_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        queue_name: str,
    ) -> None:
        await handlers.dev_join(interaction, discord_user_id, queue_name)

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

    @tree.command(
        name="dev_player_info",
        description="任意の Discord user ID のプレイヤー情報を表示します",
    )
    @app_commands.describe(discord_user_id="表示したい Discord user ID")
    async def dev_player_info_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
    ) -> None:
        await handlers.dev_player_info(interaction, discord_user_id)

    @tree.command(name="dev_match_parent", description="ダミーユーザーを親に立候補させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_parent_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_parent(interaction, discord_user_id, match_id)

    @tree.command(name="dev_match_win", description="ダミーユーザーに勝ちを報告させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_win_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_win(interaction, discord_user_id, match_id)

    @tree.command(name="dev_match_lose", description="ダミーユーザーに負けを報告させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_lose_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_lose(interaction, discord_user_id, match_id)

    @tree.command(name="dev_match_draw", description="ダミーユーザーに引き分けを報告させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_draw_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_draw(interaction, discord_user_id, match_id)

    @tree.command(name="dev_match_void", description="ダミーユーザーに無効試合を報告させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_void_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_void(interaction, discord_user_id, match_id)

    @tree.command(
        name="dev_match_approve",
        description="ダミーユーザーに仮決定結果を承認させます",
    )
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_approve_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_approve(interaction, discord_user_id, match_id)

    @tree.command(name="dev_is_admin", description="実行者が admin かどうかを確認します")
    async def dev_is_admin_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.dev_is_admin(interaction)
