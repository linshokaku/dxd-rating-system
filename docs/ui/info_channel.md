# レート戦情報チャンネル UI 仕様

## UI 識別子

- `ui_type`: `info_channel`

## 目的

登録済みユーザーが、`レート戦情報` チャンネルに設置された公開 button UI から、自分用の情報確認 private thread を作成できるようにする。

## 対象外

- 情報確認用 private thread 内に表示する情報取得 UI の詳細。
  - [info_thread.md](info_thread.md)
- `/info_thread`、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` の slash command 入出力仕様。
  - [../commands/user-commands.md](../commands/user-commands.md)
- `レート戦情報` チャンネル自体の用途と権限。
  - [registered_channels.md](registered_channels.md)
- 情報確認用チャンネルの作成コマンド仕様。
  - [setup_channel.md](setup_channel.md)

## 前提

- この UI は、登録済みユーザー向けの `レート戦情報` チャンネルに設置する。
- チャンネル自体の用途と権限は [registered_channels.md](registered_channels.md) を参照する。
- UI 設置コマンドの仕様は [setup_channel.md](setup_channel.md) を参照する。
- この UI は `/info_thread` の代替導線であり、業務処理、成功文言、失敗文言は `/info_thread` と同じものを使う。
- `/info_thread` は引き続き有効な導線として残す。
- 公開チャンネル側の UI は info thread 作成導線だけを担当し、情報本文の表示は作成後の private thread 側に委ねる。

## 公開 UI の表示要素

- 情報確認導線用の常設メッセージを 1 つ設置する。
- メッセージには、少なくとも以下を表示する。
  - この UI が情報確認用 private thread を作成するための導線であること。
  - 用途に応じた button を押して thread を作成すること。
  - button 押下直後にはランキング本文やプレイヤー情報本文を自動投稿しないこと。
  - 作成後の private thread 側で、将来的に各種情報確認 UI を使えるようにする想定であること。
- メッセージには、以下の 4 button を設置する。
  - `現在シーズンのランキング`
  - `シーズン別ランキング`
  - `現在シーズンのプレイヤー情報`
  - `シーズン別プレイヤー情報`
- button の visual order や row 配置は仕様の本体に含めない。

## Button 一覧

### `現在シーズンのランキング`

- `/info_thread command_name:leaderboard` と同等の操作を行う。
- 現在シーズンのランキング確認用 thread を作成する。

### `シーズン別ランキング`

- `/info_thread command_name:leaderboard_season` と同等の操作を行う。
- シーズン別ランキング確認用 thread を作成する。

### `現在シーズンのプレイヤー情報`

- `/info_thread command_name:player_info` と同等の操作を行う。
- 現在シーズンのプレイヤー情報確認用 thread を作成する。

### `シーズン別プレイヤー情報`

- `/info_thread command_name:player_info_season` と同等の操作を行う。
- シーズン別プレイヤー情報確認用 thread を作成する。

## 操作フロー

1. 登録済みユーザーが `レート戦情報` チャンネルの常設メッセージを開く。
2. 登録済みユーザーが、用途に応じた button を 1 つ押す。
3. Bot は、押下したユーザーにだけ見える ephemeral defer を先に返す。
4. Bot は、押下された button に対応する `command_name` を使って `/info_thread` と同じ業務処理を実行する。
5. 成功時は、`レート戦情報` チャンネル配下に情報確認用の private thread を 1 つ作成する。
6. 作成した thread には、少なくとも実行ユーザー、admin、Bot を参加させる。
7. 実行結果は、押下したユーザーにだけ見える followup のテキストメッセージで返す。

## 正常時の挙動

- 押下した button に対応する `command_name` で info thread 作成を試みる。
- `レート戦情報` チャンネル配下に、対応する用途の情報確認用 private thread を 1 つ作成する。
- thread 作成直後には、押下した用途に応じた案内メッセージを thread 内へ送る。
- button 押下直後には、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` 相当の本文データを自動では投稿しない。
- Bot は、作成した `thread_id` を実行ユーザーの最新の情報確認 thread として一時的に紐づける。
- 実行ユーザーに既存の紐づけがあっても、新しい thread を作成して最新紐づけを上書きする。
- 押下直後に ephemeral defer を返し、最終結果は ephemeral な followup のテキストメッセージで返す。
- 成功時の文言は以下とする。
  - `情報確認用スレッドを作成しました。`

## エラー時の挙動

- 情報確認用チャンネルが見つからない:
  - ephemeral な followup のテキストメッセージで `情報確認用チャンネルが見つかりません。管理者に確認してください。` を返す。
- 内部エラー:
  - ephemeral な followup のテキストメッセージで `情報確認用スレッドの作成に失敗しました。管理者に確認してください。` を返す。

## 可視性

- button 押下後の結果メッセージは、成功時も失敗時も押下したユーザーにだけ見えるようにする。
- `レート戦情報` チャンネルには、button 押下のたびに公開メッセージを追加送信しない。
- 情報本文の表示は、作成後の private thread に集約する。
- 公開チャンネル上の常設メッセージ自体は、button 押下のたびに内容を変更しなくてよい。

## 冪等性

- 同一ユーザーが短時間に複数回 button を押した場合でも、各押下は `/info_thread` と同じ扱いとし、押された回数分だけ新しい thread を作成してよい。
- `command_name` ごとの別管理は行わず、実行ユーザーごとに最新 1 件だけを保持する。
- 新しい thread を作成した後は、古い thread を最新の表示先として再利用しない。

## 既存コマンドとの対応

- `現在シーズンのランキング` -> `/info_thread command_name:leaderboard`
- `シーズン別ランキング` -> `/info_thread command_name:leaderboard_season`
- `現在シーズンのプレイヤー情報` -> `/info_thread command_name:player_info`
- `シーズン別プレイヤー情報` -> `/info_thread command_name:player_info_season`

## 関連仕様

- チャンネル構成は [registered_channels.md](registered_channels.md) を参照する。
- private thread 側の仕様は [info_thread.md](info_thread.md) を参照する。
- コマンド入出力仕様は [../commands/user-commands.md](../commands/user-commands.md) を参照する。
- UI 設置コマンドは [setup_channel.md](setup_channel.md) を参照する。
