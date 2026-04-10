from __future__ import annotations

from collections.abc import Sequence

from dxd_rating.contexts.restrictions.application import PlayerAccessRestrictionDuration
from dxd_rating.platform.db.models import PlayerAccessRestrictionType

# 管理者向けの基本応答文言
ADMIN_ONLY_MESSAGE = "このコマンドは管理者のみ実行できます。"
INVALID_ADMIN_TARGET_USER_MESSAGE = "対象ユーザーの指定が不正です。"
INVALID_MATCH_RESULT_MESSAGE = "result が不正です。"
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
SEASON_NAME_REQUIRED_MESSAGE = "シーズン名を入力してください。"
SEASON_NAME_TOO_LONG_MESSAGE = "シーズン名が長すぎます。"
SEASON_NAME_LEADING_DIGIT_MESSAGE = "シーズン名の先頭に数字は使えません。"
SEASON_NAME_ALREADY_EXISTS_MESSAGE = "指定したシーズン名はすでに使われています。"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_SUCCESS_MESSAGE = "UI 設置チャンネルを作成しました。"
ADMIN_INVALID_UI_TYPE_MESSAGE = "指定した UI は存在しません。"
ADMIN_INVALID_CHANNEL_NAME_MESSAGE = "channel_name が不正です。"
ADMIN_DUPLICATE_CHANNEL_NAME_MESSAGE = "同名のチャンネルがすでに存在します。"
ADMIN_UI_ALREADY_INSTALLED_MESSAGE = "指定した UI はすでに設置済みです。"
ADMIN_MANAGED_UI_PERMISSION_MESSAGE = "Bot に必要な権限がありません。"
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
ADMIN_INVALID_TEARDOWN_CONFIRM_MESSAGE = "confirm が不正です。"
ADMIN_TEARDOWN_UI_CHANNELS_SUCCESS_MESSAGE = "UI 設置チャンネルをすべて撤収しました。"
ADMIN_TEARDOWN_UI_CHANNELS_EMPTY_MESSAGE = "撤収対象の UI 設置チャンネルはありません。"
ADMIN_TEARDOWN_UI_CHANNELS_FAILED_MESSAGE = (
    "UI 設置チャンネルの撤収に失敗しました。管理者に確認してください。"
)

# 管理者向けの表示ラベル
MANAGED_UI_PERMISSION_LABEL_MANAGE_CHANNELS = "チャンネルの管理"
MANAGED_UI_PERMISSION_LABEL_MANAGE_ROLES = "ロールの管理"
MANAGED_UI_PERMISSION_LABEL_CREATE_PRIVATE_THREADS = "プライベートスレッドの作成"
MANAGED_UI_PERMISSION_LABEL_SEND_MESSAGES_IN_THREADS = "スレッドでメッセージを送信"
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
ADMIN_INCORRECT_REPORT_PENALTY_DESCRIPTION = "勝敗誤報告ペナルティ"
ADMIN_NO_REPORT_PENALTY_DESCRIPTION = "勝敗無報告ペナルティ"
ADMIN_ROOM_SETUP_DELAY_PENALTY_DESCRIPTION = "部屋立て遅延ペナルティ"
ADMIN_MATCH_MISTAKE_PENALTY_DESCRIPTION = "試合ミスペナルティ"
ADMIN_LATE_PENALTY_DESCRIPTION = "遅刻ペナルティ"
ADMIN_DISCONNECT_PENALTY_DESCRIPTION = "切断ペナルティ"

