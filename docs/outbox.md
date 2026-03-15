# outbox / 非同期通知仕様

## 目的

- マッチングキュー関連の後続通知を Discord へ非同期に送る
- Bot プロセスのクラッシュや再起動があっても、通知の二重送信と送信漏れを避ける
- commands 層と通知実行層を疎結合に保つ
- 常時 polling による待機コストを抑える

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
- `outbox_events` を通知の真実のソースとする
- PostgreSQL の `LISTEN/NOTIFY` は durable なキューではなく、「新着がある」ことを runtime に知らせる起床トリガーとして使う
- 低コスト化のため、通常時の配送開始は polling ではなく `LISTEN/NOTIFY` を主経路とする
- `LISTEN/NOTIFY` の取りこぼし対策として、低頻度の保険用 polling を併用する

## アーキテクチャ方針

### service 層

- service 層は DB トランザクション内で `outbox_events` に event を作成する
- 同じ DB トランザクション内で `NOTIFY` を発行してよい
- ただし Discord 送信そのものは DB トランザクション内で行わない

補足:

- `NOTIFY` は transaction commit 後に配送されることを前提とする
- transaction が rollback された場合、その transaction で発行した `NOTIFY` も配送されないことを前提とする

### runtime 層

- runtime 層は専用の listener 接続で `LISTEN` する
- `NOTIFY` を受けたら、未 publish の event を取得して配送する
- Discord 送信が成功した場合のみ `published_at` を更新する
- Discord 送信に失敗した場合は `published_at` を更新しない
- listener 再接続直後と startup 時には catch-up を走らせ、未配送 event を回収する
- listener が何らかの理由で通知を取りこぼしても回収できるよう、数分単位の低頻度 polling を保険として実行する

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

## `LISTEN/NOTIFY` と保険用 polling

### 主経路

- event 作成 transaction は `outbox_events` への insert と `NOTIFY` を同じ transaction に含める
- listener は専用接続で `LISTEN` し、通知を受けたら dispatch を開始する
- dispatch は pending event がなくなるまで繰り返してよい

### catch-up

- startup 時には、listener の待受開始後に catch-up を 1 回走らせる
- listener 接続が切断され、再接続に成功した直後も catch-up を 1 回走らせる

意図:

- listener 切断中に飛んだ `NOTIFY` を取りこぼしても、`outbox_events` に残っている未配送 event を回収できるようにする

### listener 切断時の処理

- listener は PostgreSQL への専用接続で `LISTEN` しているものとする
- `LISTEN` の登録は接続にひもづくため、その接続が切れたら `LISTEN` 状態も失われる前提で扱う
- listener ループは、通知待受中または接続確認中に接続断や `OperationalError` 相当を検知したら、その接続を破棄する
- 接続断を検知したら、runtime は再接続ループへ移行する
- 再接続ループでは新しい PostgreSQL 接続を張り直し、成功後に再度 `LISTEN` を実行する
- `LISTEN` の再登録に成功した時点で listener 復旧とみなす
- listener 復旧直後には catch-up を 1 回走らせる

再接続 backoff:

- 初回待機は `1s` とする
- 以後、失敗のたびに待機時間を 2 倍にする
- 上限は `512s` とする
- 想定する待機列は `1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s, 512s` とする
- `512s` 到達後は、復旧するまで `512s` 間隔で再試行を継続する
- 再接続と `LISTEN` の再登録が成功したら、backoff は `1s` にリセットする

補足:

- この再接続ループは listener 接続の復旧のための retry であり、`outbox_events` を常時問い合わせる polling とは区別する
- 通常時の配送開始は `LISTEN/NOTIFY` を主経路とし、listener が健全な間は outbox テーブルの高頻度 polling は行わない
- listener 断中の通知取りこぼしは、再接続直後の catch-up と保険用 polling で回収する

### 保険用 polling

- `LISTEN/NOTIFY` を主経路としつつ、数分単位の低頻度 polling を保険として残す
- 保険用 polling は、listener の通知取りこぼしや想定外の不整合を検知・回収するためのものとする
- 保険用 polling で pending event を見つけて配送まで進んだ場合、その事実を warning log として残す
- 保険用 polling で event が見つからなかった場合は warning を出さない

warning log の意図:

- 本来は `LISTEN/NOTIFY` で起床できるはずなので、保険経由の配送は「通知の取りこぼしが起きた可能性がある」ことを運用上すぐに気づけるようにする

