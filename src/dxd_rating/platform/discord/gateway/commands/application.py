from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import discord
from discord import app_commands
from sqlalchemy.orm import Session, sessionmaker

from dxd_rating.contexts.common.application import (
    InvalidMatchFormatError,
    InvalidPlayerAccessRestrictionDurationError,
    InvalidPlayerAccessRestrictionTypeError,
    InvalidQueueNameError,
    InvalidSeasonNameError,
    MatchFlowError,
    MatchSpectatingRestrictedError,
    PlayerAccessRestrictionAlreadyExistsError,
    PlayerAlreadyRegisteredError,
    PlayerNotRegisteredError,
    PlayerSeasonStatsNotFoundError,
    QueueAlreadyJoinedError,
    QueueJoinNotAllowedError,
    QueueJoinRestrictedError,
    QueueNotJoinedError,
    SeasonAlreadyExistsError,
    SeasonNotFoundError,
)
from dxd_rating.contexts.matches.application import (
    MatchReportSubmissionResult,
    MatchSpectateResult,
    PlayerPenaltyAdjustmentResult,
)
from dxd_rating.contexts.matchmaking.application import (
    JoinQueueResult,
    LeaveQueueResult,
    MatchingQueueNotificationContext,
    PresentQueueResult,
)
from dxd_rating.contexts.players.application import (
    PlayerIdentityService,
    PlayerInfo,
    PlayerLookupService,
    register_player,
)
from dxd_rating.contexts.players.domain import resolve_player_display_name
from dxd_rating.contexts.restrictions.application import (
    PlayerAccessRestrictionDuration,
    PlayerAccessRestrictionService,
)
from dxd_rating.contexts.seasons.application import SeasonService
from dxd_rating.contexts.ui.application import (
    REGISTERED_PLAYER_ROLE_NAME,
    InfoThreadBindingService,
    InfoThreadCommandName,
    ManagedUiDefinition,
    ManagedUiService,
    get_managed_ui_definition,
    get_required_managed_ui_definitions,
)
from dxd_rating.platform.config.bot import BotSettings
from dxd_rating.platform.db.models import (
    ManagedUiChannel,
    ManagedUiType,
    MatchFormat,
    MatchReportInputResult,
    MatchResult,
    PenaltyType,
    PlayerAccessRestrictionType,
)
from dxd_rating.platform.db.session import session_scope
from dxd_rating.platform.discord.ui import (
    build_info_thread_initial_message,
    build_managed_ui_channel_overwrites,
    create_matchmaking_presence_thread_view,
    is_valid_managed_ui_channel_name,
    send_initial_managed_ui_message,
)
from dxd_rating.shared.constants import (
    MATCH_FORMAT_CHOICES,
    MATCH_QUEUE_NAME_CHOICES,
    is_dummy_discord_user_id,
)

REGISTER_SUCCESS_MESSAGE = "登録が完了しました。"
REGISTER_ALREADY_REGISTERED_MESSAGE = "すでに登録済みです。"
REGISTER_FAILED_MESSAGE = "登録に失敗しました。管理者に確認してください。"

