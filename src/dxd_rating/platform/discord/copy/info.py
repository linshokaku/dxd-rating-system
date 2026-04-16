from __future__ import annotations

from dxd_rating.contexts.leaderboard.application import (
    CurrentLeaderboardPage,
    SeasonLeaderboardPage,
)
from dxd_rating.contexts.players.application import PlayerInfo
from dxd_rating.contexts.ui.application import InfoThreadCommandName
from dxd_rating.platform.discord.copy.time_format import format_discord_datetime

# 情報確認チャンネルの UI 本文
INFO_CHANNEL_MESSAGE = "\n".join(
    [
        "このチャンネルはレート戦の情報確認用です。",
        "使いたい項目のボタンを押すと、自分用の情報確認スレッドを作成できます。",
        "スレッド作成直後にランキングやプレイヤー情報は自動表示されません。",
    ]
)

# 情報確認チャンネルとスレッドのボタン・placeholder・誘導文言
INFO_CHANNEL_LEADERBOARD_BUTTON_LABEL = "現在シーズンのランキング"
INFO_CHANNEL_LEADERBOARD_SEASON_BUTTON_LABEL = "シーズン別ランキング"
INFO_CHANNEL_PLAYER_INFO_BUTTON_LABEL = "現在シーズンのプレイヤー情報"
INFO_CHANNEL_PLAYER_INFO_SEASON_BUTTON_LABEL = "シーズン別プレイヤー情報"
INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX = (
    "再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。"
)
INFO_THREAD_PLAYER_INFO_SHOW_BUTTON_LABEL = "プレイヤー情報を表示"
INFO_THREAD_PLAYER_INFO_FALLBACK_ERROR_MESSAGE = (
    "プレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
INFO_THREAD_PLAYER_INFO_SEASON_PLACEHOLDER = "シーズンを選択"
INFO_THREAD_PLAYER_INFO_SEASON_SELECT_SEASON_MESSAGE = (
    f"シーズンを選択してください。{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_PLAYER_INFO_SEASON_FALLBACK_ERROR_MESSAGE = (
    "シーズン別プレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
INFO_THREAD_LEADERBOARD_MATCH_FORMAT_PLACEHOLDER = "試合形式を選択"
INFO_THREAD_LEADERBOARD_SHOW_BUTTON_LABEL = "ランキングを表示"
INFO_THREAD_LEADERBOARD_SELECT_MATCH_FORMAT_MESSAGE = (
    f"試合形式を選択してください。{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_LEADERBOARD_NEXT_PAGE_BUTTON_LABEL = "次のページ"
INFO_THREAD_LEADERBOARD_FALLBACK_ERROR_MESSAGE = (
    "ランキングの取得に失敗しました。管理者に確認してください。"
)
INFO_THREAD_LEADERBOARD_SEASON_PLACEHOLDER = "シーズンを選択"
INFO_THREAD_LEADERBOARD_SEASON_SELECT_SEASON_MESSAGE = (
    f"シーズンを選択してください。{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)
INFO_THREAD_LEADERBOARD_SEASON_SELECT_BOTH_MESSAGE = (
    f"シーズンと試合形式を選択してください。{INFO_THREAD_RETRY_INFO_THREAD_MESSAGE_SUFFIX}"
)

# slash command の説明文言
PLAYER_INFO_COMMAND_DESCRIPTION = "自分のプレイヤー情報を表示します"
INFO_THREAD_COMMAND_DESCRIPTION = "情報確認用スレッドを作成します"
INFO_THREAD_COMMAND_NAME_DESCRIPTION = "作成したい情報確認スレッドの用途"
PLAYER_INFO_SEASON_COMMAND_DESCRIPTION = "指定したシーズンの自分のプレイヤー情報を表示します"
PLAYER_INFO_SEASON_SEASON_ID_DESCRIPTION = "対象の season_id"
LEADERBOARD_COMMAND_DESCRIPTION = "現在シーズンのランキングを表示します"
LEADERBOARD_MATCH_FORMAT_DESCRIPTION = "対象のフォーマット"
LEADERBOARD_PAGE_DESCRIPTION = "表示したいページ番号"
LEADERBOARD_SEASON_COMMAND_DESCRIPTION = "指定したシーズンのランキングを表示します"
LEADERBOARD_SEASON_SEASON_ID_DESCRIPTION = "対象の season_id"
LEADERBOARD_SEASON_MATCH_FORMAT_DESCRIPTION = "対象のフォーマット"
LEADERBOARD_SEASON_PAGE_DESCRIPTION = "表示したいページ番号"

# 情報確認まわりの応答文言
PLAYER_INFO_SUCCESS_MESSAGE = "プレイヤー情報を表示しました。"
PLAYER_INFO_FAILED_MESSAGE = "プレイヤー情報の取得に失敗しました。管理者に確認してください。"
PLAYER_SEASON_INFO_SUCCESS_MESSAGE = "シーズン別プレイヤー情報を表示しました。"
PLAYER_SEASON_INFO_FAILED_MESSAGE = (
    "シーズン別プレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
SEASON_NOT_FOUND_MESSAGE = "指定したシーズンが見つかりません。"
PLAYER_SEASON_INFO_NOT_FOUND_MESSAGE = "指定したシーズンのプレイヤー情報はありません。"
LEADERBOARD_SUCCESS_MESSAGE = "ランキングを表示しました。"
LEADERBOARD_FAILED_MESSAGE = "ランキングの取得に失敗しました。管理者に確認してください。"
LEADERBOARD_SEASON_FAILED_MESSAGE = (
    "シーズン別ランキングの取得に失敗しました。管理者に確認してください。"
)
INVALID_LEADERBOARD_PAGE_MESSAGE = "page は 1 以上で指定してください。"
LEADERBOARD_PAGE_NOT_FOUND_MESSAGE = "指定したページにはランキングがありません。"
SEASON_NOT_STARTED_MESSAGE = "指定したシーズンはまだ開始していません。"
INFO_THREAD_REQUIRED_MESSAGE = (
    "先に情報確認チャンネルのボタンから情報確認用スレッドを作成してください。"
)
INFO_THREAD_NOT_FOUND_MESSAGE = (
    "情報確認用スレッドが見つかりません。"
    "情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。"
)
INFO_THREAD_INACTIVE_MESSAGE = (
    "このスレッドは現在の情報確認用スレッドではありません。"
    "最新の情報確認用スレッドを利用してください。"
)
INFO_THREAD_SUCCESS_MESSAGE = "情報確認用スレッドを作成しました。"
INFO_THREAD_CHANNEL_NOT_FOUND_MESSAGE = (
    "情報確認用チャンネルが見つかりません。管理者に確認してください。"
)
INFO_CHANNEL_FALLBACK_ERROR_MESSAGE = (
    "情報確認用スレッドの作成に失敗しました。管理者に確認してください。"
)
INFO_THREAD_FAILED_MESSAGE = "情報確認用スレッドの作成に失敗しました。管理者に確認してください。"

# 情報確認スレッド本文の定義
INFO_THREAD_INITIAL_MESSAGES = {
    InfoThreadCommandName.PLAYER_INFO: "\n".join(
        [
            "このスレッドは現在シーズンのプレイヤー情報確認用です。",
            "「プレイヤー情報を表示」を押してください。",
        ]
    ),
    InfoThreadCommandName.PLAYER_INFO_SEASON: "\n".join(
        [
            "このスレッドはシーズン別プレイヤー情報確認用です。",
            "シーズンを選んで「プレイヤー情報を表示」を押してください。",
        ]
    ),
    InfoThreadCommandName.LEADERBOARD: "\n".join(
        [
            "このスレッドは現在シーズンのランキング確認用です。",
            "試合形式を選んで「ランキングを表示」を押してください。",
        ]
    ),
    InfoThreadCommandName.LEADERBOARD_SEASON: "\n".join(
        [
            "このスレッドはシーズン別ランキング確認用です。",
            "シーズンと試合形式を選んで「ランキングを表示」を押してください。",
        ]
    ),
}


# 情報確認文言の組み立て関数
def build_info_thread_initial_message(command_name: InfoThreadCommandName) -> str:
    return INFO_THREAD_INITIAL_MESSAGES[command_name]


def build_player_info_message(
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
            else format_discord_datetime(format_stats.last_played_at)
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


def build_current_leaderboard_message(leaderboard_page: CurrentLeaderboardPage) -> str:
    first_rank = leaderboard_page.entries[0].rank
    last_rank = leaderboard_page.entries[-1].rank
    lines = [
        "ランキング",
        f"season: {leaderboard_page.season_name}",
        f"match_format: {leaderboard_page.match_format.value}",
        f"page: {leaderboard_page.page}",
        f"items: {first_rank}-{last_rank}",
        "",
    ]
    lines.extend(
        (
            f"{entry.rank} / {entry.display_name} / {entry.rating:.2f} / "
            f"{_format_leaderboard_rank_change(entry.rank_change_1d)} / "
            f"{_format_leaderboard_rank_change(entry.rank_change_3d)} / "
            f"{_format_leaderboard_rank_change(entry.rank_change_7d)}"
        )
        for entry in leaderboard_page.entries
    )
    return "\n".join(lines)


def build_season_leaderboard_message(leaderboard_page: SeasonLeaderboardPage) -> str:
    first_rank = leaderboard_page.entries[0].rank
    last_rank = leaderboard_page.entries[-1].rank
    lines = [
        "ランキング",
        f"season_id: {leaderboard_page.season_id}",
        f"season_name: {leaderboard_page.season_name}",
        f"match_format: {leaderboard_page.match_format.value}",
        f"page: {leaderboard_page.page}",
        f"items: {first_rank}-{last_rank}",
        "",
    ]
    lines.extend(
        f"{entry.rank} / {entry.display_name} / {entry.rating:.2f}"
        for entry in leaderboard_page.entries
    )
    return "\n".join(lines)


def build_info_thread_name(suffix: str) -> str:
    return f"情報-{suffix}"


def _format_leaderboard_rank_change(rank_change: int | None) -> str:
    if rank_change is None:
        return "-"
    if rank_change > 0:
        return f"+{rank_change}"
    return str(rank_change)
