from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from sqlalchemy.orm import Session, sessionmaker

from bot.config import Settings
from bot.constants import is_dummy_discord_user_id
from bot.db.session import session_scope
from bot.models import MatchReportInput, MatchResultType, PlayerPenaltyType
from bot.services import (
    MatchApprovalNotOpenError,
    MatchApprovalNotRequiredError,
    MatchingQueueNotificationContext,
    MatchingQueueService,
    MatchNotFoundError,
    MatchParticipantError,
    MatchReportClosedError,
    MatchReportNotOpenError,
    MatchService,
    ParentAlreadyDecidedError,
    ParentVolunteerClosedError,
    PlayerAlreadyRegisteredError,
    PlayerLookupService,
    PlayerNotRegisteredError,
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

MATCH_NOT_FOUND_MESSAGE = "指定した試合が見つかりません。"
MATCH_PARTICIPANT_ONLY_MESSAGE = "この試合の参加者のみ実行できます。"
MATCH_PARENT_SUCCESS_MESSAGE = "親に決定しました。"
MATCH_PARENT_ALREADY_DECIDED_MESSAGE = "この試合ではすでに親が決まっています。"
MATCH_PARENT_CLOSED_MESSAGE = "親募集期間は終了しています。"
MATCH_PARENT_FAILED_MESSAGE = "親決定に失敗しました。管理者に確認してください。"

MATCH_REPORT_SUCCESS_MESSAGE = "勝敗報告を受け付けました。"
MATCH_REPORT_NOT_OPEN_MESSAGE = "この試合ではまだその報告を受け付けていません。"
MATCH_REPORT_CLOSED_MESSAGE = "この試合の勝敗報告受付は終了しています。"
MATCH_REPORT_FAILED_MESSAGE = "勝敗報告に失敗しました。管理者に確認してください。"

MATCH_APPROVE_SUCCESS_MESSAGE = "仮決定結果を承認しました。"
MATCH_APPROVE_NOT_OPEN_MESSAGE = "この試合は承認期間中ではありません。"
MATCH_APPROVE_NOT_REQUIRED_MESSAGE = "あなたはこの試合の承認対象ではありません。"
MATCH_APPROVE_FAILED_MESSAGE = "仮決定結果の承認に失敗しました。管理者に確認してください。"

DEV_MATCH_PARENT_SUCCESS_MESSAGE = "指定したダミーユーザーを親に決定しました。"
DEV_MATCH_REPORT_SUCCESS_MESSAGE = "指定したダミーユーザーの勝敗報告を受け付けました。"
DEV_MATCH_APPROVE_SUCCESS_MESSAGE = "指定したダミーユーザーの承認を受け付けました。"
DEV_MATCH_FAILED_MESSAGE = "ダミーユーザーの試合操作に失敗しました。管理者に確認してください。"

ADMIN_MATCH_RESULT_SUCCESS_MESSAGE = "試合結果を上書きしました。"
ADMIN_MATCH_RESULT_FAILED_MESSAGE = "試合結果の上書きに失敗しました。管理者に確認してください。"
ADMIN_PENALTY_UPDATED_MESSAGE = "ペナルティを更新しました。"
ADMIN_PENALTY_FAILED_MESSAGE = "ペナルティ更新に失敗しました。管理者に確認してください。"


def is_super_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.super_admin_user_ids


class BotCommandHandlers:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        *,
        matching_queue_service: MatchingQueueService | None = None,
        match_service: MatchService | None = None,
        player_lookup_service: PlayerLookupService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self._matching_queue_service = matching_queue_service
        self._match_service = match_service
        self.player_lookup_service = player_lookup_service or PlayerLookupService(session_factory)
        self.logger = logger or logging.getLogger(__name__)

    @property
    def matching_queue_service(self) -> MatchingQueueService | None:
        return self._matching_queue_service

    @matching_queue_service.setter
    def matching_queue_service(self, service: MatchingQueueService | None) -> None:
        self._matching_queue_service = service

    @property
    def match_service(self) -> MatchService | None:
        return self._match_service

    @match_service.setter
    def match_service(self, service: MatchService | None) -> None:
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

    async def join(self, interaction: discord.Interaction[Any]) -> None:
        try:
            notification_context = self._build_notification_context(interaction)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()
            result = await asyncio.to_thread(
                service.join_queue,
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
            result = await asyncio.to_thread(
                service.present,
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
            result = await asyncio.to_thread(service.leave, player_id)
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

    async def match_parent(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_parent(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            success_message=MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=MATCH_PARENT_FAILED_MESSAGE,
        )

    async def match_win(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_report(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            input_result=MatchReportInput.WIN,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_REPORT_FAILED_MESSAGE,
        )

    async def match_loss(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_report(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            input_result=MatchReportInput.LOSS,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_REPORT_FAILED_MESSAGE,
        )

    async def match_draw(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_report(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            input_result=MatchReportInput.DRAW,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_REPORT_FAILED_MESSAGE,
        )

    async def match_void(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_report(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            input_result=MatchReportInput.VOID,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_REPORT_FAILED_MESSAGE,
        )

    async def match_approve(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._handle_match_approve(
            interaction,
            discord_user_id=interaction.user.id,
            match_id=match_id,
            success_message=MATCH_APPROVE_SUCCESS_MESSAGE,
            failure_message=MATCH_APPROVE_FAILED_MESSAGE,
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
            await asyncio.to_thread(
                service.join_queue,
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
            result = await asyncio.to_thread(
                service.present,
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
            result = await asyncio.to_thread(service.leave, player_id)
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

        await self._handle_match_parent(
            interaction,
            discord_user_id=target_discord_user_id,
            match_id=match_id,
            success_message=DEV_MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_FAILED_MESSAGE,
        )

    async def dev_match_win(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._handle_dev_match_report(
            interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInput.WIN,
        )

    async def dev_match_loss(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._handle_dev_match_report(
            interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInput.LOSS,
        )

    async def dev_match_draw(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._handle_dev_match_report(
            interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInput.DRAW,
        )

    async def dev_match_void(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await self._handle_dev_match_report(
            interaction,
            discord_user_id=discord_user_id,
            match_id=match_id,
            input_result=MatchReportInput.VOID,
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
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return

        await self._handle_match_approve(
            interaction,
            discord_user_id=target_discord_user_id,
            match_id=match_id,
            success_message=DEV_MATCH_APPROVE_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_FAILED_MESSAGE,
        )

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

    async def admin_match_result(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        result: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            parsed_result = self._parse_match_result_type(result)
            service = self._require_match_service()
            await asyncio.to_thread(service.override_final_result, match_id, parsed_result)
        except ValueError:
            await self._send_message(interaction, "result が不正です。")
            return
        except MatchNotFoundError:
            await self._send_message(interaction, MATCH_NOT_FOUND_MESSAGE)
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
        penalty_type: PlayerPenaltyType,
    ) -> None:
        await self._handle_admin_penalty_adjustment(
            interaction,
            discord_user_id=discord_user_id,
            penalty_type=penalty_type,
            delta=1,
        )

    async def admin_sub_penalty(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        penalty_type: PlayerPenaltyType,
    ) -> None:
        await self._handle_admin_penalty_adjustment(
            interaction,
            discord_user_id=discord_user_id,
            penalty_type=penalty_type,
            delta=-1,
        )

    async def _handle_match_parent(
        self,
        interaction: discord.Interaction[Any],
        *,
        discord_user_id: int,
        match_id: int,
        success_message: str,
        failure_message: str,
    ) -> None:
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, discord_user_id)
            service = self._require_match_service()
            await asyncio.to_thread(service.volunteer_parent, match_id, player_id)
        except PlayerNotRegisteredError:
            await self._send_message(
                interaction,
                (
                    PLAYER_REGISTRATION_REQUIRED_MESSAGE
                    if discord_user_id == interaction.user.id
                    else DEV_TARGET_NOT_REGISTERED_MESSAGE
                ),
            )
            return
        except MatchNotFoundError:
            await self._send_message(interaction, MATCH_NOT_FOUND_MESSAGE)
            return
        except MatchParticipantError:
            await self._send_message(interaction, MATCH_PARTICIPANT_ONLY_MESSAGE)
            return
        except ParentAlreadyDecidedError:
            await self._send_message(interaction, MATCH_PARENT_ALREADY_DECIDED_MESSAGE)
            return
        except ParentVolunteerClosedError:
            await self._send_message(interaction, MATCH_PARENT_CLOSED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_parent command "
                "target_discord_user_id=%s match_id=%s",
                discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message)
            return

        await self._send_message(interaction, success_message)

    async def _handle_match_report(
        self,
        interaction: discord.Interaction[Any],
        *,
        discord_user_id: int,
        match_id: int,
        input_result: MatchReportInput,
        success_message: str,
        failure_message: str,
    ) -> None:
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, discord_user_id)
            service = self._require_match_service()
            await asyncio.to_thread(service.submit_report, match_id, player_id, input_result)
        except PlayerNotRegisteredError:
            await self._send_message(
                interaction,
                (
                    PLAYER_REGISTRATION_REQUIRED_MESSAGE
                    if discord_user_id == interaction.user.id
                    else DEV_TARGET_NOT_REGISTERED_MESSAGE
                ),
            )
            return
        except MatchNotFoundError:
            await self._send_message(interaction, MATCH_NOT_FOUND_MESSAGE)
            return
        except MatchParticipantError:
            await self._send_message(interaction, MATCH_PARTICIPANT_ONLY_MESSAGE)
            return
        except MatchReportNotOpenError:
            await self._send_message(interaction, MATCH_REPORT_NOT_OPEN_MESSAGE)
            return
        except MatchReportClosedError:
            await self._send_message(interaction, MATCH_REPORT_CLOSED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_report command "
                "target_discord_user_id=%s match_id=%s input_result=%s",
                discord_user_id,
                match_id,
                input_result.value,
            )
            await self._send_message(interaction, failure_message)
            return

        await self._send_message(interaction, success_message)

    async def _handle_match_approve(
        self,
        interaction: discord.Interaction[Any],
        *,
        discord_user_id: int,
        match_id: int,
        success_message: str,
        failure_message: str,
    ) -> None:
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, discord_user_id)
            service = self._require_match_service()
            await asyncio.to_thread(service.approve_provisional_result, match_id, player_id)
        except PlayerNotRegisteredError:
            await self._send_message(
                interaction,
                (
                    PLAYER_REGISTRATION_REQUIRED_MESSAGE
                    if discord_user_id == interaction.user.id
                    else DEV_TARGET_NOT_REGISTERED_MESSAGE
                ),
            )
            return
        except MatchNotFoundError:
            await self._send_message(interaction, MATCH_NOT_FOUND_MESSAGE)
            return
        except MatchParticipantError:
            await self._send_message(interaction, MATCH_PARTICIPANT_ONLY_MESSAGE)
            return
        except MatchApprovalNotOpenError:
            await self._send_message(interaction, MATCH_APPROVE_NOT_OPEN_MESSAGE)
            return
        except MatchApprovalNotRequiredError:
            await self._send_message(interaction, MATCH_APPROVE_NOT_REQUIRED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_approve command "
                "target_discord_user_id=%s match_id=%s",
                discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message)
            return

        await self._send_message(interaction, success_message)

    async def _handle_dev_match_report(
        self,
        interaction: discord.Interaction[Any],
        *,
        discord_user_id: str,
        match_id: int,
        input_result: MatchReportInput,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return

        await self._handle_match_report(
            interaction,
            discord_user_id=target_discord_user_id,
            match_id=match_id,
            input_result=input_result,
            success_message=DEV_MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_FAILED_MESSAGE,
        )

    async def _handle_admin_penalty_adjustment(
        self,
        interaction: discord.Interaction[Any],
        *,
        discord_user_id: str,
        penalty_type: PlayerPenaltyType,
        delta: int,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_match_service()
            await asyncio.to_thread(service.adjust_penalty, player_id, penalty_type, delta)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute admin penalty command "
                "executor_discord_user_id=%s target_discord_user_id=%s penalty_type=%s delta=%s",
                interaction.user.id,
                discord_user_id,
                penalty_type.value,
                delta,
            )
            await self._send_message(interaction, ADMIN_PENALTY_FAILED_MESSAGE)
            return

        await self._send_message(interaction, ADMIN_PENALTY_UPDATED_MESSAGE)

    def _register_player(self, discord_user_id: int) -> None:
        with session_scope(self.session_factory) as session:
            register_player(session=session, discord_user_id=discord_user_id)

    def _lookup_player_id(self, discord_user_id: int) -> int:
        return self.player_lookup_service.get_player_id_by_discord_user_id(discord_user_id)

    def _require_matching_queue_service(self) -> MatchingQueueService:
        if self._matching_queue_service is None:
            raise RuntimeError("MatchingQueueService is not configured")
        return self._matching_queue_service

    def _require_match_service(self) -> MatchService:
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
                interaction.user.id
                if mention_discord_user_id is None
                else mention_discord_user_id
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

    def _parse_match_result_type(self, value: str) -> MatchResultType:
        normalized_value = value.strip().lower()
        return MatchResultType(normalized_value)

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
    def register_penalty_command(
        *,
        name: str,
        description: str,
        penalty_type: PlayerPenaltyType,
        delta: int,
    ) -> None:
        if delta > 0:

            @tree.command(name=name, description=description)
            @app_commands.describe(discord_user_id="対象の Discord user ID")
            async def add_penalty_command(
                interaction: discord.Interaction[Any],
                discord_user_id: str,
            ) -> None:
                await handlers.admin_add_penalty(interaction, discord_user_id, penalty_type)

            return

        @tree.command(name=name, description=description)
        @app_commands.describe(discord_user_id="対象の Discord user ID")
        async def sub_penalty_command(
            interaction: discord.Interaction[Any],
            discord_user_id: str,
        ) -> None:
            await handlers.admin_sub_penalty(interaction, discord_user_id, penalty_type)

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

    @tree.command(name="match_parent", description="指定試合の親に立候補します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_parent_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_parent(interaction, match_id)

    @tree.command(name="match_win", description="指定試合の勝ちを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_win_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_win(interaction, match_id)

    @tree.command(name="match_loss", description="指定試合の負けを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_loss_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_loss(interaction, match_id)

    @tree.command(name="match_draw", description="指定試合の引き分けを報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_draw_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_draw(interaction, match_id)

    @tree.command(name="match_void", description="指定試合の無効試合を報告します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_void_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_void(interaction, match_id)

    @tree.command(name="match_approve", description="指定試合の仮決定結果を承認します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_approve_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_approve(interaction, match_id)

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

    @tree.command(name="dev_match_parent", description="ダミーユーザーを親に決定します")
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

    @tree.command(name="dev_match_loss", description="ダミーユーザーに負けを報告させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_loss_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_loss(interaction, discord_user_id, match_id)

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

    @tree.command(name="dev_match_approve", description="ダミーユーザーに承認させます")
    @app_commands.describe(discord_user_id="対象の dummy_user_id", match_id="対象の match_id")
    async def dev_match_approve_command(
        interaction: discord.Interaction[Any],
        discord_user_id: str,
        match_id: int,
    ) -> None:
        await handlers.dev_match_approve(interaction, discord_user_id, match_id)

    @tree.command(name="admin_match_result", description="試合結果を上書きします")
    @app_commands.describe(
        match_id="対象の match_id",
        result="team_a_win / team_b_win / draw / void",
    )
    async def admin_match_result_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        result: str,
    ) -> None:
        await handlers.admin_match_result(interaction, match_id, result)

    register_penalty_command(
        name="admin_add_incorrect",
        description="勝敗誤報告ペナルティを +1 します",
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_incorrect",
        description="勝敗誤報告ペナルティを -1 します",
        penalty_type=PlayerPenaltyType.INCORRECT_REPORT,
        delta=-1,
    )
    register_penalty_command(
        name="admin_add_unreported",
        description="勝敗無報告ペナルティを +1 します",
        penalty_type=PlayerPenaltyType.NOT_REPORTED,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_unreported",
        description="勝敗無報告ペナルティを -1 します",
        penalty_type=PlayerPenaltyType.NOT_REPORTED,
        delta=-1,
    )
    register_penalty_command(
        name="admin_add_room_delay",
        description="部屋立て遅延ペナルティを +1 します",
        penalty_type=PlayerPenaltyType.ROOM_DELAY,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_room_delay",
        description="部屋立て遅延ペナルティを -1 します",
        penalty_type=PlayerPenaltyType.ROOM_DELAY,
        delta=-1,
    )
    register_penalty_command(
        name="admin_add_match_mistake",
        description="試合ミスペナルティを +1 します",
        penalty_type=PlayerPenaltyType.MATCH_MISTAKE,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_match_mistake",
        description="試合ミスペナルティを -1 します",
        penalty_type=PlayerPenaltyType.MATCH_MISTAKE,
        delta=-1,
    )
    register_penalty_command(
        name="admin_add_late",
        description="遅刻ペナルティを +1 します",
        penalty_type=PlayerPenaltyType.LATE,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_late",
        description="遅刻ペナルティを -1 します",
        penalty_type=PlayerPenaltyType.LATE,
        delta=-1,
    )
    register_penalty_command(
        name="admin_add_disconnect",
        description="切断ペナルティを +1 します",
        penalty_type=PlayerPenaltyType.DISCONNECT,
        delta=1,
    )
    register_penalty_command(
        name="admin_sub_disconnect",
        description="切断ペナルティを -1 します",
        penalty_type=PlayerPenaltyType.DISCONNECT,
        delta=-1,
    )