PLAYER_REGISTRATION_REQUIRED_MESSAGE = (
    "プレイヤー登録が必要です。先に /register を実行してください。"
)
INVALID_QUEUE_NAME_MESSAGE = "指定したキューは存在しません。"
QUEUE_JOIN_NOT_ALLOWED_MESSAGE = "現在のレーティングではそのキューに参加できません。"
QUEUE_JOIN_RESTRICTED_MESSAGE = "現在キュー参加を制限されています。"
JOIN_ALREADY_JOINED_MESSAGE = "すでにキュー参加中です。"
PRESENT_NOT_JOINED_MESSAGE = "キューに参加していません。"
MATCHMAKING_PRESENCE_THREAD_NOT_JOINED_MESSAGE = (
    "現在このキューには参加していません。"
    "再参加する場合は親チャンネルの参加導線から参加してください。"
)
MATCHMAKING_PRESENCE_THREAD_MISMATCH_MESSAGE = (
    "このスレッドは現在参加中のキューには紐づいていません。"
    "再参加する場合は親チャンネルの参加ボタンから参加してください。"
)
JOIN_FAILED_MESSAGE = "キュー参加に失敗しました。管理者に確認してください。"
PRESENT_FAILED_MESSAGE = "在席更新に失敗しました。管理者に確認してください。"
LEAVE_FAILED_MESSAGE = "キュー退出に失敗しました。管理者に確認してください。"
PLAYER_INFO_FAILED_MESSAGE = "プレイヤー情報の取得に失敗しました。管理者に確認してください。"
PLAYER_SEASON_INFO_FAILED_MESSAGE = (
    "シーズン別プレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
INFO_THREAD_SUCCESS_MESSAGE = "情報確認用スレッドを作成しました。"
INFO_THREAD_CHANNEL_NOT_FOUND_MESSAGE = (
    "情報確認用チャンネルが見つかりません。管理者に確認してください。"
)
INFO_THREAD_FAILED_MESSAGE = "情報確認用スレッドの作成に失敗しました。管理者に確認してください。"

MATCH_PARENT_SUCCESS_MESSAGE = "親に立候補しました。"
MATCH_SPECTATE_RESTRICTED_MESSAGE = "現在観戦を制限されています。"
MATCH_SPECTATE_FAILED_MESSAGE = "観戦応募に失敗しました。管理者に確認してください。"
MATCH_REPORT_SUCCESS_MESSAGE = "勝敗報告を受け付けました。"
MATCH_APPROVE_SUCCESS_MESSAGE = "仮決定結果を承認しました。"
MATCH_ACTION_FAILED_MESSAGE = "試合操作に失敗しました。管理者に確認してください。"

ADMIN_ONLY_MESSAGE = "このコマンドは管理者のみ実行できます。"
INVALID_DISCORD_USER_ID_MESSAGE = "discord_user_id が不正です。"
INVALID_ADMIN_TARGET_USER_MESSAGE = "対象ユーザーの指定が不正です。"
ADMIN_MATCH_RESULT_SUCCESS_MESSAGE = "試合結果を上書きしました。"
ADMIN_MATCH_RESULT_FAILED_MESSAGE = "試合結果の上書きに失敗しました。管理者に確認してください。"
ADMIN_TARGET_NOT_REGISTERED_MESSAGE = "指定したユーザーは未登録です。"
ADMIN_RESTRICTION_FAILED_MESSAGE = "利用制限の設定に失敗しました。管理者に確認してください。"
ADMIN_UNRESTRICTION_FAILED_MESSAGE = "利用制限の解除に失敗しました。管理者に確認してください。"
ADMIN_RESTRICTION_ALREADY_EXISTS_MESSAGE = "指定したユーザーにはすでに同種別の制限が有効です。"
INVALID_RESTRICTION_TYPE_MESSAGE = "restriction_type が不正です。"
INVALID_RESTRICTION_DURATION_MESSAGE = "duration が不正です。"
ADMIN_PENALTY_ADD_SUCCESS_MESSAGE = "ペナルティを加算しました。"
ADMIN_PENALTY_SUB_SUCCESS_MESSAGE = "ペナルティを減算しました。"
ADMIN_PENALTY_FAILED_MESSAGE = "ペナルティ操作に失敗しました。管理者に確認してください。"
ADMIN_RENAME_SEASON_SUCCESS_MESSAGE = "シーズン名を変更しました。"
ADMIN_RENAME_SEASON_FAILED_MESSAGE = "シーズン名の変更に失敗しました。管理者に確認してください。"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_SUCCESS_MESSAGE = "UI 設置チャンネルを作成しました。"
ADMIN_INVALID_UI_TYPE_MESSAGE = "指定した UI は存在しません。"
ADMIN_INVALID_CHANNEL_NAME_MESSAGE = "channel_name が不正です。"
ADMIN_DUPLICATE_CHANNEL_NAME_MESSAGE = "同名のチャンネルがすでに存在します。"
ADMIN_UI_ALREADY_INSTALLED_MESSAGE = "指定した UI はすでに設置済みです。"
ADMIN_MANAGED_UI_PERMISSION_MESSAGE = "Bot に必要な権限がありません。"
MANAGED_UI_PERMISSION_LABEL_MANAGE_CHANNELS = "チャンネルの管理"
MANAGED_UI_PERMISSION_LABEL_MANAGE_ROLES = "ロールの管理"
MANAGED_UI_PERMISSION_LABEL_CREATE_PRIVATE_THREADS = "プライベートスレッドの作成"
MANAGED_UI_PERMISSION_LABEL_SEND_MESSAGES_IN_THREADS = "スレッドでメッセージを送信"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_FAILED_MESSAGE = (
    "UI 設置チャンネルの作成に失敗しました。管理者に確認してください。"
)
ADMIN_SETUP_UI_CHANNELS_SUCCESS_MESSAGE = "必要な UI 設置チャンネルを作成しました。"
ADMIN_SETUP_UI_CHANNELS_ALREADY_CREATED_MESSAGE = "必要な UI 設置チャンネルはすでに作成済みです。"
ADMIN_RECOMMENDED_CHANNEL_NAME_CONFLICT_MESSAGE = "推奨チャンネル名のチャンネルがすでに存在します。"
ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE = (
    "必要な UI 設置チャンネルの作成に失敗しました。管理者に確認してください。"
)
ADMIN_INVALID_CLEANUP_CONFIRM_MESSAGE = "confirm が不正です。"
ADMIN_CLEANUP_UI_CHANNELS_SUCCESS_MESSAGE = "setup の障害となる重複チャンネルを削除しました。"
ADMIN_CLEANUP_UI_CHANNELS_EMPTY_MESSAGE = "削除対象の重複チャンネルはありません。"
ADMIN_CLEANUP_UI_CHANNELS_FAILED_MESSAGE = (
    "重複チャンネルの cleanup に失敗しました。管理者に確認してください。"
)
ADMIN_CLEANUP_CONFIRM_VALUE = "cleanup"
ADMIN_INVALID_TEARDOWN_CONFIRM_MESSAGE = "confirm が不正です。"
ADMIN_TEARDOWN_UI_CHANNELS_SUCCESS_MESSAGE = "UI 設置チャンネルをすべて撤収しました。"
ADMIN_TEARDOWN_UI_CHANNELS_EMPTY_MESSAGE = "撤収対象の UI 設置チャンネルはありません。"
ADMIN_TEARDOWN_UI_CHANNELS_FAILED_MESSAGE = (
    "UI 設置チャンネルの撤収に失敗しました。管理者に確認してください。"
)
ADMIN_TEARDOWN_CONFIRM_VALUE = "teardown"
MATCHMAKING_PRESENCE_THREAD_NAME_PREFIX = "在席確認-"
INFO_THREAD_NAME_PREFIX = "情報-"
MAX_DISCORD_THREAD_NAME_LENGTH = 100
MATCHMAKING_PRESENCE_THREAD_GUIDE_MESSAGE = "在席確認は {thread_mention} で行ってください。"

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
DEV_JOIN_RESTRICTED_MESSAGE = "指定したユーザーは現在キュー参加を制限されています。"
DEV_JOIN_FAILED_MESSAGE = "指定したユーザーのキュー参加に失敗しました。管理者に確認してください。"

DEV_PRESENT_SUCCESS_MESSAGE = "指定したユーザーの在席を更新しました。"
DEV_PRESENT_NOT_JOINED_MESSAGE = "指定したユーザーはキューに参加していません。"
DEV_PRESENT_EXPIRED_MESSAGE = "指定したユーザーは期限切れのためキューから外れました。"
DEV_PRESENT_FAILED_MESSAGE = "指定したユーザーの在席更新に失敗しました。管理者に確認してください。"

DEV_LEAVE_SUCCESS_MESSAGE = "指定したユーザーをキューから退出させました。"
DEV_LEAVE_EXPIRED_MESSAGE = "指定したユーザーはすでに期限切れでキューから外れています。"
DEV_LEAVE_FAILED_MESSAGE = "指定したユーザーのキュー退出に失敗しました。管理者に確認してください。"
DEV_PLAYER_INFO_FAILED_MESSAGE = (
    "指定したユーザーのプレイヤー情報取得に失敗しました。管理者に確認してください。"
)
DEV_PLAYER_SEASON_INFO_FAILED_MESSAGE = (
    "指定したユーザーのシーズン別プレイヤー情報取得に失敗しました。管理者に確認してください。"
)

DEV_MATCH_PARENT_SUCCESS_MESSAGE = "指定したユーザーを親に立候補させました。"
DEV_MATCH_SPECTATE_SUCCESS_MESSAGE = "指定したユーザーの観戦応募を受け付けました。"
DEV_MATCH_SPECTATE_RESTRICTED_MESSAGE = "指定したユーザーは現在観戦を制限されています。"
DEV_MATCH_REPORT_SUCCESS_MESSAGE = "指定したユーザーの勝敗報告を受け付けました。"
DEV_MATCH_APPROVE_SUCCESS_MESSAGE = "指定したユーザーが仮決定結果を承認しました。"
DEV_MATCH_ACTION_FAILED_MESSAGE = (
    "ダミーユーザーの試合操作に失敗しました。管理者に確認してください。"
)

DEV_IS_ADMIN_ERROR_MESSAGE = "エラーが発生しました。管理者に確認してください。"

PLAYER_ACCESS_RESTRICTION_TYPE_LABELS = {
    PlayerAccessRestrictionType.QUEUE_JOIN: "キュー参加",
    PlayerAccessRestrictionType.SPECTATE: "観戦",
}
PLAYER_ACCESS_RESTRICTION_DURATION_LABELS = {
    PlayerAccessRestrictionDuration.ONE_DAY: "1日",
    PlayerAccessRestrictionDuration.THREE_DAYS: "3日",
    PlayerAccessRestrictionDuration.SEVEN_DAYS: "7日",
    PlayerAccessRestrictionDuration.FOURTEEN_DAYS: "14日",
    PlayerAccessRestrictionDuration.TWENTY_EIGHT_DAYS: "28日",
    PlayerAccessRestrictionDuration.FIFTY_SIX_DAYS: "56日",
    PlayerAccessRestrictionDuration.EIGHTY_FOUR_DAYS: "84日",
    PlayerAccessRestrictionDuration.PERMANENT: "永久",
}

DUMMY_USER_REFERENCE_PATTERN = re.compile(r"<dummy_(\d+)>")


@dataclass(frozen=True, slots=True)
class ProvisionedManagedUiChannel:
    definition: ManagedUiDefinition
    channel: discord.abc.GuildChannel


class ManagedUiProvisioningError(Exception):
    def __init__(self, provisioned_channel: ProvisionedManagedUiChannel) -> None:
        super().__init__(provisioned_channel.definition.ui_type.value)
        self.provisioned_channel = provisioned_channel


class RequiredManagedUiChannelUnavailableError(RuntimeError):
    def __init__(
        self,
        *,
        ui_type: ManagedUiType,
        reason: str,
        channel_id: int | None = None,
    ) -> None:
        super().__init__(f"{ui_type.value}: {reason}")
        self.ui_type = ui_type
        self.reason = reason
        self.channel_id = channel_id


def is_super_admin(user_id: int, settings: BotSettings) -> bool:
    return user_id in settings.super_admin_user_ids


class DiscordUserLike(Protocol):
    id: int


class MatchingQueueCommandService(Protocol):
    async def join_queue(
        self,
        player_id: int,
        match_format: MatchFormat | str,
        queue_name: str,
        *,
        notification_context: MatchingQueueNotificationContext | None = None,
        after_join: Callable[[JoinQueueResult], Awaitable[None]] | None = None,
    ) -> JoinQueueResult: ...

    async def update_waiting_notification_context(
        self,
        queue_entry_id: int,
        notification_context: MatchingQueueNotificationContext,
    ) -> bool: ...

    async def update_waiting_presence_thread_channel_id(
        self,
        queue_entry_id: int,
        presence_thread_channel_id: int,
    ) -> bool: ...

    async def get_waiting_entry_notification_channel_id(self, player_id: int) -> int | None: ...

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

    async def spectate_match(
        self,
        match_id: int,
        player_id: int,
    ) -> MatchSpectateResult: ...

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


class PlayerAccessRestrictionCommandService(Protocol):
    def restrict_player_access(
        self,
        player_id: int,
        restriction_type: PlayerAccessRestrictionType | str,
        duration: PlayerAccessRestrictionDuration | str,
        *,
        admin_discord_user_id: int,
        reason: str | None = None,
    ) -> object: ...

    def unrestrict_player_access(
        self,
        player_id: int,
        restriction_type: PlayerAccessRestrictionType | str,
        *,
        admin_discord_user_id: int,
    ) -> object: ...


class BotCommandHandlers:
    def __init__(
        self,
        settings: BotSettings,
        session_factory: sessionmaker[Session],
        *,
        matching_queue_service: MatchingQueueCommandService | None = None,
        match_service: MatchCommandService | None = None,
        player_access_restriction_service: PlayerAccessRestrictionCommandService | None = None,
        player_lookup_service: PlayerLookupService | None = None,
        player_identity_service: PlayerIdentityService | None = None,
        season_service: SeasonService | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(__name__)
        self._matching_queue_service = matching_queue_service
        self._match_service = match_service
        self._player_access_restriction_service = (
            player_access_restriction_service or PlayerAccessRestrictionService(session_factory)
        )
        self.player_identity_service = player_identity_service or PlayerIdentityService(
            session_factory
        )
        self.season_service = season_service or SeasonService(session_factory)
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
        self.managed_ui_service = ManagedUiService(session_factory)
        self.info_thread_binding_service = InfoThreadBindingService(session_factory)

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
        await self._run_register(interaction, ephemeral=True)

    async def register_from_ui(self, interaction: discord.Interaction[Any]) -> None:
        await self._run_register(interaction, ephemeral=True)

    async def _run_register(
        self,
        interaction: discord.Interaction[Any],
        *,
        ephemeral: bool,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        try:
            await asyncio.to_thread(self._register_player, interaction.user.id)
        except PlayerAlreadyRegisteredError:
            await self._send_player_operation_message(
                interaction,
                REGISTER_ALREADY_REGISTERED_MESSAGE,
            )
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /register command discord_user_id=%s",
                interaction.user.id,
            )
            if ephemeral:
                await self._send_player_operation_message(interaction, REGISTER_FAILED_MESSAGE)
            else:
                await self._send_message(interaction, REGISTER_FAILED_MESSAGE, ephemeral=False)
            return

        await self._sync_requesting_user_identity(interaction)
        await self._best_effort_assign_registered_player_role(interaction)
        if ephemeral:
            await self._send_player_operation_message(interaction, REGISTER_SUCCESS_MESSAGE)
        else:
            await self._send_message(interaction, REGISTER_SUCCESS_MESSAGE, ephemeral=False)

    async def join(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
    ) -> None:
        await self._run_join(
            interaction,
            match_format,
            queue_name,
            create_presence_thread=True,
        )

    async def join_from_ui(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
    ) -> None:
        await self._run_join(
            interaction,
            match_format,
            queue_name,
            create_presence_thread=True,
        )

    async def _run_join(
        self,
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
        *,
        create_presence_thread: bool,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        parent_channel: discord.abc.GuildChannel | None = None
        thread_id: int | None = None
        try:
            parent_channel = await self._resolve_required_matchmaking_presence_parent_channel(
                interaction
            )
            notification_context = await self._build_matchmaking_join_notification_context(
                interaction,
                parent_channel=parent_channel,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            service = self._require_matching_queue_service()

            async def after_join(result: JoinQueueResult) -> None:
                nonlocal thread_id
                if not create_presence_thread:
                    return

                thread_id = await self._create_and_bind_matchmaking_presence_thread(
                    interaction,
                    queue_entry_id=result.queue_entry_id,
                    parent_channel=parent_channel,
                    initial_message=result.message,
                    target_discord_user_id=interaction.user.id,
                    target_user=interaction.user,
                    invite_target_user=True,
                )

            result = await service.join_queue(
                player_id,
                match_format,
                queue_name,
                notification_context=notification_context,
                after_join=after_join,
            )
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except InvalidMatchFormatError:
            await self._send_player_operation_message(
                interaction,
                "指定したフォーマットは存在しません。",
            )
            return
        except InvalidQueueNameError:
            await self._send_player_operation_message(interaction, INVALID_QUEUE_NAME_MESSAGE)
            return
        except QueueJoinNotAllowedError:
            await self._send_player_operation_message(
                interaction,
                QUEUE_JOIN_NOT_ALLOWED_MESSAGE,
            )
            return
        except QueueJoinRestrictedError:
            await self._send_player_operation_message(
                interaction,
                QUEUE_JOIN_RESTRICTED_MESSAGE,
            )
            return
        except QueueAlreadyJoinedError:
            await self._send_player_operation_message(interaction, JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /join command discord_user_id=%s match_format=%s queue_name=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                match_format,
                queue_name,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_player_operation_message(interaction, JOIN_FAILED_MESSAGE)
            return

        await self._send_player_operation_message(
            interaction,
            self._format_matchmaking_join_success_message(result.message, thread_id=thread_id),
        )

    async def present(self, interaction: discord.Interaction[Any]) -> None:
        await self._run_present(interaction, require_presence_thread_binding=False)

    async def present_from_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        await self._run_present(interaction, require_presence_thread_binding=True)

    async def _run_present(
        self,
        interaction: discord.Interaction[Any],
        *,
        require_presence_thread_binding: bool,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            if require_presence_thread_binding:
                should_continue = await self._validate_matchmaking_presence_thread_binding(
                    interaction,
                    player_id,
                )
                if not should_continue:
                    return
            service = self._require_matching_queue_service()
            result = await service.present(
                player_id,
                notification_context=None,
            )
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except QueueNotJoinedError:
            await self._send_player_operation_message(
                interaction,
                MATCHMAKING_PRESENCE_THREAD_NOT_JOINED_MESSAGE
                if require_presence_thread_binding
                else PRESENT_NOT_JOINED_MESSAGE,
            )
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /present command discord_user_id=%s channel_id=%s guild_id=%s",
                interaction.user.id,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_player_operation_message(interaction, PRESENT_FAILED_MESSAGE)
            return

        await self._send_player_operation_message(interaction, result.message)

    async def leave(self, interaction: discord.Interaction[Any]) -> None:
        await self._run_leave(interaction, require_presence_thread_binding=False)

    async def leave_from_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        await self._run_leave(interaction, require_presence_thread_binding=True)

    async def _run_leave(
        self,
        interaction: discord.Interaction[Any],
        *,
        require_presence_thread_binding: bool,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            if require_presence_thread_binding:
                should_continue = await self._validate_matchmaking_presence_thread_binding(
                    interaction,
                    player_id,
                )
                if not should_continue:
                    return
            service = self._require_matching_queue_service()
            result = await service.leave(player_id)
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /leave command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_player_operation_message(interaction, LEAVE_FAILED_MESSAGE)
            return

        await self._send_player_operation_message(interaction, result.message)

    async def player_info(self, interaction: discord.Interaction[Any]) -> None:
        await self._sync_requesting_user_identity(interaction)
        try:
            player_info = await asyncio.to_thread(
                self._lookup_player_info,
                interaction.user.id,
            )
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /player_info command discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_player_operation_message(interaction, PLAYER_INFO_FAILED_MESSAGE)
            return

        await self._send_player_operation_message(
            interaction,
            self._format_player_info_message(player_info),
        )

    async def info_thread(
        self,
        interaction: discord.Interaction[Any],
        command_name: str,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        created_thread: object | None = None
        resolved_command_name: InfoThreadCommandName | None = None

        try:
            resolved_command_name = self._parse_info_thread_command_name(command_name)
            player_id = await asyncio.to_thread(self._lookup_player_id, interaction.user.id)
            parent_channel = await self._resolve_required_info_thread_parent_channel(interaction)
            created_thread = await self._create_info_thread(
                interaction,
                parent_channel=parent_channel,
                command_name=resolved_command_name,
                target_discord_user_id=interaction.user.id,
                target_user=interaction.user,
            )
            await asyncio.to_thread(
                self._upsert_latest_info_thread_channel_id,
                player_id,
                self._require_discord_channel_id(created_thread),
            )
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except RequiredManagedUiChannelUnavailableError:
            await self._send_player_operation_message(
                interaction,
                INFO_THREAD_CHANNEL_NOT_FOUND_MESSAGE,
            )
            return
        except Exception:
            if created_thread is not None:
                await self._best_effort_delete_info_thread(
                    created_thread,
                    reason=(
                        f"Rollback info thread creation for discord_user_id={interaction.user.id}"
                    ),
                )

            self.logger.exception(
                "Failed to execute /info_thread command discord_user_id=%s command_name=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                command_name if resolved_command_name is None else resolved_command_name.value,
                interaction.channel_id,
                interaction.guild_id,
            )
            await self._send_player_operation_message(interaction, INFO_THREAD_FAILED_MESSAGE)
            return

        await self._send_player_operation_message(interaction, INFO_THREAD_SUCCESS_MESSAGE)

    async def player_info_season(
        self,
        interaction: discord.Interaction[Any],
        season_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        try:
            player_info = await asyncio.to_thread(
                self._lookup_player_info_by_season,
                interaction.user.id,
                season_id,
            )
        except PlayerNotRegisteredError:
            await self._send_player_operation_message(
                interaction,
                PLAYER_REGISTRATION_REQUIRED_MESSAGE,
            )
            return
        except (SeasonNotFoundError, PlayerSeasonStatsNotFoundError) as exc:
            await self._send_player_operation_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /player_info_season command discord_user_id=%s season_id=%s",
                interaction.user.id,
                season_id,
            )
            await self._send_player_operation_message(
                interaction,
                PLAYER_SEASON_INFO_FAILED_MESSAGE,
            )
            return

        await self._send_player_operation_message(
            interaction,
            self._format_player_info_message(player_info, include_season=True),
        )

    async def match_parent(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_parent(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def parent_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_parent(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=MATCH_PARENT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_spectate(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_spectate(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=None,
            failure_message=MATCH_SPECTATE_FAILED_MESSAGE,
            ephemeral=False,
        )

    async def spectate_from_matchmaking_news_match_announcement(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_spectate(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=None,
            failure_message=MATCH_SPECTATE_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_win(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.WIN,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def win_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.WIN,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_lose(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.LOSE,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def lose_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.LOSE,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_draw(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.DRAW,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def draw_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.DRAW,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_void(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.VOID,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def void_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_report(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            input_result=MatchReportInputResult.VOID,
            success_message=MATCH_REPORT_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

    async def match_approve(self, interaction: discord.Interaction[Any], match_id: int) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_approve(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=MATCH_APPROVE_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
        )

    async def approve_from_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await self._sync_requesting_user_identity(interaction)
        await self._run_match_approve(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=interaction.user.id,
            success_message=MATCH_APPROVE_SUCCESS_MESSAGE,
            failure_message=MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=True,
        )

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

    async def admin_rename_season(
        self,
        interaction: discord.Interaction[Any],
        season_id: int,
        name: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            await asyncio.to_thread(self._rename_season, season_id, name)
        except (SeasonNotFoundError, InvalidSeasonNameError, SeasonAlreadyExistsError) as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                (
                    "Failed to execute /admin_rename_season command "
                    "executor_discord_user_id=%s season_id=%s"
                ),
                interaction.user.id,
                season_id,
            )
            await self._send_message(interaction, ADMIN_RENAME_SEASON_FAILED_MESSAGE)
            return

        await self._send_message(interaction, ADMIN_RENAME_SEASON_SUCCESS_MESSAGE)

    async def admin_setup_custom_ui_channel(
        self,
        interaction: discord.Interaction[Any],
        ui_type: str,
        channel_name: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            definition = get_managed_ui_definition(self._parse_managed_ui_type(ui_type))
        except ValueError:
            await self._send_message(
                interaction,
                ADMIN_INVALID_UI_TYPE_MESSAGE,
                ephemeral=True,
            )
            return

        if not is_valid_managed_ui_channel_name(channel_name):
            await self._send_message(
                interaction,
                ADMIN_INVALID_CHANNEL_NAME_MESSAGE,
                ephemeral=True,
            )
            return

        await self._defer_message_response(interaction, ephemeral=True)

        try:
            existing_managed_ui_channel = await asyncio.to_thread(
                self._get_managed_ui_channel_by_type,
                definition.ui_type,
            )
            if definition.singleton and existing_managed_ui_channel is not None:
                await self._send_message(
                    interaction,
                    ADMIN_UI_ALREADY_INSTALLED_MESSAGE,
                    ephemeral=True,
                )
                return

            guild = self._require_guild(interaction)
            if self._guild_has_channel_named(guild, channel_name):
                await self._send_message(
                    interaction,
                    ADMIN_DUPLICATE_CHANNEL_NAME_MESSAGE,
                    ephemeral=True,
                )
                return

            missing_permissions = self._find_missing_managed_ui_setup_permissions(
                guild,
                [definition],
            )
            if missing_permissions:
                await self._send_message(
                    interaction,
                    self._format_managed_ui_permission_message(missing_permissions),
                    ephemeral=True,
                )
                return

            try:
                await self._provision_managed_ui_channel(
                    guild=guild,
                    definition=definition,
                    channel_name=channel_name,
                    created_by_discord_user_id=interaction.user.id,
                )
            except discord.Forbidden as exc:
                self._log_managed_ui_forbidden(
                    action="admin_setup_custom_ui_channel",
                    executor_discord_user_id=interaction.user.id,
                    ui_type=definition.ui_type.value,
                    exc=exc,
                )
                await self._send_message(
                    interaction,
                    self._format_managed_ui_permission_message(
                        self._find_missing_managed_ui_setup_permissions(guild, [definition]),
                        forbidden_error=exc,
                    ),
                    ephemeral=True,
                )
                return
            except ManagedUiProvisioningError as exc:
                rollback_succeeded = await self._rollback_provisioned_managed_ui_channels(
                    [exc.provisioned_channel],
                    log_context=(
                        "admin_setup_custom_ui_channel "
                        f"executor_discord_user_id={interaction.user.id} "
                        f"ui_type={definition.ui_type.value}"
                    ),
                )
                if not rollback_succeeded:
                    await self._send_message(
                        interaction,
                        ADMIN_SETUP_CUSTOM_UI_CHANNEL_FAILED_MESSAGE,
                        ephemeral=True,
                    )
                    return

                forbidden_cause = exc.__cause__
                if isinstance(forbidden_cause, discord.Forbidden):
                    self._log_managed_ui_forbidden(
                        action="admin_setup_custom_ui_channel",
                        executor_discord_user_id=interaction.user.id,
                        ui_type=definition.ui_type.value,
                        exc=forbidden_cause,
                    )
                    await self._send_message(
                        interaction,
                        self._format_managed_ui_permission_message(
                            self._find_missing_managed_ui_setup_permissions(guild, [definition]),
                            forbidden_error=forbidden_cause,
                        ),
                        ephemeral=True,
                    )
                    return

                self.logger.exception(
                    "Failed to execute /admin_setup_custom_ui_channel command "
                    "executor_discord_user_id=%s ui_type=%s channel_name=%s",
                    interaction.user.id,
                    definition.ui_type.value,
                    channel_name,
                )
                await self._send_message(
                    interaction,
                    ADMIN_SETUP_CUSTOM_UI_CHANNEL_FAILED_MESSAGE,
                    ephemeral=True,
                )
                return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_setup_custom_ui_channel command "
                "executor_discord_user_id=%s ui_type=%s channel_name=%s",
                interaction.user.id,
                ui_type,
                channel_name,
            )
            await self._send_message(
                interaction,
                ADMIN_SETUP_CUSTOM_UI_CHANNEL_FAILED_MESSAGE,
                ephemeral=True,
            )
            return

        await self._send_message(
            interaction,
            ADMIN_SETUP_CUSTOM_UI_CHANNEL_SUCCESS_MESSAGE,
            ephemeral=True,
        )

    async def admin_setup_ui_channels(self, interaction: discord.Interaction[Any]) -> None:
        if not await self._ensure_admin(interaction):
            return

        await self._defer_message_response(interaction, ephemeral=True)

        try:
            guild = self._require_guild(interaction)
            managed_ui_channels = await asyncio.to_thread(self._list_managed_ui_channels)
            missing_definitions = self._get_missing_required_managed_ui_definitions(
                managed_ui_channels
            )
            if not missing_definitions:
                await self._send_message(
                    interaction,
                    ADMIN_SETUP_UI_CHANNELS_ALREADY_CREATED_MESSAGE,
                    ephemeral=True,
                )
                return

            managed_channel_ids = {
                managed_ui_channel.channel_id for managed_ui_channel in managed_ui_channels
            }
            if self._find_setup_blocking_unmanaged_channels(
                guild,
                missing_definitions=missing_definitions,
                managed_channel_ids=managed_channel_ids,
            ):
                await self._send_message(
                    interaction,
                    ADMIN_RECOMMENDED_CHANNEL_NAME_CONFLICT_MESSAGE,
                    ephemeral=True,
                )
                return

            missing_permissions = self._find_missing_managed_ui_setup_permissions(
                guild,
                missing_definitions,
            )
            if missing_permissions:
                await self._send_message(
                    interaction,
                    self._format_managed_ui_permission_message(missing_permissions),
                    ephemeral=True,
                )
                return

            provisioned_channels: list[ProvisionedManagedUiChannel] = []
            for definition in missing_definitions:
                try:
                    provisioned_channel = await self._provision_managed_ui_channel(
                        guild=guild,
                        definition=definition,
                        channel_name=definition.recommended_channel_name,
                        created_by_discord_user_id=interaction.user.id,
                        private_channel=self.settings.development_mode,
                        visible_member=interaction.user,
                    )
                except discord.Forbidden as exc:
                    rollback_succeeded = await self._rollback_provisioned_managed_ui_channels(
                        provisioned_channels,
                        log_context=(
                            "admin_setup_ui_channels "
                            f"executor_discord_user_id={interaction.user.id}"
                        ),
                    )
                    if not rollback_succeeded:
                        await self._send_message(
                            interaction,
                            ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                            ephemeral=True,
                        )
                        return

                    self._log_managed_ui_forbidden(
                        action="admin_setup_ui_channels",
                        executor_discord_user_id=interaction.user.id,
                        ui_type=definition.ui_type.value,
                        exc=exc,
                    )
                    await self._send_message(
                        interaction,
                        self._format_managed_ui_permission_message(
                            self._find_missing_managed_ui_setup_permissions(
                                guild,
                                missing_definitions,
                            ),
                            forbidden_error=exc,
                        ),
                        ephemeral=True,
                    )
                    return
                except ManagedUiProvisioningError as exc:
                    rollback_succeeded = await self._rollback_provisioned_managed_ui_channels(
                        [*provisioned_channels, exc.provisioned_channel],
                        log_context=(
                            "admin_setup_ui_channels "
                            f"executor_discord_user_id={interaction.user.id}"
                        ),
                    )
                    if not rollback_succeeded:
                        await self._send_message(
                            interaction,
                            ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                            ephemeral=True,
                        )
                        return

                    forbidden_cause = exc.__cause__
                    if isinstance(forbidden_cause, discord.Forbidden):
                        self._log_managed_ui_forbidden(
                            action="admin_setup_ui_channels",
                            executor_discord_user_id=interaction.user.id,
                            ui_type=definition.ui_type.value,
                            exc=forbidden_cause,
                        )
                        await self._send_message(
                            interaction,
                            self._format_managed_ui_permission_message(
                                self._find_missing_managed_ui_setup_permissions(
                                    guild,
                                    missing_definitions,
                                ),
                                forbidden_error=forbidden_cause,
                            ),
                            ephemeral=True,
                        )
                        return

                    self.logger.exception(
                        "Failed to execute /admin_setup_ui_channels command "
                        "executor_discord_user_id=%s ui_type=%s",
                        interaction.user.id,
                        definition.ui_type.value,
                    )
                    await self._send_message(
                        interaction,
                        ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                        ephemeral=True,
                    )
                    return
                except Exception:
                    rollback_succeeded = await self._rollback_provisioned_managed_ui_channels(
                        provisioned_channels,
                        log_context=(
                            "admin_setup_ui_channels "
                            f"executor_discord_user_id={interaction.user.id}"
                        ),
                    )
                    if not rollback_succeeded:
                        await self._send_message(
                            interaction,
                            ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                            ephemeral=True,
                        )
                        return

                    self.logger.exception(
                        "Failed to execute /admin_setup_ui_channels command "
                        "executor_discord_user_id=%s ui_type=%s",
                        interaction.user.id,
                        definition.ui_type.value,
                    )
                    await self._send_message(
                        interaction,
                        ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                        ephemeral=True,
                    )
                    return

                provisioned_channels.append(provisioned_channel)
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_setup_ui_channels command executor_discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(
                interaction,
                ADMIN_SETUP_UI_CHANNELS_FAILED_MESSAGE,
                ephemeral=True,
            )
            return

        await self._send_message(
            interaction,
            ADMIN_SETUP_UI_CHANNELS_SUCCESS_MESSAGE,
            ephemeral=True,
        )

    async def admin_cleanup_ui_channels(
        self,
        interaction: discord.Interaction[Any],
        confirm: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        if confirm != ADMIN_CLEANUP_CONFIRM_VALUE:
            await self._send_message(
                interaction,
                ADMIN_INVALID_CLEANUP_CONFIRM_MESSAGE,
                ephemeral=True,
            )
            return

        await self._defer_message_response(interaction, ephemeral=True)

        try:
            guild = self._require_guild(interaction)
            managed_ui_channels = await asyncio.to_thread(self._list_managed_ui_channels)
            missing_definitions = self._get_missing_required_managed_ui_definitions(
                managed_ui_channels
            )
            if not missing_definitions:
                await self._send_message(
                    interaction,
                    ADMIN_CLEANUP_UI_CHANNELS_EMPTY_MESSAGE,
                    ephemeral=True,
                )
                return

            managed_channel_ids = {
                managed_ui_channel.channel_id for managed_ui_channel in managed_ui_channels
            }
            blocking_channels = self._find_setup_blocking_unmanaged_channels(
                guild,
                missing_definitions=missing_definitions,
                managed_channel_ids=managed_channel_ids,
            )
            if not blocking_channels:
                await self._send_message(
                    interaction,
                    ADMIN_CLEANUP_UI_CHANNELS_EMPTY_MESSAGE,
                    ephemeral=True,
                )
                return

            missing_permissions = self._find_missing_managed_ui_teardown_permissions(guild)
            if missing_permissions:
                await self._send_message(
                    interaction,
                    self._format_managed_ui_permission_message(missing_permissions),
                    ephemeral=True,
                )
                return

            had_successful_cleanup = False
            had_forbidden_failure = False
            last_forbidden_error: discord.Forbidden | None = None
            had_other_failure = False
            for channel in blocking_channels:
                channel_id = getattr(channel, "id", None)
                channel_name = getattr(channel, "name", None)
                try:
                    await channel.delete(
                        reason="Cleanup unmanaged channel blocking admin_setup_ui_channels",
                    )
                except discord.NotFound:
                    had_successful_cleanup = True
                    continue
                except discord.Forbidden as exc:
                    had_forbidden_failure = True
                    last_forbidden_error = exc
                    self._log_managed_ui_forbidden(
                        action="admin_cleanup_ui_channels",
                        executor_discord_user_id=interaction.user.id,
                        channel_id=channel_id,
                        exc=exc,
                    )
                    continue
                except Exception:
                    had_other_failure = True
                    self.logger.exception(
                        "Failed to delete blocking unmanaged channel during cleanup "
                        "executor_discord_user_id=%s channel_id=%s channel_name=%s",
                        interaction.user.id,
                        channel_id,
                        channel_name,
                    )
                    continue

                had_successful_cleanup = True

            if had_forbidden_failure or had_other_failure:
                if had_forbidden_failure and not had_other_failure and not had_successful_cleanup:
                    await self._send_message(
                        interaction,
                        self._format_managed_ui_permission_message(
                            self._find_missing_managed_ui_teardown_permissions(guild),
                            forbidden_error=last_forbidden_error,
                        ),
                        ephemeral=True,
                    )
                    return

                await self._send_message(
                    interaction,
                    ADMIN_CLEANUP_UI_CHANNELS_FAILED_MESSAGE,
                    ephemeral=True,
                )
                return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_cleanup_ui_channels command executor_discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(
                interaction,
                ADMIN_CLEANUP_UI_CHANNELS_FAILED_MESSAGE,
                ephemeral=True,
            )
            return

        await self._send_message(
            interaction,
            ADMIN_CLEANUP_UI_CHANNELS_SUCCESS_MESSAGE,
            ephemeral=True,
        )

    async def admin_teardown_ui_channels(
        self,
        interaction: discord.Interaction[Any],
        confirm: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        if confirm != ADMIN_TEARDOWN_CONFIRM_VALUE:
            await self._send_message(
                interaction,
                ADMIN_INVALID_TEARDOWN_CONFIRM_MESSAGE,
                ephemeral=True,
            )
            return

        await self._defer_message_response(interaction, ephemeral=True)

        try:
            guild = self._require_guild(interaction)
            managed_ui_channels = await asyncio.to_thread(self._list_managed_ui_channels)
            if not managed_ui_channels:
                await self._send_message(
                    interaction,
                    ADMIN_TEARDOWN_UI_CHANNELS_EMPTY_MESSAGE,
                    ephemeral=True,
                )
                return

            missing_permissions = self._find_missing_managed_ui_teardown_permissions(guild)
            if missing_permissions:
                await self._send_message(
                    interaction,
                    self._format_managed_ui_permission_message(missing_permissions),
                    ephemeral=True,
                )
                return

            had_successful_cleanup = False
            had_forbidden_failure = False
            last_forbidden_error: discord.Forbidden | None = None
            had_other_failure = False
            for managed_ui_channel in managed_ui_channels:
                channel = self._find_guild_channel_by_id(guild, managed_ui_channel.channel_id)
                if channel is not None:
                    try:
                        await channel.delete(
                            reason=(
                                "Teardown managed UI channel "
                                f"for {managed_ui_channel.ui_type.value}"
                            ),
                        )
                    except discord.NotFound:
                        pass
                    except discord.Forbidden as exc:
                        had_forbidden_failure = True
                        last_forbidden_error = exc
                        self._log_managed_ui_forbidden(
                            action="admin_teardown_ui_channels",
                            executor_discord_user_id=interaction.user.id,
                            ui_type=managed_ui_channel.ui_type.value,
                            channel_id=managed_ui_channel.channel_id,
                            exc=exc,
                        )
                        continue
                    except Exception:
                        had_other_failure = True
                        self.logger.exception(
                            "Failed to delete managed UI channel during teardown "
                            "executor_discord_user_id=%s ui_type=%s channel_id=%s",
                            interaction.user.id,
                            managed_ui_channel.ui_type.value,
                            managed_ui_channel.channel_id,
                        )
                        continue

                try:
                    await asyncio.to_thread(
                        self._delete_managed_ui_channel_record,
                        managed_ui_channel.channel_id,
                    )
                except Exception:
                    had_other_failure = True
                    self.logger.exception(
                        "Failed to delete managed UI record during teardown "
                        "executor_discord_user_id=%s ui_type=%s channel_id=%s",
                        interaction.user.id,
                        managed_ui_channel.ui_type.value,
                        managed_ui_channel.channel_id,
                    )
                    continue

                had_successful_cleanup = True

            if had_forbidden_failure or had_other_failure:
                if had_forbidden_failure and not had_other_failure and not had_successful_cleanup:
                    await self._send_message(
                        interaction,
                        self._format_managed_ui_permission_message(
                            self._find_missing_managed_ui_teardown_permissions(guild),
                            forbidden_error=last_forbidden_error,
                        ),
                        ephemeral=True,
                    )
                    return

                await self._send_message(
                    interaction,
                    ADMIN_TEARDOWN_UI_CHANNELS_FAILED_MESSAGE,
                    ephemeral=True,
                )
                return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_teardown_ui_channels command executor_discord_user_id=%s",
                interaction.user.id,
            )
            await self._send_message(
                interaction,
                ADMIN_TEARDOWN_UI_CHANNELS_FAILED_MESSAGE,
                ephemeral=True,
            )
            return

        await self._send_message(
            interaction,
            ADMIN_TEARDOWN_UI_CHANNELS_SUCCESS_MESSAGE,
            ephemeral=True,
        )

    async def admin_add_penalty(
        self,
        interaction: discord.Interaction[Any],
        penalty_type: PenaltyType,
        *,
        target_user: DiscordUserLike | None = None,
        dummy_user: str | None = None,
    ) -> None:
        await self._run_admin_penalty(
            interaction=interaction,
            penalty_type=penalty_type,
            delta=1,
            success_message=ADMIN_PENALTY_ADD_SUCCESS_MESSAGE,
            target_user=target_user,
            dummy_user=dummy_user,
        )

    async def admin_sub_penalty(
        self,
        interaction: discord.Interaction[Any],
        penalty_type: PenaltyType,
        *,
        target_user: DiscordUserLike | None = None,
        dummy_user: str | None = None,
    ) -> None:
        await self._run_admin_penalty(
            interaction=interaction,
            penalty_type=penalty_type,
            delta=-1,
            success_message=ADMIN_PENALTY_SUB_SUCCESS_MESSAGE,
            target_user=target_user,
            dummy_user=dummy_user,
        )

    async def admin_restrict_user(
        self,
        interaction: discord.Interaction[Any],
        restriction_type: str,
        duration: str,
        *,
        target_user: DiscordUserLike | None = None,
        dummy_user: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            await self._sync_admin_target_user_identity(target_user)
            target_discord_user_id = self._resolve_admin_target_discord_user_id(
                target_user=target_user,
                dummy_user=dummy_user,
            )
            resolved_restriction_type = self._parse_restriction_type(restriction_type)
            resolved_duration = self._parse_restriction_duration(duration)
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_player_access_restriction_service()
            await asyncio.to_thread(
                service.restrict_player_access,
                player_id,
                resolved_restriction_type,
                resolved_duration,
                admin_discord_user_id=interaction.user.id,
                reason=reason,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_ADMIN_TARGET_USER_MESSAGE)
            return
        except InvalidPlayerAccessRestrictionTypeError:
            await self._send_message(interaction, INVALID_RESTRICTION_TYPE_MESSAGE)
            return
        except InvalidPlayerAccessRestrictionDurationError:
            await self._send_message(interaction, INVALID_RESTRICTION_DURATION_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, ADMIN_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except PlayerAccessRestrictionAlreadyExistsError:
            await self._send_message(interaction, ADMIN_RESTRICTION_ALREADY_EXISTS_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_restrict_user command "
                "executor_discord_user_id=%s target_discord_user_id=%s "
                "restriction_type=%s duration=%s",
                interaction.user.id,
                self._format_admin_target_for_log(target_user=target_user, dummy_user=dummy_user),
                restriction_type,
                duration,
            )
            await self._send_message(interaction, ADMIN_RESTRICTION_FAILED_MESSAGE)
            return

        await self._send_message(
            interaction,
            (
                f"指定したユーザーの"
                f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[resolved_restriction_type]}を"
                f"{PLAYER_ACCESS_RESTRICTION_DURATION_LABELS[resolved_duration]}制限しました。"
            ),
        )

    async def admin_unrestrict_user(
        self,
        interaction: discord.Interaction[Any],
        restriction_type: str,
        *,
        target_user: DiscordUserLike | None = None,
        dummy_user: str | None = None,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            await self._sync_admin_target_user_identity(target_user)
            target_discord_user_id = self._resolve_admin_target_discord_user_id(
                target_user=target_user,
                dummy_user=dummy_user,
            )
            resolved_restriction_type = self._parse_restriction_type(restriction_type)
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_player_access_restriction_service()
            await asyncio.to_thread(
                service.unrestrict_player_access,
                player_id,
                resolved_restriction_type,
                admin_discord_user_id=interaction.user.id,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_ADMIN_TARGET_USER_MESSAGE)
            return
        except InvalidPlayerAccessRestrictionTypeError:
            await self._send_message(interaction, INVALID_RESTRICTION_TYPE_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, ADMIN_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /admin_unrestrict_user command "
                "executor_discord_user_id=%s target_discord_user_id=%s restriction_type=%s",
                interaction.user.id,
                self._format_admin_target_for_log(target_user=target_user, dummy_user=dummy_user),
                restriction_type,
            )
            await self._send_message(interaction, ADMIN_UNRESTRICTION_FAILED_MESSAGE)
            return

        await self._send_message(
            interaction,
            (
                f"指定したユーザーの"
                f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[resolved_restriction_type]}制限を解除しました。"
            ),
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
        match_format: str,
        queue_name: str,
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        parent_channel: discord.abc.GuildChannel | None = None
        thread_id: int | None = None
        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            parent_channel = await self._resolve_required_matchmaking_presence_parent_channel(
                interaction
            )
            notification_context = await self._build_matchmaking_join_notification_context(
                interaction,
                mention_discord_user_id=target_discord_user_id,
                parent_channel=parent_channel,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_matching_queue_service()

            async def after_join(result: JoinQueueResult) -> None:
                nonlocal thread_id
                thread_id = await self._create_and_bind_matchmaking_presence_thread(
                    interaction,
                    queue_entry_id=result.queue_entry_id,
                    parent_channel=parent_channel,
                    initial_message=result.message,
                    target_discord_user_id=target_discord_user_id,
                    target_user=await self._resolve_presence_thread_target_user(
                        interaction,
                        target_discord_user_id,
                    ),
                    invite_target_user=not is_dummy_discord_user_id(target_discord_user_id),
                )

            await service.join_queue(
                player_id,
                match_format,
                queue_name,
                notification_context=notification_context,
                after_join=after_join,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except InvalidMatchFormatError:
            await self._send_message(interaction, "指定したフォーマットは存在しません。")
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
        except QueueJoinRestrictedError:
            await self._send_message(interaction, DEV_JOIN_RESTRICTED_MESSAGE)
            return
        except QueueAlreadyJoinedError:
            await self._send_message(interaction, DEV_JOIN_ALREADY_JOINED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_join command "
                "executor_discord_user_id=%s target_discord_user_id=%s "
                "match_format=%s queue_name=%s "
                "channel_id=%s guild_id=%s",
                interaction.user.id,
                discord_user_id,
                match_format,
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
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_matching_queue_service()
            result = await service.present(
                player_id,
                notification_context=None,
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

    async def dev_player_info_season(
        self,
        interaction: discord.Interaction[Any],
        season_id: int,
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_discord_user_id(discord_user_id)
            player_info = await asyncio.to_thread(
                self._lookup_player_info_by_season,
                target_discord_user_id,
                season_id,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, DEV_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except (SeasonNotFoundError, PlayerSeasonStatsNotFoundError) as exc:
            await self._send_message(interaction, str(exc))
            return
        except Exception:
            self.logger.exception(
                "Failed to execute /dev_player_info_season command "
                "executor_discord_user_id=%s target_discord_user_id=%s season_id=%s",
                interaction.user.id,
                discord_user_id,
                season_id,
            )
            await self._send_message(interaction, DEV_PLAYER_SEASON_INFO_FAILED_MESSAGE)
            return

        await self._send_message(
            interaction,
            self._format_player_info_message(player_info, include_season=True),
        )

    async def dev_match_parent(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
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

    async def dev_match_spectate(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            target_discord_user_id = self._parse_dummy_discord_user_id(discord_user_id)
        except ValueError:
            await self._send_message(interaction, INVALID_DISCORD_USER_ID_MESSAGE)
            return

        await self._run_match_spectate(
            interaction=interaction,
            match_id=match_id,
            executor_discord_user_id=target_discord_user_id,
            success_message=DEV_MATCH_SPECTATE_SUCCESS_MESSAGE,
            failure_message=DEV_MATCH_ACTION_FAILED_MESSAGE,
            ephemeral=False,
        )

    async def dev_match_win(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            match_id=match_id,
            discord_user_id=discord_user_id,
            input_result=MatchReportInputResult.WIN,
        )

    async def dev_match_lose(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            match_id=match_id,
            discord_user_id=discord_user_id,
            input_result=MatchReportInputResult.LOSE,
        )

    async def dev_match_draw(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            match_id=match_id,
            discord_user_id=discord_user_id,
            input_result=MatchReportInputResult.DRAW,
        )

    async def dev_match_void(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await self._run_dev_match_report(
            interaction=interaction,
            match_id=match_id,
            discord_user_id=discord_user_id,
            input_result=MatchReportInputResult.VOID,
        )

    async def dev_match_approve(
        self,
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
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
        await self._sync_requesting_user_identity(interaction)
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
        ephemeral: bool = False,
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
            await self._send_message(interaction, message, ephemeral=ephemeral)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc), ephemeral=ephemeral)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_parent command executor_discord_user_id=%s match_id=%s",
                executor_discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message, ephemeral=ephemeral)
            return

        await self._send_message(interaction, success_message, ephemeral=ephemeral)

    async def _run_match_spectate(
        self,
        *,
        interaction: discord.Interaction[Any],
        match_id: int,
        executor_discord_user_id: int,
        success_message: str | None,
        failure_message: str,
        ephemeral: bool,
    ) -> None:
        try:
            player_id = await asyncio.to_thread(self._lookup_player_id, executor_discord_user_id)
            service = self._require_match_service()
            result = await service.spectate_match(match_id, player_id)
        except PlayerNotRegisteredError:
            message = (
                PLAYER_REGISTRATION_REQUIRED_MESSAGE
                if executor_discord_user_id == interaction.user.id
                else DEV_TARGET_NOT_REGISTERED_MESSAGE
            )
            await self._send_message(interaction, message, ephemeral=ephemeral)
            return
        except MatchSpectatingRestrictedError:
            message = (
                MATCH_SPECTATE_RESTRICTED_MESSAGE
                if executor_discord_user_id == interaction.user.id
                else DEV_MATCH_SPECTATE_RESTRICTED_MESSAGE
            )
            await self._send_message(interaction, message, ephemeral=ephemeral)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc), ephemeral=ephemeral)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_spectate command executor_discord_user_id=%s match_id=%s",
                executor_discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message, ephemeral=ephemeral)
            return

        await self._best_effort_invite_match_operation_thread_user(
            interaction,
            match_id=result.match_id,
            target_discord_user_id=executor_discord_user_id,
        )

        if success_message is not None:
            await self._send_message(interaction, success_message, ephemeral=ephemeral)
            return

        await self._send_message(
            interaction,
            (
                "観戦応募を受け付けました。"
                f"現在 {result.active_spectator_count} / {result.max_spectators} 人です。"
            ),
            ephemeral=ephemeral,
        )

    async def _run_match_report(
        self,
        *,
        interaction: discord.Interaction[Any],
        match_id: int,
        executor_discord_user_id: int,
        input_result: MatchReportInputResult,
        success_message: str,
        failure_message: str,
        ephemeral: bool = False,
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
            await self._send_message(interaction, message, ephemeral=ephemeral)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc), ephemeral=ephemeral)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_report command "
                "executor_discord_user_id=%s match_id=%s input_result=%s",
                executor_discord_user_id,
                match_id,
                input_result.value,
            )
            await self._send_message(interaction, failure_message, ephemeral=ephemeral)
            return

        await self._send_message(interaction, success_message, ephemeral=ephemeral)

    async def _run_match_approve(
        self,
        *,
        interaction: discord.Interaction[Any],
        match_id: int,
        executor_discord_user_id: int,
        success_message: str,
        failure_message: str,
        ephemeral: bool = False,
    ) -> None:
        try:
            notification_context = self._build_notification_context(
                interaction,
                mention_discord_user_id=executor_discord_user_id,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, executor_discord_user_id)
            service = self._require_match_service()
            await service.approve_match_result(
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
            await self._send_message(interaction, message, ephemeral=ephemeral)
            return
        except MatchFlowError as exc:
            await self._send_message(interaction, str(exc), ephemeral=ephemeral)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute match_approve command executor_discord_user_id=%s match_id=%s",
                executor_discord_user_id,
                match_id,
            )
            await self._send_message(interaction, failure_message, ephemeral=ephemeral)
            return

        await self._send_message(interaction, success_message, ephemeral=ephemeral)

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
        penalty_type: PenaltyType,
        delta: int,
        success_message: str,
        target_user: DiscordUserLike | None = None,
        dummy_user: str | None = None,
    ) -> None:
        if not await self._ensure_admin(interaction):
            return

        try:
            await self._sync_admin_target_user_identity(target_user)
            target_discord_user_id = self._resolve_admin_target_discord_user_id(
                target_user=target_user,
                dummy_user=dummy_user,
            )
            player_id = await asyncio.to_thread(self._lookup_player_id, target_discord_user_id)
            service = self._require_match_service()
            await service.adjust_penalty(
                player_id,
                penalty_type,
                delta,
                admin_discord_user_id=interaction.user.id,
            )
        except ValueError:
            await self._send_message(interaction, INVALID_ADMIN_TARGET_USER_MESSAGE)
            return
        except PlayerNotRegisteredError:
            await self._send_message(interaction, ADMIN_TARGET_NOT_REGISTERED_MESSAGE)
            return
        except Exception:
            self.logger.exception(
                "Failed to execute admin penalty command executor_discord_user_id=%s "
                "target_discord_user_id=%s penalty_type=%s delta=%s",
                interaction.user.id,
                self._format_admin_target_for_log(target_user=target_user, dummy_user=dummy_user),
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

    def _lookup_player_info_by_season(self, discord_user_id: int, season_id: int) -> PlayerInfo:
        return self.player_lookup_service.get_player_info_by_discord_user_id_and_season_id(
            discord_user_id,
            season_id,
        )

    def _upsert_latest_info_thread_channel_id(
        self,
        player_id: int,
        thread_channel_id: int,
    ) -> None:
        self.info_thread_binding_service.upsert_latest_thread_channel_id(
            player_id=player_id,
            thread_channel_id=thread_channel_id,
        )

    def _rename_season(self, season_id: int, name: str) -> None:
        self.season_service.rename_season(season_id, name)

    def _list_managed_ui_channels(self) -> list[ManagedUiChannel]:
        return self.managed_ui_service.list_managed_ui_channels()

    def _get_managed_ui_channel_by_type(
        self,
        ui_type: ManagedUiType,
    ) -> ManagedUiChannel | None:
        return self.managed_ui_service.get_managed_ui_channel_by_type(ui_type)

    def _create_managed_ui_channel_record(
        self,
        ui_type: ManagedUiType,
        channel_id: int,
        message_id: int,
        created_by_discord_user_id: int,
    ) -> ManagedUiChannel:
        return self.managed_ui_service.create_managed_ui_channel(
            ui_type=ui_type,
            channel_id=channel_id,
            message_id=message_id,
            created_by_discord_user_id=created_by_discord_user_id,
        )

    def _delete_managed_ui_channel_record(self, channel_id: int) -> bool:
        return self.managed_ui_service.delete_managed_ui_channel_by_channel_id(channel_id)

    def _delete_managed_ui_channel_records(self, channel_ids: list[int]) -> int:
        return self.managed_ui_service.delete_managed_ui_channels_by_channel_ids(channel_ids)

    def _require_matching_queue_service(self) -> MatchingQueueCommandService:
        if self._matching_queue_service is None:
            raise RuntimeError("MatchingQueueService is not configured")
        return self._matching_queue_service

    def _require_match_service(self) -> MatchCommandService:
        if self._match_service is None:
            raise RuntimeError("MatchService is not configured")
        return self._match_service

    def _require_player_access_restriction_service(
        self,
    ) -> PlayerAccessRestrictionCommandService:
        return self._player_access_restriction_service

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

    def _build_player_operation_notification_context(
        self,
        interaction: discord.Interaction[Any],
        *,
        mention_discord_user_id: int | None = None,
        channel_id: int | None = None,
    ) -> MatchingQueueNotificationContext:
        resolved_channel_id = interaction.channel_id if channel_id is None else channel_id
        if resolved_channel_id is None:
            raise ValueError("interaction.channel_id is required")

        resolved_mention_discord_user_id = (
            interaction.user.id if mention_discord_user_id is None else mention_discord_user_id
        )

        return MatchingQueueNotificationContext(
            channel_id=resolved_channel_id,
            guild_id=interaction.guild_id,
            mention_discord_user_id=resolved_mention_discord_user_id,
        )

    async def _build_matchmaking_join_notification_context(
        self,
        interaction: discord.Interaction[Any],
        *,
        mention_discord_user_id: int | None = None,
        parent_channel: discord.abc.GuildChannel | None = None,
    ) -> MatchingQueueNotificationContext:
        resolved_parent_channel = parent_channel
        if resolved_parent_channel is None:
            resolved_parent_channel = (
                await self._resolve_required_matchmaking_presence_parent_channel(interaction)
            )
        parent_channel_id = getattr(resolved_parent_channel, "id", None)
        return self._build_player_operation_notification_context(
            interaction,
            mention_discord_user_id=mention_discord_user_id,
            channel_id=parent_channel_id if isinstance(parent_channel_id, int) else None,
        )

    async def _ensure_admin(self, interaction: discord.Interaction[Any]) -> bool:
        await self._sync_requesting_user_identity(interaction)
        if is_super_admin(interaction.user.id, self.settings):
            return True

        await self._send_message(interaction, ADMIN_ONLY_MESSAGE)
        return False

    async def _sync_requesting_user_identity(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        await asyncio.to_thread(self._best_effort_sync_discord_user, interaction.user)

    async def _sync_admin_target_user_identity(
        self,
        target_user: DiscordUserLike | None,
    ) -> None:
        if target_user is None:
            return

        await asyncio.to_thread(self._best_effort_sync_discord_user, target_user)

    def _best_effort_sync_discord_user(self, discord_user: DiscordUserLike | None) -> None:
        if discord_user is None:
            return

        discord_user_id = getattr(discord_user, "id", None)
        try:
            self.player_identity_service.sync_discord_user(discord_user)
        except Exception:
            self.logger.exception(
                "Failed to sync player identity cache discord_user_id=%s",
                discord_user_id,
            )

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

    def _parse_dummy_user_reference(self, value: str) -> int:
        match = DUMMY_USER_REFERENCE_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ValueError("dummy_user must be in <dummy_{dummy user id}> format")

        return self._parse_dummy_discord_user_id(match.group(1))

    def _resolve_admin_target_discord_user_id(
        self,
        *,
        target_user: DiscordUserLike | None,
        dummy_user: str | None,
    ) -> int:
        has_target_user = target_user is not None
        has_dummy_user = dummy_user is not None and dummy_user.strip() != ""
        if has_target_user == has_dummy_user:
            raise ValueError("exactly one of target_user or dummy_user must be provided")

        if target_user is not None:
            return target_user.id

        assert dummy_user is not None
        return self._parse_dummy_user_reference(dummy_user)

    def _format_admin_target_for_log(
        self,
        *,
        target_user: DiscordUserLike | None,
        dummy_user: str | None,
    ) -> str:
        if target_user is not None:
            return str(target_user.id)

        return repr(dummy_user)

    def _parse_match_result(self, value: str) -> MatchResult:
        return MatchResult(value)

    def _parse_managed_ui_type(self, value: str) -> ManagedUiType:
        return ManagedUiType(value)

    def _parse_info_thread_command_name(self, value: str) -> InfoThreadCommandName:
        return InfoThreadCommandName(value)

    def _parse_restriction_type(self, value: str) -> PlayerAccessRestrictionType:
        try:
            return PlayerAccessRestrictionType(value)
        except ValueError as exc:
            raise InvalidPlayerAccessRestrictionTypeError(
                f"Invalid restriction_type: {value}"
            ) from exc

    def _parse_restriction_duration(self, value: str) -> PlayerAccessRestrictionDuration:
        try:
            return PlayerAccessRestrictionDuration(value)
        except ValueError as exc:
            raise InvalidPlayerAccessRestrictionDurationError(f"Invalid duration: {value}") from exc

    def _format_player_info_message(
        self,
        player_info: PlayerInfo,
        *,
        include_season: bool = False,
    ) -> str:
        lines = ["プレイヤー情報"]
        if include_season:
            lines.extend(
                [
                    f"season_id: {player_info.season.season_id}",
                    f"season_name: {player_info.season.name}",
                ]
            )
        for format_stats in player_info.format_stats:
            last_played_at = (
                "-"
                if format_stats.last_played_at is None
                else format_stats.last_played_at.isoformat()
            )
            lines.extend(
                [
                    format_stats.match_format.value,
                    f"rating: {format_stats.rating:.2f}",
                    f"games_played: {format_stats.games_played}",
                    f"wins: {format_stats.wins}",
                    f"losses: {format_stats.losses}",
                    f"draws: {format_stats.draws}",
                    f"last_played_at: {last_played_at}",
                ]
            )
        return "\n".join(lines)

    def _build_matchmaking_presence_thread_name(
        self,
        *,
        discord_user_id: int,
        discord_user: DiscordUserLike | None = None,
    ) -> str:
        display_name = resolve_player_display_name(
            discord_user_id=discord_user_id,
            guild_display_name=getattr(discord_user, "nick", None),
            global_display_name=getattr(discord_user, "global_name", None),
            username=getattr(discord_user, "name", None),
        )
        suffix = str(discord_user_id) if display_name is None else display_name
        return f"{MATCHMAKING_PRESENCE_THREAD_NAME_PREFIX}{suffix}"[:MAX_DISCORD_THREAD_NAME_LENGTH]

    def _build_info_thread_name(
        self,
        *,
        discord_user_id: int,
        discord_user: DiscordUserLike | None = None,
    ) -> str:
        display_name = resolve_player_display_name(
            discord_user_id=discord_user_id,
            guild_display_name=getattr(discord_user, "nick", None),
            global_display_name=getattr(discord_user, "global_name", None),
            username=getattr(discord_user, "name", None),
        )
        suffix = str(discord_user_id) if display_name is None else display_name
        return f"{INFO_THREAD_NAME_PREFIX}{suffix}"[:MAX_DISCORD_THREAD_NAME_LENGTH]

    def _build_match_operation_thread_name(self, match_id: int) -> str:
        return f"試合-{match_id}"

    def _format_matchmaking_join_success_message(
        self,
        base_message: str,
        *,
        thread_id: int | None,
    ) -> str:
        if thread_id is None:
            return base_message

        return "\n".join(
            [
                base_message,
                MATCHMAKING_PRESENCE_THREAD_GUIDE_MESSAGE.format(thread_mention=f"<#{thread_id}>"),
            ]
        )

    async def _validate_matchmaking_presence_thread_binding(
        self,
        interaction: discord.Interaction[Any],
        player_id: int,
    ) -> bool:
        if interaction.channel_id is None:
            await self._send_player_operation_message(
                interaction,
                MATCHMAKING_PRESENCE_THREAD_MISMATCH_MESSAGE,
            )
            return False

        service = self._require_matching_queue_service()
        waiting_entry_notification_channel_id = (
            await service.get_waiting_entry_notification_channel_id(player_id)
        )
        if waiting_entry_notification_channel_id is None:
            await self._send_player_operation_message(
                interaction,
                MATCHMAKING_PRESENCE_THREAD_NOT_JOINED_MESSAGE,
            )
            return False

        if waiting_entry_notification_channel_id != interaction.channel_id:
            await self._send_player_operation_message(
                interaction,
                MATCHMAKING_PRESENCE_THREAD_MISMATCH_MESSAGE,
            )
            return False

        return True

    def _require_guild(self, interaction: discord.Interaction[Any]) -> discord.Guild:
        guild = interaction.guild
        if guild is None:
            raise ValueError("interaction.guild is required")
        return guild

    def _guild_has_channel_named(self, guild: discord.Guild, channel_name: str) -> bool:
        return any(
            getattr(channel, "name", None) == channel_name
            for channel in getattr(guild, "channels", ())
        )

    def _get_missing_required_managed_ui_definitions(
        self,
        managed_ui_channels: Sequence[ManagedUiChannel],
    ) -> list[ManagedUiDefinition]:
        managed_ui_channel_by_type = {
            managed_ui_channel.ui_type: managed_ui_channel
            for managed_ui_channel in managed_ui_channels
        }
        return [
            definition
            for definition in get_required_managed_ui_definitions()
            if definition.ui_type not in managed_ui_channel_by_type
        ]

    def _find_setup_blocking_unmanaged_channels(
        self,
        guild: discord.Guild,
        *,
        missing_definitions: Sequence[ManagedUiDefinition],
        managed_channel_ids: set[int],
    ) -> list[discord.abc.GuildChannel]:
        blocking_channel_names = {
            definition.recommended_channel_name for definition in missing_definitions
        }
        if not blocking_channel_names:
            return []

        blocking_channels_by_id: dict[int, discord.abc.GuildChannel] = {}
        for channel in getattr(guild, "channels", ()):
            channel_id = getattr(channel, "id", None)
            channel_name = getattr(channel, "name", None)
            if not isinstance(channel_id, int):
                continue
            if channel_id in managed_channel_ids:
                continue
            if channel_name not in blocking_channel_names:
                continue

            blocking_channels_by_id[channel_id] = cast(discord.abc.GuildChannel, channel)

        return list(blocking_channels_by_id.values())

    def _find_guild_channel_by_id(
        self,
        guild: discord.Guild,
        channel_id: int,
    ) -> discord.abc.GuildChannel | None:
        get_channel = getattr(guild, "get_channel", None)
        if callable(get_channel):
            channel = get_channel(channel_id)
            if channel is not None:
                return cast(discord.abc.GuildChannel, channel)

        for channel in getattr(guild, "channels", ()):
            if getattr(channel, "id", None) == channel_id:
                return cast(discord.abc.GuildChannel, channel)
        return None

    def _find_guild_channel_by_name(
        self,
        guild: discord.Guild,
        channel_name: str,
    ) -> discord.abc.GuildChannel | None:
        for channel in getattr(guild, "channels", ()):
            if getattr(channel, "name", None) == channel_name:
                return cast(discord.abc.GuildChannel, channel)
        return None

    def _find_guild_role_by_name(
        self,
        guild: discord.Guild,
        role_name: str,
    ) -> discord.Role | None:
        for role in getattr(guild, "roles", ()):
            if getattr(role, "name", None) == role_name:
                return cast(discord.Role, role)
        return None

    def _find_missing_managed_ui_setup_permissions(
        self,
        guild: discord.Guild,
        definitions: Sequence[ManagedUiDefinition],
    ) -> tuple[str, ...]:
        guild_permissions = self._get_bot_guild_permissions(guild)
        if guild_permissions is None:
            return ()

        missing_permissions: list[str] = []
        if not guild_permissions.manage_channels:
            missing_permissions.append(MANAGED_UI_PERMISSION_LABEL_MANAGE_CHANNELS)

        requires_registered_player_role = any(
            definition.requires_registered_player_role for definition in definitions
        )
        registered_player_role = self._find_guild_role_by_name(guild, REGISTERED_PLAYER_ROLE_NAME)
        if (
            requires_registered_player_role
            and registered_player_role is None
            and not guild_permissions.manage_roles
        ):
            missing_permissions.append(MANAGED_UI_PERMISSION_LABEL_MANAGE_ROLES)

        requires_matchmaking_thread_permissions = any(
            definition.ui_type is ManagedUiType.MATCHMAKING_CHANNEL for definition in definitions
        )
        if requires_matchmaking_thread_permissions:
            if not guild_permissions.create_private_threads:
                missing_permissions.append(MANAGED_UI_PERMISSION_LABEL_CREATE_PRIVATE_THREADS)
            if not guild_permissions.send_messages_in_threads:
                missing_permissions.append(MANAGED_UI_PERMISSION_LABEL_SEND_MESSAGES_IN_THREADS)

        return tuple(missing_permissions)

    def _find_missing_managed_ui_teardown_permissions(
        self,
        guild: discord.Guild,
    ) -> tuple[str, ...]:
        guild_permissions = self._get_bot_guild_permissions(guild)
        if guild_permissions is None:
            return ()

        if guild_permissions.manage_channels:
            return ()

        return (MANAGED_UI_PERMISSION_LABEL_MANAGE_CHANNELS,)

    def _get_bot_guild_permissions(self, guild: discord.Guild) -> discord.Permissions | None:
        bot_member = guild.me
        if bot_member is None:
            return None

        guild_permissions = getattr(bot_member, "guild_permissions", None)
        if not isinstance(guild_permissions, discord.Permissions):
            return None

        return guild_permissions

    def _format_discord_forbidden_detail(
        self,
        exc: discord.Forbidden,
    ) -> str:
        reason = getattr(exc.response, "reason", "Forbidden")
        normalized_text = self._normalize_discord_http_exception_text(exc.text)
        detail = f"Discord API: {exc.status} {reason} (error code: {exc.code})"
        if not normalized_text:
            return detail

        return f"{detail}: {normalized_text}"

    def _normalize_discord_http_exception_text(self, text: str) -> str:
        return " ".join(text.splitlines()).strip()

    def _log_managed_ui_forbidden(
        self,
        *,
        action: str,
        executor_discord_user_id: int,
        exc: discord.Forbidden,
        ui_type: str | None = None,
        channel_id: int | None = None,
    ) -> None:
        self.logger.warning(
            "Discord forbidden during managed UI operation action=%s "
            "executor_discord_user_id=%s ui_type=%s channel_id=%s "
            "status=%s code=%s reason=%s text=%s",
            action,
            executor_discord_user_id,
            ui_type,
            channel_id,
            exc.status,
            exc.code,
            getattr(exc.response, "reason", None),
            self._normalize_discord_http_exception_text(exc.text),
        )

    def _format_managed_ui_permission_message(
        self,
        missing_permissions: Sequence[str] = (),
        *,
        forbidden_error: discord.Forbidden | None = None,
    ) -> str:
        parts = [ADMIN_MANAGED_UI_PERMISSION_MESSAGE]
        if missing_permissions:
            parts.append(f"不足している権限: {', '.join(missing_permissions)}")
        if forbidden_error is not None:
            parts.append(self._format_discord_forbidden_detail(forbidden_error))

        return " ".join(parts)

    async def _ensure_registered_player_role(
        self,
        guild: discord.Guild,
    ) -> discord.Role:
        existing_role = self._find_guild_role_by_name(guild, REGISTERED_PLAYER_ROLE_NAME)
        if existing_role is not None:
            return existing_role

        created_role = await guild.create_role(
            name=REGISTERED_PLAYER_ROLE_NAME,
            mentionable=False,
            reason="Create registered player role for managed UI channels",
        )
        return cast(discord.Role, created_role)

    async def _best_effort_assign_registered_player_role(
        self,
        interaction: discord.Interaction[Any],
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        role = self._find_guild_role_by_name(guild, REGISTERED_PLAYER_ROLE_NAME)
        if role is None:
            return

        member = interaction.user
        add_roles = getattr(member, "add_roles", None)
        if not callable(add_roles):
            return

        member_roles = getattr(member, "roles", ())
        if any(getattr(existing_role, "id", None) == role.id for existing_role in member_roles):
            return

        try:
            await add_roles(
                role,
                reason="Grant registered player role after successful registration",
            )
        except Exception:
            self.logger.exception(
                "Failed to grant registered player role discord_user_id=%s guild_id=%s role_id=%s",
                interaction.user.id,
                interaction.guild_id,
                role.id,
            )

    async def _best_effort_create_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[Any],
        *,
        parent_channel: discord.abc.GuildChannel | None,
        initial_message: str,
        target_discord_user_id: int,
        target_user: DiscordUserLike | None,
        invite_target_user: bool,
    ) -> int | None:
        try:
            guild = self._require_guild(interaction)
            resolved_parent_channel = parent_channel
            if resolved_parent_channel is None:
                resolved_parent_channel = (
                    await self._resolve_required_matchmaking_presence_parent_channel(interaction)
                )

            create_thread = getattr(resolved_parent_channel, "create_thread", None)
            if not callable(create_thread):
                raise TypeError(
                    "channel_id="
                    f"{getattr(resolved_parent_channel, 'id', None)} "
                    "does not support thread creation"
                )

            thread = await create_thread(
                name=self._build_matchmaking_presence_thread_name(
                    discord_user_id=target_discord_user_id,
                    discord_user=target_user,
                ),
                type=discord.ChannelType.private_thread,
                invitable=False,
                reason="Create matchmaking presence thread "
                f"for discord_user_id={target_discord_user_id}",
            )

            add_user = getattr(thread, "add_user", None)
            if callable(add_user):
                invitees: list[DiscordUserLike] = []
                if invite_target_user and target_user is not None:
                    invitees.append(target_user)
                invitees.extend(await self._resolve_admin_presence_thread_users(interaction, guild))
                for invitee in self._dedupe_discord_users(invitees):
                    await add_user(invitee)

            await thread.send(
                initial_message,
                view=create_matchmaking_presence_thread_view(self),
            )
            thread_id = getattr(thread, "id", None)
            if isinstance(thread_id, int):
                return thread_id
        except Exception:
            self.logger.exception(
                "Failed to create matchmaking presence thread discord_user_id=%s "
                "channel_id=%s guild_id=%s",
                target_discord_user_id,
                interaction.channel_id,
                interaction.guild_id,
            )
        return None

    async def _create_info_thread(
        self,
        interaction: discord.Interaction[Any],
        *,
        parent_channel: discord.abc.GuildChannel,
        command_name: InfoThreadCommandName,
        target_discord_user_id: int,
        target_user: DiscordUserLike | None,
    ) -> object:
        guild = self._require_guild(interaction)
        create_thread = getattr(parent_channel, "create_thread", None)
        if not callable(create_thread):
            raise TypeError(
                f"channel_id={getattr(parent_channel, 'id', None)} does not support thread creation"
            )

        thread = await create_thread(
            name=self._build_info_thread_name(
                discord_user_id=target_discord_user_id,
                discord_user=target_user,
            ),
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Create info thread for discord_user_id={target_discord_user_id}",
        )

        add_user = getattr(thread, "add_user", None)
        if callable(add_user):
            invitees: list[DiscordUserLike] = []
            if target_user is not None:
                invitees.append(target_user)
            invitees.extend(await self._resolve_admin_presence_thread_users(interaction, guild))
            for invitee in self._dedupe_discord_users(invitees):
                await add_user(invitee)

        await cast(Any, thread).send(build_info_thread_initial_message(command_name))
        self._require_discord_channel_id(thread)
        return thread

    async def _best_effort_delete_info_thread(
        self,
        thread: object,
        *,
        reason: str,
    ) -> None:
        delete = getattr(thread, "delete", None)
        if not callable(delete):
            return

        thread_id = getattr(thread, "id", None)
        try:
            await delete(reason=reason)
        except Exception:
            self.logger.exception(
                "Failed to cleanup info thread thread_id=%s reason=%s",
                thread_id,
                reason,
            )

    async def _create_and_bind_matchmaking_presence_thread(
        self,
        interaction: discord.Interaction[Any],
        *,
        queue_entry_id: int,
        parent_channel: discord.abc.GuildChannel | None,
        initial_message: str,
        target_discord_user_id: int,
        target_user: DiscordUserLike | None,
        invite_target_user: bool,
    ) -> int | None:
        thread_id = await self._best_effort_create_matchmaking_presence_thread(
            interaction,
            parent_channel=parent_channel,
            initial_message=initial_message,
            target_discord_user_id=target_discord_user_id,
            target_user=target_user,
            invite_target_user=invite_target_user,
        )
        if thread_id is None:
            return None

        service = self._require_matching_queue_service()
        updated = await service.update_waiting_presence_thread_channel_id(
            queue_entry_id,
            thread_id,
        )
        if not updated:
            self.logger.warning(
                "Failed to bind matchmaking presence thread queue_entry_id=%s "
                "thread_id=%s discord_user_id=%s",
                queue_entry_id,
                thread_id,
                target_discord_user_id,
            )
        return thread_id

    async def _best_effort_invite_match_operation_thread_user(
        self,
        interaction: discord.Interaction[Any],
        *,
        match_id: int,
        target_discord_user_id: int,
    ) -> None:
        if is_dummy_discord_user_id(target_discord_user_id):
            return

        try:
            target_user = await self._resolve_presence_thread_target_user(
                interaction,
                target_discord_user_id,
            )
            if target_user is None:
                return

            thread = await self._resolve_match_operation_thread(
                interaction,
                match_id=match_id,
            )
            if thread is None:
                return

            add_user = getattr(thread, "add_user", None)
            if not callable(add_user):
                return

            await add_user(target_user)
        except Exception:
            self.logger.exception(
                "Failed to invite user to match operation thread "
                "discord_user_id=%s match_id=%s guild_id=%s",
                target_discord_user_id,
                match_id,
                interaction.guild_id,
            )

    async def _resolve_match_operation_thread(
        self,
        interaction: discord.Interaction[Any],
        *,
        match_id: int,
    ) -> object | None:
        parent_channel = await self._resolve_matchmaking_presence_parent_channel(interaction)
        if parent_channel is None:
            return None

        thread_name = self._build_match_operation_thread_name(match_id)
        parent_channel_id = getattr(parent_channel, "id", None)
        for candidate in self._iter_match_operation_thread_candidates(parent_channel):
            if getattr(candidate, "name", None) != thread_name:
                continue

            candidate_parent = getattr(candidate, "parent", None)
            candidate_parent_id = getattr(candidate_parent, "id", None)
            if (
                isinstance(parent_channel_id, int)
                and isinstance(candidate_parent_id, int)
                and candidate_parent_id != parent_channel_id
            ):
                continue
            if (
                candidate_parent is not None
                and candidate_parent_id is None
                and candidate_parent is not parent_channel
            ):
                continue

            return candidate

        return None

    def _iter_match_operation_thread_candidates(
        self,
        parent_channel: discord.abc.GuildChannel,
    ) -> Iterable[object]:
        for attribute_name in ("created_threads", "threads"):
            candidates = getattr(parent_channel, attribute_name, None)
            if isinstance(candidates, list | tuple):
                yield from candidates

        guild = getattr(parent_channel, "guild", None)
        guild_threads = getattr(guild, "threads", None)
        if isinstance(guild_threads, list | tuple):
            yield from guild_threads

    async def _resolve_matchmaking_presence_parent_channel(
        self,
        interaction: discord.Interaction[Any],
    ) -> discord.abc.GuildChannel | None:
        guild = interaction.guild
        if guild is None:
            return None

        managed_ui_channel = await asyncio.to_thread(
            self._get_managed_ui_channel_by_type,
            ManagedUiType.MATCHMAKING_CHANNEL,
        )
        if managed_ui_channel is not None:
            channel = self._find_guild_channel_by_id(guild, managed_ui_channel.channel_id)
            if channel is not None and callable(getattr(channel, "create_thread", None)):
                return channel

        definition = get_managed_ui_definition(ManagedUiType.MATCHMAKING_CHANNEL)
        channel = self._find_guild_channel_by_name(guild, definition.recommended_channel_name)
        if channel is not None and callable(getattr(channel, "create_thread", None)):
            return channel

        return None

    async def _resolve_required_matchmaking_presence_parent_channel(
        self,
        interaction: discord.Interaction[Any],
    ) -> discord.abc.GuildChannel:
        guild = interaction.guild
        if guild is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
                reason="interaction guild is unavailable",
            )
        managed_ui_channel = await asyncio.to_thread(
            self._get_managed_ui_channel_by_type,
            ManagedUiType.MATCHMAKING_CHANNEL,
        )
        if managed_ui_channel is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
                reason="managed UI channel is not setup",
            )

        channel = self._find_guild_channel_by_id(guild, managed_ui_channel.channel_id)
        if channel is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
                reason="managed UI channel is missing from guild",
                channel_id=managed_ui_channel.channel_id,
            )

        if not callable(getattr(channel, "create_thread", None)):
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.MATCHMAKING_CHANNEL,
                reason="managed UI channel does not support thread creation",
                channel_id=managed_ui_channel.channel_id,
            )

        return channel

    async def _resolve_required_info_thread_parent_channel(
        self,
        interaction: discord.Interaction[Any],
    ) -> discord.abc.GuildChannel:
        guild = interaction.guild
        if guild is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.INFO_CHANNEL,
                reason="interaction guild is unavailable",
            )

        managed_ui_channel = await asyncio.to_thread(
            self._get_managed_ui_channel_by_type,
            ManagedUiType.INFO_CHANNEL,
        )
        if managed_ui_channel is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.INFO_CHANNEL,
                reason="managed UI channel is not setup",
            )

        channel = self._find_guild_channel_by_id(guild, managed_ui_channel.channel_id)
        if channel is None:
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.INFO_CHANNEL,
                reason="managed UI channel is missing from guild",
                channel_id=managed_ui_channel.channel_id,
            )

        if not callable(getattr(channel, "create_thread", None)):
            raise RequiredManagedUiChannelUnavailableError(
                ui_type=ManagedUiType.INFO_CHANNEL,
                reason="managed UI channel does not support thread creation",
                channel_id=managed_ui_channel.channel_id,
            )

        return channel

    async def _resolve_presence_thread_target_user(
        self,
        interaction: discord.Interaction[Any],
        discord_user_id: int,
    ) -> DiscordUserLike | None:
        if is_dummy_discord_user_id(discord_user_id):
            return None

        if interaction.user.id == discord_user_id:
            return interaction.user

        guild = interaction.guild
        if guild is None:
            return None

        target_user = await self._resolve_guild_member(guild, discord_user_id)
        await self._sync_admin_target_user_identity(target_user)
        return target_user

    async def _resolve_admin_presence_thread_users(
        self,
        interaction: discord.Interaction[Any],
        guild: discord.Guild,
    ) -> list[DiscordUserLike]:
        admin_users: list[DiscordUserLike] = []
        for admin_discord_user_id in sorted(self.settings.super_admin_user_ids):
            if admin_discord_user_id == interaction.user.id:
                admin_users.append(interaction.user)
                continue

            admin_user = await self._resolve_guild_member(guild, admin_discord_user_id)
            if admin_user is not None:
                admin_users.append(admin_user)

        return admin_users

    async def _resolve_guild_member(
        self,
        guild: discord.Guild,
        discord_user_id: int,
    ) -> DiscordUserLike | None:
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            member = get_member(discord_user_id)
            if member is not None:
                return cast(DiscordUserLike, member)

        fetch_member = getattr(guild, "fetch_member", None)
        if callable(fetch_member):
            try:
                member = await fetch_member(discord_user_id)
            except Exception:
                self.logger.warning(
                    "Failed to resolve guild member for presence thread "
                    "discord_user_id=%s guild_id=%s",
                    discord_user_id,
                    guild.id,
                )
            else:
                return cast(DiscordUserLike, member)

        return None

    def _dedupe_discord_users(
        self,
        users: Sequence[DiscordUserLike],
    ) -> list[DiscordUserLike]:
        deduped_users: list[DiscordUserLike] = []
        seen_user_ids: set[int] = set()
        for user in users:
            user_id = getattr(user, "id", None)
            if not isinstance(user_id, int) or user_id in seen_user_ids:
                continue
            deduped_users.append(user)
            seen_user_ids.add(user_id)
        return deduped_users

    def _require_discord_channel_id(self, channel: object) -> int:
        channel_id = getattr(channel, "id", None)
        if not isinstance(channel_id, int):
            raise TypeError(f"Discord channel id is unavailable: {channel!r}")
        return channel_id

    async def _provision_managed_ui_channel(
        self,
        *,
        guild: discord.Guild,
        definition: ManagedUiDefinition,
        channel_name: str,
        created_by_discord_user_id: int,
        private_channel: bool = False,
        visible_member: discord.abc.Snowflake | None = None,
    ) -> ProvisionedManagedUiChannel:
        registered_player_role = None
        if definition.requires_registered_player_role:
            registered_player_role = await self._ensure_registered_player_role(guild)

        channel = await guild.create_text_channel(
            channel_name,
            overwrites=cast(
                Any,
                build_managed_ui_channel_overwrites(
                    guild,
                    definition.ui_type,
                    registered_player_role=registered_player_role,
                    private_channel=private_channel,
                    visible_member=visible_member,
                ),
            ),
            reason=f"Create managed UI channel for {definition.ui_type.value}",
        )
        provisioned_channel = ProvisionedManagedUiChannel(
            definition=definition,
            channel=channel,
        )
        try:
            message = await send_initial_managed_ui_message(
                cast(discord.TextChannel, channel),
                ui_type=definition.ui_type,
                interaction_handler=self,
            )
            await asyncio.to_thread(
                self._create_managed_ui_channel_record,
                definition.ui_type,
                channel.id,
                message.id,
                created_by_discord_user_id,
            )
        except Exception as exc:
            raise ManagedUiProvisioningError(provisioned_channel) from exc

        return provisioned_channel

    async def _rollback_provisioned_managed_ui_channels(
        self,
        provisioned_channels: list[ProvisionedManagedUiChannel],
        *,
        log_context: str,
    ) -> bool:
        if not provisioned_channels:
            return True

        rollback_succeeded = True
        deleted_or_missing_channel_ids: list[int] = []
        for provisioned_channel in reversed(provisioned_channels):
            try:
                await provisioned_channel.channel.delete(
                    reason=(
                        "Rollback managed UI channel creation "
                        f"for {provisioned_channel.definition.ui_type.value}"
                    ),
                )
            except discord.NotFound:
                deleted_or_missing_channel_ids.append(provisioned_channel.channel.id)
            except Exception:
                rollback_succeeded = False
                self.logger.exception(
                    "Failed to rollback managed UI channel creation %s ui_type=%s channel_id=%s",
                    log_context,
                    provisioned_channel.definition.ui_type.value,
                    provisioned_channel.channel.id,
                )
            else:
                deleted_or_missing_channel_ids.append(provisioned_channel.channel.id)

        if deleted_or_missing_channel_ids:
            try:
                await asyncio.to_thread(
                    self._delete_managed_ui_channel_records,
                    deleted_or_missing_channel_ids,
                )
            except Exception:
                rollback_succeeded = False
                self.logger.exception(
                    "Failed to rollback managed UI records %s channel_ids=%s",
                    log_context,
                    deleted_or_missing_channel_ids,
                )

        return rollback_succeeded

    async def _send_message(
        self,
        interaction: discord.Interaction[Any],
        message: str,
        *,
        ephemeral: bool = False,
    ) -> None:
        response = interaction.response
        is_done = getattr(response, "is_done", None)
        if callable(is_done) and is_done():
            await interaction.followup.send(message, ephemeral=ephemeral)
            return

        await response.send_message(message, ephemeral=ephemeral)

    async def _defer_message_response(
        self,
        interaction: discord.Interaction[Any],
        *,
        ephemeral: bool,
    ) -> None:
        response = interaction.response
        is_done = getattr(response, "is_done", None)
        if callable(is_done) and is_done():
            return

        await response.defer(ephemeral=ephemeral, thinking=True)

    async def _send_player_operation_message(
        self,
        interaction: discord.Interaction[Any],
        message: str,
    ) -> None:
        await self._send_message(interaction, message, ephemeral=True)


def register_app_commands(
    tree: app_commands.CommandTree[Any],
    handlers: BotCommandHandlers,
) -> None:
    match_format_choices = [
        app_commands.Choice(name=match_format, value=match_format)
        for match_format in MATCH_FORMAT_CHOICES
    ]
    queue_name_choices = [
        app_commands.Choice(name=queue_name, value=queue_name)
        for queue_name in MATCH_QUEUE_NAME_CHOICES
    ]
    restriction_type_choices = [
        app_commands.Choice(
            name=PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[restriction_type],
            value=restriction_type.value,
        )
        for restriction_type in (
            PlayerAccessRestrictionType.QUEUE_JOIN,
            PlayerAccessRestrictionType.SPECTATE,
        )
    ]
    restriction_duration_choices = [
        app_commands.Choice(
            name=PLAYER_ACCESS_RESTRICTION_DURATION_LABELS[duration],
            value=duration.value,
        )
        for duration in (
            PlayerAccessRestrictionDuration.ONE_DAY,
            PlayerAccessRestrictionDuration.THREE_DAYS,
            PlayerAccessRestrictionDuration.SEVEN_DAYS,
            PlayerAccessRestrictionDuration.FOURTEEN_DAYS,
            PlayerAccessRestrictionDuration.TWENTY_EIGHT_DAYS,
            PlayerAccessRestrictionDuration.FIFTY_SIX_DAYS,
            PlayerAccessRestrictionDuration.EIGHTY_FOUR_DAYS,
            PlayerAccessRestrictionDuration.PERMANENT,
        )
    ]
    managed_ui_type_choices = [
        app_commands.Choice(name=definition.ui_type.value, value=definition.ui_type.value)
        for definition in get_required_managed_ui_definitions()
    ]
    info_thread_command_choices = [
        app_commands.Choice(name=command_name.value, value=command_name.value)
        for command_name in InfoThreadCommandName
    ]

    @tree.command(name="register", description="プレイヤー登録を行います")
    async def register_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.register(interaction)

    @tree.command(name="join", description="マッチングキューに参加します")
    @app_commands.describe(match_format="参加したいフォーマット", queue_name="参加したいキュー名")
    @app_commands.choices(match_format=match_format_choices)
    @app_commands.choices(queue_name=queue_name_choices)
    async def join_command(
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
    ) -> None:
        await handlers.join(interaction, match_format, queue_name)

    @tree.command(name="present", description="在席を更新して期限を延長します")
    async def present_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.present(interaction)

    @tree.command(name="leave", description="マッチングキューから退出します")
    async def leave_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.leave(interaction)

    @tree.command(name="player_info", description="自分のプレイヤー情報を表示します")
    async def player_info_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.player_info(interaction)

    @tree.command(name="info_thread", description="情報確認用スレッドを作成します")
    @app_commands.describe(command_name="作成したい情報確認スレッドの用途")
    @app_commands.choices(command_name=info_thread_command_choices)
    async def info_thread_command(
        interaction: discord.Interaction[Any],
        command_name: str,
    ) -> None:
        await handlers.info_thread(interaction, command_name)

    @tree.command(
        name="player_info_season",
        description="指定したシーズンの自分のプレイヤー情報を表示します",
    )
    @app_commands.describe(season_id="対象の season_id")
    async def player_info_season_command(
        interaction: discord.Interaction[Any],
        season_id: int,
    ) -> None:
        await handlers.player_info_season(interaction, season_id)

    @tree.command(name="match_parent", description="試合の親に立候補します")
    @app_commands.describe(match_id="対象の match_id")
    async def match_parent_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_parent(interaction, match_id)

    @tree.command(name="match_spectate", description="試合の観戦応募を行います")
    @app_commands.describe(match_id="対象の match_id")
    async def match_spectate_command(
        interaction: discord.Interaction[Any],
        match_id: int,
    ) -> None:
        await handlers.match_spectate(interaction, match_id)

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

    @tree.command(name="admin_rename_season", description="シーズン名を変更します")
    @app_commands.describe(season_id="対象の season_id", name="新しいシーズン名")
    async def admin_rename_season_command(
        interaction: discord.Interaction[Any],
        season_id: int,
        name: str,
    ) -> None:
        await handlers.admin_rename_season(interaction, season_id, name)

    @tree.command(
        name="admin_setup_custom_ui_channel",
        description="指定した UI 設置チャンネルを作成します",
    )
    @app_commands.describe(
        ui_type="設置したい UI の種別",
        channel_name="作成するチャンネル名",
    )
    @app_commands.choices(ui_type=managed_ui_type_choices)
    async def admin_setup_custom_ui_channel_command(
        interaction: discord.Interaction[Any],
        ui_type: str,
        channel_name: str,
    ) -> None:
        await handlers.admin_setup_custom_ui_channel(interaction, ui_type, channel_name)

    @tree.command(
        name="admin_setup_ui_channels",
        description="必要な UI 設置チャンネルをまとめて作成します",
    )
    async def admin_setup_ui_channels_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.admin_setup_ui_channels(interaction)

    @tree.command(
        name="admin_cleanup_ui_channels",
        description="setup の障害となる重複チャンネルを削除します",
    )
    @app_commands.describe(confirm="cleanup する場合は cleanup を入力")
    async def admin_cleanup_ui_channels_command(
        interaction: discord.Interaction[Any],
        confirm: str,
    ) -> None:
        await handlers.admin_cleanup_ui_channels(interaction, confirm)

    @tree.command(
        name="admin_teardown_ui_channels",
        description="管理対象の UI 設置チャンネルをまとめて撤収します",
    )
    @app_commands.describe(confirm="撤収する場合は teardown を入力")
    async def admin_teardown_ui_channels_command(
        interaction: discord.Interaction[Any],
        confirm: str,
    ) -> None:
        await handlers.admin_teardown_ui_channels(interaction, confirm)

    @tree.command(name="admin_restrict_user", description="ユーザーの利用権限を制限します")
    @app_commands.describe(
        restriction_type="制限したい権限",
        duration="制限期間",
        user="対象の Discord ユーザー",
        dummy_user="対象のダミーユーザー。<dummy_123> 形式",
        reason="制限理由",
    )
    @app_commands.choices(restriction_type=restriction_type_choices)
    @app_commands.choices(duration=restriction_duration_choices)
    async def admin_restrict_user_command(
        interaction: discord.Interaction[Any],
        restriction_type: str,
        duration: str,
        user: discord.Member | discord.User | None = None,
        dummy_user: str | None = None,
        reason: str | None = None,
    ) -> None:
        await handlers.admin_restrict_user(
            interaction,
            restriction_type,
            duration,
            target_user=user,
            dummy_user=dummy_user,
            reason=reason,
        )

    @tree.command(name="admin_unrestrict_user", description="ユーザーの利用権限制限を解除します")
    @app_commands.describe(
        restriction_type="解除したい制限種別",
        user="対象の Discord ユーザー",
        dummy_user="対象のダミーユーザー。<dummy_123> 形式",
    )
    @app_commands.choices(restriction_type=restriction_type_choices)
    async def admin_unrestrict_user_command(
        interaction: discord.Interaction[Any],
        restriction_type: str,
        user: discord.Member | discord.User | None = None,
        dummy_user: str | None = None,
    ) -> None:
        await handlers.admin_unrestrict_user(
            interaction,
            restriction_type,
            target_user=user,
            dummy_user=dummy_user,
        )

    def register_penalty_commands(
        *,
        add_name: str,
        sub_name: str,
        description: str,
        penalty_type: PenaltyType,
    ) -> None:
        @tree.command(name=add_name, description=f"{description} を +1 します")
        @app_commands.describe(
            user="対象の Discord ユーザー",
            dummy_user="対象のダミーユーザー。<dummy_123> 形式",
        )
        async def add_command(
            interaction: discord.Interaction[Any],
            user: discord.Member | discord.User | None = None,
            dummy_user: str | None = None,
        ) -> None:
            await handlers.admin_add_penalty(
                interaction,
                penalty_type,
                target_user=user,
                dummy_user=dummy_user,
            )

        @tree.command(name=sub_name, description=f"{description} を -1 します")
        @app_commands.describe(
            user="対象の Discord ユーザー",
            dummy_user="対象のダミーユーザー。<dummy_123> 形式",
        )
        async def sub_command(
            interaction: discord.Interaction[Any],
            user: discord.Member | discord.User | None = None,
            dummy_user: str | None = None,
        ) -> None:
            await handlers.admin_sub_penalty(
                interaction,
                penalty_type,
                target_user=user,
                dummy_user=dummy_user,
            )

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
        match_format="参加させたいフォーマット",
        queue_name="参加させたいキュー名",
        discord_user_id="キュー参加させたい Discord user ID",
    )
    @app_commands.choices(match_format=match_format_choices)
    @app_commands.choices(queue_name=queue_name_choices)
    async def dev_join_command(
        interaction: discord.Interaction[Any],
        match_format: str,
        queue_name: str,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_join(interaction, match_format, queue_name, discord_user_id)

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

    @tree.command(
        name="dev_player_info_season",
        description="指定したシーズンの任意の Discord user ID のプレイヤー情報を表示します",
    )
    @app_commands.describe(
        season_id="対象の season_id",
        discord_user_id="表示したい Discord user ID",
    )
    async def dev_player_info_season_command(
        interaction: discord.Interaction[Any],
        season_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_player_info_season(interaction, season_id, discord_user_id)

    @tree.command(name="dev_match_parent", description="ダミーユーザーを親に立候補させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_parent_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_parent(interaction, match_id, discord_user_id)

    @tree.command(name="dev_match_spectate", description="ダミーユーザーに観戦応募させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_spectate_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_spectate(interaction, match_id, discord_user_id)

    @tree.command(name="dev_match_win", description="ダミーユーザーに勝ちを報告させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_win_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_win(interaction, match_id, discord_user_id)

    @tree.command(name="dev_match_lose", description="ダミーユーザーに負けを報告させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_lose_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_lose(interaction, match_id, discord_user_id)

    @tree.command(name="dev_match_draw", description="ダミーユーザーに引き分けを報告させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_draw_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_draw(interaction, match_id, discord_user_id)

    @tree.command(name="dev_match_void", description="ダミーユーザーに無効試合を報告させます")
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_void_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_void(interaction, match_id, discord_user_id)

    @tree.command(
        name="dev_match_approve",
        description="ダミーユーザーに仮決定結果を承認させます",
    )
    @app_commands.describe(match_id="対象の match_id", discord_user_id="対象の dummy_user_id")
    async def dev_match_approve_command(
        interaction: discord.Interaction[Any],
        match_id: int,
        discord_user_id: str,
    ) -> None:
        await handlers.dev_match_approve(interaction, match_id, discord_user_id)

    @tree.command(name="dev_is_admin", description="実行者が admin かどうかを確認します")
    async def dev_is_admin_command(interaction: discord.Interaction[Any]) -> None:
        await handlers.dev_is_admin(interaction)
