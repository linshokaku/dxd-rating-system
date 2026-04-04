from __future__ import annotations

from dxd_rating.contexts.ui.application import InfoThreadCommandName

INFO_THREAD_INITIAL_MESSAGES = {
    InfoThreadCommandName.PLAYER_INFO: "\n".join(
        [
            "このスレッドは現在シーズンのプレイヤー情報確認用です。",
            (
                "今後はこのスレッド内のボタンから /player_info "
                "と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
    InfoThreadCommandName.PLAYER_INFO_SEASON: "\n".join(
        [
            "このスレッドはシーズン別プレイヤー情報確認用です。",
            (
                "今後はこのスレッド内の season_id 選択と実行ボタンから "
                "/player_info_season と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
    InfoThreadCommandName.LEADERBOARD: "\n".join(
        [
            "このスレッドは現在シーズンのランキング確認用です。",
            (
                "今後はこのスレッド内の match_format 選択、page 選択、実行ボタンから "
                "/leaderboard と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
    InfoThreadCommandName.LEADERBOARD_SEASON: "\n".join(
        [
            "このスレッドはシーズン別ランキング確認用です。",
            (
                "今後はこのスレッド内の season_id 選択、match_format 選択、page "
                "選択、実行ボタンから /leaderboard_season "
                "と同等の操作を行えるようにする予定です。"
            ),
        ]
    ),
}


def build_info_thread_initial_message(command_name: InfoThreadCommandName) -> str:
    return INFO_THREAD_INITIAL_MESSAGES[command_name]
