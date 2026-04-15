from __future__ import annotations

# 登録パネルの表示文言
REGISTER_PANEL_MESSAGE = "\n".join(
    [
        "プレイヤー登録はこちらから行えます。",
        "ボタンを押すと利用規約に同意したものとして扱います。",
        "登録後は Bot の各種機能を利用できます。",
        "登録後はマッチング関連チャンネルとシステムアナウンスを閲覧できます。",
    ]
)
REGISTER_PANEL_BUTTON_LABEL = "利用規約に同意して登録"

# slash command の説明文言
REGISTER_COMMAND_DESCRIPTION = "プレイヤー登録を行います"

# 登録まわりの応答文言
REGISTER_SUCCESS_MESSAGE = "登録が完了しました。"
REGISTER_ALREADY_REGISTERED_MESSAGE = "すでに登録済みです。"
REGISTER_FAILED_MESSAGE = "登録に失敗しました。管理者に確認してください。"
PLAYER_REGISTRATION_REQUIRED_MESSAGE = (
    "プレイヤー登録が必要です。登録案内チャンネルのボタンから登録してください。"
)
REGISTER_PANEL_FALLBACK_ERROR_MESSAGE = "登録に失敗しました。管理者に確認してください。"
