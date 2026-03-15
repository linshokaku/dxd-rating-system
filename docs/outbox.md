# outbox / 非同期通知仕様

## 目的

- マッチングキュー関連の後続通知を Discord へ非同期に送る
- Bot プロセスのクラッシュや再起動があっても、通知の二重送信と送信漏れを避ける
- commands 層と通知実行層を疎結合に保つ

## 対象イベント

現時点では少なくとも以下を対象とする。

- `presence_reminder`
- `queue_expired`
- `match_created`

## 基本方針

- 通知の発火判断は service 層で行う
- 通知の実行は runtime 層で行う
- commands 層は通知先コンテキストを取得して service 層へ渡す
- 通知先コンテキストは DB に保存し、再起動後も復元できるようにする
- outbox event は、publish 時に必要な配送先情報を失わないようにする

## 配送先ポリシー

現時点では、後続通知の配送先は以下の方針とする。

- 通知は、関連するマッチングキュー系コマンドを打った channel に送る
- 通知メッセージの先頭には、そのコマンドを実行した人への mention を付ける
- mention 形式は `<@discord_user_id>` を用いる

補足:

- thread も Discord 上では channel として扱い、`channel_id` で識別する
- `guild_id` は送信先の検証やログ用途のため保持してよい
- このポリシーは暫定であり、将来は固定通知 channel や DM へ拡張する可能性がある

## 通知先コンテキスト

### 目的

- reminder / expire / match_created がコマンド実行後に発生しても、どこへ送るかを決定できるようにする
- startup sync や reconcile による回収時でも、元の送信先を復元できるようにする

### 最低限必要な情報

- `channel_id`
- `guild_id` または `NULL`
- `mention_discord_user_id`
- `recorded_at`

### 保存単位

マッチングキューでは、通知先コンテキストは少なくとも `waiting` 行ごとに保持できる必要がある。

理由:

- `join` ごとに新しい `waiting` 行が作られる
- `present` によって同じ `waiting` 行の期限が延長される
- reminder / expire は個別の `queue_entry_id` に対して発生する
- startup sync で `waiting` 行から reminder / expire の復元を行うため

## コマンド実行時の更新ルール

### join

- `join` 成功時に、新しく作成された `waiting` 行へ通知先コンテキストを保存する
- 保存する `channel_id` は `join` を実行した channel とする
- 保存する `mention_discord_user_id` は `join` を実行した Discord user ID とする

### present

- `present` 成功時に、対象の `waiting` 行の通知先コンテキストを上書きする
- 上書き後は、新しい reminder / expire はその最新コンテキストを使う

### leave

- `leave` 成功時は後続通知の対象外になるため、通知先コンテキスト更新は必須ではない
- `leave` が遅すぎて同期的に `expired` になった場合も、非同期通知は送らない

## イベント別の配送ルール

### `presence_reminder`

- 対象 `queue_entry_id` に紐づく最新の通知先コンテキストを使う
- 送信先はその `channel_id`
- mention 対象はその `mention_discord_user_id`
- メッセージ例:
  - `<@123456789012345678> 在席確認です。1分以内に在席更新がない場合はマッチングキューから外れます。`

### `queue_expired`

- 対象 `queue_entry_id` に紐づく最新の通知先コンテキストを使う
- 送信先はその `channel_id`
- mention 対象はその `mention_discord_user_id`
- メッセージ例:
  - `<@123456789012345678> 期限切れでマッチングキューから外れました。`

### `match_created`

- `match_created` は 1 件のマッチに複数のプレイヤーが含まれる
- 現時点では、参加した各 `queue_entry` ごとに通知先コンテキストを解決できるようにする
- runtime 層は、各参加者の通知先コンテキストに対して通知を配送する

現時点の配送方針:

- 同じ match について、参加者ごとに「その人が最後にコマンドを打った channel」へ送ってよい
- 各メッセージには、その通知先コンテキストに対応する `mention_discord_user_id` を付ける
- 同じ `(channel_id, mention_discord_user_id)` に重複する配送先があれば 1 回にまとめてよい

メッセージ例:

- `<@123456789012345678> マッチ成立です。対戦相手とチーム分けを確認してください。`

## outbox payload の要件

### 共通

- publisher が Discord API を叩く時点で、配送先の解決に必要な情報を取得できること
- event payload は、少なくとも event 自体の内容と配送先解決に必要な情報を持つこと

### 推奨

配送先コンテキストは、publish 時に別テーブルや別エンティティを再参照して解決してもよいが、現時点では以下のどちらかを満たすことを推奨する。

1. outbox payload に配送先スナップショットを直接入れる
2. payload に `queue_entry_id` / `queue_entry_ids` を入れ、publish 時にそこから配送先を決定できる

現時点の実装方針としては、次を想定する。

- `presence_reminder`
  - `queue_entry_id`
  - `player_id`
  - `revision`
  - `expire_at`
- `queue_expired`
  - `queue_entry_id`
  - `player_id`
  - `revision`
  - `expire_at`
- `match_created`
  - `match_id`
  - `queue_entry_ids`
  - `player_ids`
  - `teams`

ただし、runtime 層で配送先を確実に解決できるよう、別途 `queue_entry` 側に通知先コンテキストが保持されていることを前提とする。

## runtime 層の publish 仕様

- outbox dispatcher は未 publish の event を取得する
- event ごとに必要な配送先を解決する
- Discord 送信が成功した場合のみ `published_at` を更新する
- Discord 送信に失敗した場合は `published_at` を更新しない
- 配送先コンテキストが欠落している場合は内部エラーとして扱い、warning または error log を出す

## startup sync / 再起動との関係

- startup sync によって reminder / expire / match_created が回収されても、通知先は保存済みコンテキストから決定できる必要がある
- `join` commit 後にプロセスが落ちた場合でも、通知先コンテキストが DB に残っていれば再起動後に reminder / expire を送れる
- `join` commit 後にマッチング試行前にプロセスが落ちた場合でも、参加者ごとの通知先コンテキストが残っていれば、startup sync が作成した `match_created` 通知を配送できる

## commands 層への要求

- マッチングキュー系コマンドは、成功時に通知先コンテキストを service 層へ渡す
- 最低限、以下を取得できること
  - `interaction.channel_id`
  - `interaction.guild_id`
  - `interaction.user.id`

## 未決定事項

- `match_created` を 1 channel に集約するか、参加者ごとに個別 channel へ送るか
- 同じ match に対して複数 channel へ送る際の文面を統一するか
- mention 対象を「コマンド実行者」に固定するか、「プレイヤー本人」に寄せるか
- 将来 DM 通知を導入するか