## 一時失敗時の retry / backoff

### 基本方針

- Discord 送信の一時失敗に対しては、durable な retry を行う
- retry の待機状態は in-memory だけでなく DB にも保持する
- 長い待機を dispatcher 本体で `sleep` して抱え込まない
- DB トランザクションや row lock を保持したまま待機しない

### 必要な状態

- outbox event は、少なくとも以下の retry 状態を保持できること
  - `failure_count`
  - `next_attempt_at`
- 必要に応じて、運用調査用に以下を保持してよい
  - `last_error`
  - `last_failed_at`

### 一時失敗時の更新ルール

- Discord 送信が一時失敗した場合、`published_at` は更新しない
- 代わりに `failure_count` を増やし、次回の再試行時刻として `next_attempt_at` を更新する
- 失敗情報の更新は DB に commit して確定させる
- commit 後に、`next_attempt_at` に対応する in-process timer を登録する

### backoff 方式

- retry の待機時間は指数バックオフとする
- 初回待機は `1s` とする
- 以後、失敗のたびに待機時間を 2 倍にする
- 上限は `512s` とする
- 想定する待機列は `1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s, 512s` とする
- `512s` 到達後は、成功するまで `512s` 間隔で再試行する
- 送信が成功したら、その event の retry 状態は完了扱いとする

### in-process timer の役割

- timer は `next_attempt_at` まで `sleep` し、期限到来後に dispatch を起動する
- timer は「その event を再試行可能時刻に起こす」ことだけを責務とする
- 実際の送信対象判定は DB 上の状態を見て行う
- dispatch 対象は、少なくとも `published_at IS NULL` かつ `next_attempt_at <= now()` の event とする

### startup / 再起動時の再構築

- runtime 起動時には、未 publish かつ `next_attempt_at` が将来時刻の event について timer を再構築してよい
- `next_attempt_at <= now()` の event は、startup catch-up または通常 dispatch で即時回収できること
- runtime 再起動で timer が失われても、DB に retry 状態が残っていれば復旧可能であること

### `LISTEN/NOTIFY` と retry timer の関係

- `LISTEN/NOTIFY` は新規 event の即時配送開始の主経路とする
- retry timer は、一時失敗した既存 event を将来時刻に再試行するための補助経路とする
- 低頻度 polling は、listener の取りこぼしや timer 再構築漏れを回収する最後の保険とする

## runtime 層の publish 仕様

- outbox dispatcher は、少なくとも `published_at IS NULL` かつ `next_attempt_at <= now()` の event を取得する
- event ごとに必要な配送先を解決する
- Discord 送信が成功した場合のみ `published_at` を更新する
- Discord 送信が一時失敗した場合は `published_at` を更新せず、retry 状態を更新する
- 配送先コンテキストが欠落している場合は内部エラーとして扱い、warning または error log を出す
- `LISTEN/NOTIFY` 起因の dispatch と保険用 polling 起因の dispatch を区別してログできるようにする
- retry timer 起因の dispatch も区別してログできるようにする
- 保険用 polling 起因で 1 件以上 publish できた場合は warning log を出す

## startup sync / 再起動との関係

- startup sync によって reminder / expire / match_created が回収されても、通知先は保存済みコンテキストから決定できる必要がある
- `join` commit 後にプロセスが落ちた場合でも、通知先コンテキストが DB に残っていれば再起動後に reminder / expire を送れる
- `join` commit 後にマッチング試行前にプロセスが落ちた場合でも、参加者ごとの通知先コンテキストが残っていれば、startup sync が作成した `match_created` 通知を配送できる
- runtime 再起動時は、listener 待受開始後の catch-up、retry timer の再構築、保険用 polling により未配送 event を回収できるようにする

## commands 層への要求

- マッチングキュー系コマンドは、成功時に通知先コンテキストを service 層へ渡す
- 最低限、以下を取得できること
  - `interaction.channel_id`
  - `interaction.guild_id`
  - `interaction.user.id`

## 未決定事項

- `LISTEN/NOTIFY` の channel 名を固定文字列にするか、設定値にするか
- 保険用 polling の間隔を固定値にするか、設定値にするか
- `match_created` を 1 channel に集約するか、参加者ごとに個別 channel へ送るか
- 同じ match に対して複数 channel へ送る際の文面を統一するか
- mention 対象を「コマンド実行者」に固定するか、「プレイヤー本人」に寄せるか
- 将来 DM 通知を導入するか
