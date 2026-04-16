from __future__ import annotations

# dev command の基本応答文言
INVALID_DISCORD_USER_ID_MESSAGE = "discord_user_id が不正です。"
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
DEV_INFO_THREAD_SUCCESS_MESSAGE = "指定したユーザーの情報確認用スレッドを作成しました。"
DEV_INFO_THREAD_FAILED_MESSAGE = (
    "指定したユーザーの情報確認用スレッドの作成に失敗しました。管理者に確認してください。"
)
DEV_INFO_THREAD_REQUIRED_MESSAGE = "先に /info_thread または /dev_info_thread を実行してください。"
DEV_INFO_THREAD_NOT_FOUND_MESSAGE = (
    "情報確認用スレッドが見つかりません。"
    "先に /info_thread または /dev_info_thread を実行してください。"
)
DEV_PLAYER_INFO_SUCCESS_MESSAGE = "指定したユーザーのプレイヤー情報を表示しました。"
DEV_PLAYER_INFO_FAILED_MESSAGE = (
    "指定したユーザーのプレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
DEV_PLAYER_SEASON_INFO_SUCCESS_MESSAGE = (
    "指定したユーザーのシーズン別プレイヤー情報を表示しました。"
)
DEV_PLAYER_SEASON_INFO_FAILED_MESSAGE = (
    "指定したユーザーのシーズン別プレイヤー情報の取得に失敗しました。管理者に確認してください。"
)
DEV_LEADERBOARD_SUCCESS_MESSAGE = "指定したユーザーの情報確認用スレッドにランキングを表示しました。"
DEV_LEADERBOARD_FAILED_MESSAGE = (
    "指定したユーザーのランキングの取得に失敗しました。管理者に確認してください。"
)
DEV_LEADERBOARD_SEASON_SUCCESS_MESSAGE = (
    "指定したユーザーの情報確認用スレッドにシーズン別ランキングを表示しました。"
)
DEV_LEADERBOARD_SEASON_FAILED_MESSAGE = (
    "指定したユーザーのシーズン別ランキングの取得に失敗しました。管理者に確認してください。"
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

# dev command の説明文言
DEV_REGISTER_COMMAND_DESCRIPTION = "任意の Discord user ID を登録します"
DEV_REGISTER_DISCORD_USER_ID_DESCRIPTION = "登録したい Discord user ID"
DEV_JOIN_COMMAND_DESCRIPTION = "任意の Discord user ID をキュー参加させます"
DEV_JOIN_MATCH_FORMAT_DESCRIPTION = "参加させたいフォーマット"
DEV_JOIN_QUEUE_NAME_DESCRIPTION = "参加させたいキュー名"
DEV_JOIN_DISCORD_USER_ID_DESCRIPTION = "キュー参加させたい Discord user ID"
DEV_PRESENT_COMMAND_DESCRIPTION = "任意の Discord user ID の在席を更新します"
DEV_PRESENT_DISCORD_USER_ID_DESCRIPTION = "在席を更新したい Discord user ID"
DEV_LEAVE_COMMAND_DESCRIPTION = "任意の Discord user ID をキューから退出させます"
DEV_LEAVE_DISCORD_USER_ID_DESCRIPTION = "キューから退出させたい Discord user ID"
DEV_INFO_THREAD_COMMAND_DESCRIPTION = (
    "任意の Discord user ID に紐づく情報確認用スレッドを作成します"
)
DEV_INFO_THREAD_COMMAND_NAME_DESCRIPTION = "作成したい情報確認スレッドの用途"
DEV_INFO_THREAD_DISCORD_USER_ID_DESCRIPTION = "紐づけたい Discord user ID"
DEV_PLAYER_INFO_COMMAND_DESCRIPTION = "任意の Discord user ID のプレイヤー情報を表示します"
DEV_PLAYER_INFO_DISCORD_USER_ID_DESCRIPTION = "表示したい Discord user ID"
DEV_PLAYER_INFO_SEASON_COMMAND_DESCRIPTION = (
    "指定したシーズンの任意の Discord user ID のプレイヤー情報を表示します"
)
DEV_PLAYER_INFO_SEASON_ID_DESCRIPTION = "対象の season_id"
DEV_PLAYER_INFO_SEASON_DISCORD_USER_ID_DESCRIPTION = "表示したい Discord user ID"
DEV_LEADERBOARD_COMMAND_DESCRIPTION = (
    "任意の Discord user ID の情報確認スレッドに現在シーズンのランキングを表示します"
)
DEV_LEADERBOARD_MATCH_FORMAT_DESCRIPTION = "対象のフォーマット"
DEV_LEADERBOARD_PAGE_DESCRIPTION = "表示したいページ番号"
DEV_LEADERBOARD_DISCORD_USER_ID_DESCRIPTION = "表示先として使いたい Discord user ID"
DEV_LEADERBOARD_SEASON_COMMAND_DESCRIPTION = (
    "任意の Discord user ID の情報確認スレッドに指定シーズンのランキングを表示します"
)
DEV_LEADERBOARD_SEASON_ID_DESCRIPTION = "対象の season_id"
DEV_LEADERBOARD_SEASON_MATCH_FORMAT_DESCRIPTION = "対象のフォーマット"
DEV_LEADERBOARD_SEASON_PAGE_DESCRIPTION = "表示したいページ番号"
DEV_LEADERBOARD_SEASON_DISCORD_USER_ID_DESCRIPTION = "表示先として使いたい Discord user ID"
DEV_MATCH_PARENT_COMMAND_DESCRIPTION = "ダミーユーザーを親に立候補させます"
DEV_MATCH_SPECTATE_COMMAND_DESCRIPTION = "ダミーユーザーに観戦応募させます"
DEV_MATCH_WIN_COMMAND_DESCRIPTION = "ダミーユーザーに勝ちを報告させます"
DEV_MATCH_LOSE_COMMAND_DESCRIPTION = "ダミーユーザーに負けを報告させます"
DEV_MATCH_DRAW_COMMAND_DESCRIPTION = "ダミーユーザーに引き分けを報告させます"
DEV_MATCH_VOID_COMMAND_DESCRIPTION = "ダミーユーザーに無効試合を報告させます"
DEV_MATCH_APPROVE_COMMAND_DESCRIPTION = "ダミーユーザーに仮決定結果を承認させます"
DEV_MATCH_MATCH_ID_DESCRIPTION = "対象の match_id"
DEV_MATCH_DISCORD_USER_ID_DESCRIPTION = "対象の dummy_user_id"
DEV_IS_ADMIN_COMMAND_DESCRIPTION = "実行者が admin かどうかを確認します"


# dev 向けの組み立て文言
def build_dev_is_admin_message(is_admin: bool) -> str:
    return "はい" if is_admin else "いいえ"