# 管理者向け slash command の説明文言
ADMIN_MATCH_RESULT_COMMAND_DESCRIPTION = "試合結果を上書きします"
ADMIN_MATCH_RESULT_MATCH_ID_DESCRIPTION = "対象の match_id"
ADMIN_MATCH_RESULT_RESULT_DESCRIPTION = "上書きする結果"
ADMIN_RENAME_SEASON_COMMAND_DESCRIPTION = "シーズン名を変更します"
ADMIN_RENAME_SEASON_SEASON_ID_DESCRIPTION = "対象の season_id"
ADMIN_RENAME_SEASON_NAME_DESCRIPTION = "新しいシーズン名"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_COMMAND_DESCRIPTION = "指定した UI 設置チャンネルを作成します"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_UI_TYPE_DESCRIPTION = "設置したい UI の種別"
ADMIN_SETUP_CUSTOM_UI_CHANNEL_CHANNEL_NAME_DESCRIPTION = "作成するチャンネル名"
ADMIN_SETUP_UI_CHANNELS_COMMAND_DESCRIPTION = "必要な UI 設置チャンネルをまとめて作成します"
ADMIN_CLEANUP_UI_CHANNELS_COMMAND_DESCRIPTION = "setup の障害となる重複チャンネルを削除します"
ADMIN_CLEANUP_UI_CHANNELS_CONFIRM_DESCRIPTION = "cleanup する場合は cleanup を入力"
ADMIN_TEARDOWN_UI_CHANNELS_COMMAND_DESCRIPTION = "管理対象の UI 設置チャンネルをまとめて撤収します"
ADMIN_TEARDOWN_UI_CHANNELS_CONFIRM_DESCRIPTION = "撤収する場合は teardown を入力"
ADMIN_RESTRICT_USER_COMMAND_DESCRIPTION = "ユーザーの利用権限を制限します"
ADMIN_RESTRICT_USER_RESTRICTION_TYPE_DESCRIPTION = "制限したい権限"
ADMIN_RESTRICT_USER_DURATION_DESCRIPTION = "制限期間"
ADMIN_RESTRICT_USER_USER_DESCRIPTION = "対象の Discord ユーザー"
ADMIN_RESTRICT_USER_DUMMY_USER_DESCRIPTION = "対象のダミーユーザー。<dummy_123> 形式"
ADMIN_RESTRICT_USER_REASON_DESCRIPTION = "制限理由"
ADMIN_UNRESTRICT_USER_COMMAND_DESCRIPTION = "ユーザーの利用権限制限を解除します"
ADMIN_UNRESTRICT_USER_RESTRICTION_TYPE_DESCRIPTION = "解除したい制限種別"
ADMIN_UNRESTRICT_USER_USER_DESCRIPTION = "対象の Discord ユーザー"
ADMIN_UNRESTRICT_USER_DUMMY_USER_DESCRIPTION = "対象のダミーユーザー。<dummy_123> 形式"
ADMIN_PENALTY_USER_DESCRIPTION = "対象の Discord ユーザー"
ADMIN_PENALTY_DUMMY_USER_DESCRIPTION = "対象のダミーユーザー。<dummy_123> 形式"

# 管理者向け文言の組み立て関数
def build_admin_match_result_public_message(match_id: int, result_label: str) -> str:
    return f"match_id: {match_id} の試合結果が管理者操作により「{result_label}」に上書きされました。"


def build_admin_restriction_executor_message(
    restriction_type: PlayerAccessRestrictionType,
    duration: PlayerAccessRestrictionDuration,
) -> str:
    return (
        f"指定したユーザーの"
        f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[restriction_type]}を"
        f"{PLAYER_ACCESS_RESTRICTION_DURATION_LABELS[duration]}制限しました。"
    )


def build_admin_restriction_public_message(
    target_label: str,
    restriction_type: PlayerAccessRestrictionType,
    duration: PlayerAccessRestrictionDuration,
) -> str:
    return (
        f"{target_label} の"
        f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[restriction_type]}を"
        f"{PLAYER_ACCESS_RESTRICTION_DURATION_LABELS[duration]}制限しました。"
    )


def build_admin_unrestriction_executor_message(
    restriction_type: PlayerAccessRestrictionType,
) -> str:
    return (
        f"指定したユーザーの"
        f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[restriction_type]}制限を解除しました。"
    )


def build_admin_unrestriction_public_message(
    target_label: str,
    restriction_type: PlayerAccessRestrictionType,
) -> str:
    return (
        f"{target_label} の"
        f"{PLAYER_ACCESS_RESTRICTION_TYPE_LABELS[restriction_type]}制限を解除しました。"
    )


def build_admin_penalty_public_message(
    target_label: str,
    penalty_label: str,
    delta: int,
    count: int,
) -> str:
    adjustment = "+1" if delta > 0 else "-1"
    return (
        f"{target_label} の"
        f"{penalty_label}ペナルティを{adjustment}しました。"
        f"現在の累積: {count}"
    )


def build_admin_penalty_command_description(description: str, delta: int) -> str:
    adjustment = "+1" if delta > 0 else "-1"
    return f"{description} を {adjustment} します"


def build_managed_ui_permission_message(
    missing_permissions: Sequence[str] = (),
    *,
    forbidden_detail: str | None = None,
) -> str:
    parts = [ADMIN_MANAGED_UI_PERMISSION_MESSAGE]
    if missing_permissions:
        parts.append(f"不足している権限: {', '.join(missing_permissions)}")
    if forbidden_detail is not None:
        parts.append(forbidden_detail)
    return " ".join(parts)

