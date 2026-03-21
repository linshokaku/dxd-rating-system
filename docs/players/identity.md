# プレイヤー識別・表示名仕様

## 目的

- システム内でのプレイヤー識別方法を定義する
- Bot が保持する表示名キャッシュの方針を定義する
- read-only な Web 表示から Discord API を直接引かなくても最低限の表示ができるようにする

## 識別子

- プレイヤーの外部識別子は `discord_user_id` とする
- アプリケーション内部の主キーは `players.id` とする
- 試合、レート、キュー、ペナルティなどの関連テーブルは `player_id` を参照する

補足:

- `discord_user_id` は Discord 側の安定識別子として扱う
- 表示名は識別子ではなく、あくまで表示用データとして扱う

## 表示名キャッシュの基本方針

- 表示名の真実のソースは Discord とする
- Bot は表示用途のために、Discord 上の表示名を DB へキャッシュしてよい
- このキャッシュは補助情報であり、厳密な即時一致は要求しない
- 表示名キャッシュの更新失敗だけを理由にコマンド本体を失敗させない
- 初期前提が 1 つの Discord サーバーであるため、サーバー内表示名を優先してよい

## 保持項目

`players` には、必要に応じて少なくとも以下を持たせてよい。

- `display_name`
- `display_name_updated_at`
- `last_seen_at`

意図:

- `display_name`: ランキングや管理画面での表示用
- `display_name_updated_at`: キャッシュの鮮度確認用
- `last_seen_at`: Bot と最後に接触した時刻の把握用

## 表示名の解決順

実在する Discord ユーザーについて、Bot はコマンド interaction から取得できる情報をもとに、以下の優先順位で表示名を解決してよい。

1. guild 内の表示名
2. Discord 上のグローバル表示名
3. username

補足:

- 別途 API fetch を毎回行うことは必須としない
- 初期実装では、interaction payload から取得できる値だけで更新する方針を推奨する

## 更新タイミング

### 自己操作コマンド

登録済みのユーザーが Bot に対してコマンドを実行した場合、Bot はその interaction に含まれるユーザー情報を使って、対象ユーザー自身の表示名キャッシュを best effort で更新してよい。

対象例:

- `/register`
- `/join`
- `/present`
- `/leave`
- `/match_spectate`
- `/match_win`
- `/match_lose`
- `/match_draw`
- `/match_void`
- `/match_approve`

### 管理者コマンド

admin コマンドで `discord.Member` または `discord.User` を直接受け取った場合は、その対象ユーザーの表示名キャッシュも best effort で更新してよい。

### 更新内容

表示名キャッシュを更新する場合は、少なくとも以下を行う。

- `display_name` を更新する
- `display_name_updated_at` を更新する
- `last_seen_at` を更新する

## ダミーユーザー

開発用のダミーユーザーについては、Discord API から表示名を取得しない。

初期方針:

- 表示名は `<dummy_{discord_user_id}>` 形式で固定してよい
- ダミーユーザーの表示名は手動変更や同期対象にしない

## フォールバック

表示名キャッシュが未設定または古い場合でも、システムは動作を継続する。

表示側の最低限のフォールバック例:

- `display_name` があればそれを使う
- なければ `discord_user_id` を文字列表示する

## 非目的

本仕様では、以下は扱わない。

- Discord 上の表示名変更イベントのリアルタイム購読
- 複数 guild 間での表示名の持ち分け
- 表示名の変更履歴保存
