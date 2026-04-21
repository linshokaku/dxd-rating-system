from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from dxd_rating.contexts.matchmaking.application import MatchmakingStatusSnapshotEntry
from dxd_rating.platform.db.models import MatchFormat
from dxd_rating.platform.discord.copy.time_format import format_discord_datetime

# マッチング導線の UI 本文
MATCHMAKING_CHANNEL_STATUS_PLACEHOLDER_MESSAGE = "\n".join(
    [
        "直近30分の参加状況",
        "参加状況はまだ取得されていません",
    ]
)

# マッチング導線のボタン・placeholder・補助文言
MATCHMAKING_CHANNEL_QUEUE_NAME_PLACEHOLDER = "階級を選択"
MATCHMAKING_CHANNEL_JOIN_BUTTON_LABEL = "参加"
MATCHMAKING_CHANNEL_UPDATE_STATUS_BUTTON_LABEL = "更新する"
MATCHMAKING_CHANNEL_SELECT_QUEUE_NAME_MESSAGE = "階級を選択してください。"
MATCHMAKING_PRESENCE_THREAD_PRESENT_BUTTON_LABEL = "在席"
MATCHMAKING_PRESENCE_THREAD_LEAVE_BUTTON_LABEL = "マッチングキャンセル"

# slash command の説明文言
JOIN_COMMAND_DESCRIPTION = "マッチングキューに参加します"
JOIN_MATCH_FORMAT_DESCRIPTION = "参加したいフォーマット"
JOIN_QUEUE_NAME_DESCRIPTION = "参加したいキュー名"
PRESENT_COMMAND_DESCRIPTION = "在席を更新して期限を延長します"
LEAVE_COMMAND_DESCRIPTION = "マッチングキューから退出します"
UPDATE_MATCHMAKING_STATUS_COMMAND_DESCRIPTION = "レート戦マッチングの参加状況表示を更新します"

# プレイヤー向けの応答文言
INVALID_MATCH_FORMAT_MESSAGE = "指定したフォーマットは存在しません。"
INVALID_QUEUE_NAME_MESSAGE = "指定したキューは存在しません。"
QUEUE_JOIN_NOT_ALLOWED_MESSAGE = "現在のレーティングではそのキューに参加できません。"
QUEUE_JOIN_RESTRICTED_MESSAGE = "現在キュー参加を制限されています。"
JOIN_ALREADY_JOINED_MESSAGE = "すでにキュー参加中です。"
JOIN_SUCCESS_MESSAGE = "キューに参加しました。30分間マッチングします。"
PRESENT_SUCCESS_MESSAGE = "在席を更新しました。次の期限は30分後です。"
PRESENT_NOT_JOINED_MESSAGE = "キューに参加していません。"
PRESENT_EXPIRED_MESSAGE = "期限切れのためキューから外れました。"
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
LEAVE_SUCCESS_MESSAGE = "キューから退出しました。"
LEAVE_ALREADY_EXPIRED_MESSAGE = "すでに期限切れでキューから外れています。"
LEAVE_FAILED_MESSAGE = "キュー退出に失敗しました。管理者に確認してください。"
UPDATE_MATCHMAKING_STATUS_SUCCESS_MESSAGE = "参加状況を更新しました。"
UPDATE_MATCHMAKING_STATUS_FAILED_MESSAGE = (
    "参加状況の更新に失敗しました。管理者に確認してください。"
)
MATCHMAKING_CHANNEL_FALLBACK_ERROR_MESSAGE = "操作に失敗しました。管理者に確認してください。"
MATCHMAKING_CHANNEL_STATUS_UPDATE_FALLBACK_ERROR_MESSAGE = (
    "参加状況の更新に失敗しました。管理者に確認してください。"
)
MATCHMAKING_PRESENCE_THREAD_FALLBACK_ERROR_MESSAGE = (
    "操作に失敗しました。管理者に確認してください。"
)


# キュー関連の通知文言
def build_queue_joined_notification_message(
    *,
    player_mention: str,
    match_format: str,
    queue_name: str,
) -> str:
    return "\n".join(
        [
            f"{player_mention} がキューに参加しました。",
            f"マッチ形式: {match_format}",
            f"マッチ階級: {queue_name}",
        ]
    )


PRESENCE_REMINDER_NOTIFICATION_MESSAGE = (
    "在席確認です。5分以内に在席更新がない場合はマッチングキューから外れます。"
)
QUEUE_EXPIRED_NOTIFICATION_MESSAGE = "期限切れでマッチングキューから外れました。"


# マッチング導線の組み立て文言
def build_matchmaking_guide_message(guide_url: str) -> str:
    return "\n".join(
        [
            "レート戦の遊び方",
            "下のパネルで参加したいマッチ形式の階級を選び、参加ボタンを押すとマッチングを始められます。",
            "マッチしたら、まずマッチを進める「親」を1人決めてください。",
            "マッチは3試合制で、勝った試合が多いチームの勝ちです。",
            "勝った試合数が同じなら引き分けです。",
            "どちらかのチームが2連勝したら、その時点でマッチ終了です。",
            "マッチが終わったら、参加した **全員** が勝敗報告をしてください。",
            f"くわしい遊び方は [こちらから]({guide_url}) 確認できます。",
        ]
    )


def build_matchmaking_status_message(
    snapshot: Sequence[MatchmakingStatusSnapshotEntry],
    updated_at: datetime,
) -> str:
    lines = [
        "直近30分の参加状況",
        f"最終更新: {format_discord_datetime(updated_at)}",
    ]
    lines.extend(
        f"{entry.match_format.value}-{entry.queue_name}: {entry.active_count}" for entry in snapshot
    )
    return "\n".join(lines)


def build_matchmaking_panel_message(match_format: MatchFormat | str) -> str:
    resolved_match_format = (
        match_format if isinstance(match_format, MatchFormat) else MatchFormat(match_format)
    )
    return "\n".join(
        [
            f"{resolved_match_format.value} の参加キューを選択してください。",
            "在席確認やマッチングキャンセルは、参加後に作成される在席確認スレッドで行ってください。",
        ]
    )


def build_matchmaking_presence_thread_guide_message(thread_mention: str) -> str:
    return f"在席確認は {thread_mention} で行ってください。"


def build_matchmaking_join_success_message(thread_mention: str | None = None) -> str:
    if thread_mention is None:
        return JOIN_SUCCESS_MESSAGE
    return "\n".join(
        [
            JOIN_SUCCESS_MESSAGE,
            build_matchmaking_presence_thread_guide_message(thread_mention),
        ]
    )


def build_matchmaking_presence_thread_name(suffix: str) -> str:
    return f"在席確認-{suffix}"
