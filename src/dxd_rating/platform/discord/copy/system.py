from __future__ import annotations

from collections.abc import Sequence

# システム系チャンネルの本文
SYSTEM_ANNOUNCEMENTS_CHANNEL_MESSAGE = "このチャンネルは運営からのシステムアナウンス専用です。"
ADMIN_CONTACT_CHANNEL_MESSAGE = "運営への連絡やフィードバックはこちらへどうぞ。"
ADMIN_OPERATIONS_CHANNEL_MESSAGE = "このチャンネルは super admin 専用の運用連絡チャンネルです。"

# システム共通の応答文言
APPLICATION_COMMAND_INTERNAL_ERROR_MESSAGE = "内部エラーが発生しました。管理者に確認してください。"

# 運用通知の本文
ADMIN_OPERATIONS_DAILY_WORKER_STARTED_MESSAGE = "daily worker が起動しました。"
SEASON_COMPLETED_MESSAGE = "シーズンの全試合が完了しました。"
SEASON_TOP_RANKINGS_MESSAGE = "シーズン最終順位表"


# 運用通知の組み立て関数
def build_admin_operations_daily_worker_started_message(started_at: str) -> str:
    return "\n".join(
        [
            ADMIN_OPERATIONS_DAILY_WORKER_STARTED_MESSAGE,
            f"開始時刻: {started_at}",
        ]
    )


def build_season_completed_message(
    season_id: int,
    season_name: str,
    completed_at: str,
) -> str:
    return "\n".join(
        [
            SEASON_COMPLETED_MESSAGE,
            f"season_id: {season_id}",
            f"season_name: {season_name}",
            f"完了時刻: {completed_at}",
        ]
    )


def build_season_top_rankings_message(
    *,
    season_id: int,
    season_name: str,
    match_format: str,
    item_range: str | None,
    ranking_lines: Sequence[str],
) -> str:
    lines = [
        SEASON_TOP_RANKINGS_MESSAGE,
        f"season_id: {season_id}",
        f"season_name: {season_name}",
        f"match_format: {match_format}",
    ]
    if not ranking_lines:
        lines.extend(["", "対象者なし"])
        return "\n".join(lines)

    assert item_range is not None
    lines.extend([f"items: {item_range}", "", *ranking_lines])
    return "\n".join(lines)
