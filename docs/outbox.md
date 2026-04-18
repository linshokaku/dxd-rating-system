# outbox / 非同期通知仕様

## 目的

- マッチングキュー関連、公開アナウンス、運用通知の後続通知を Discord へ非同期に送る
- Bot プロセスのクラッシュや再起動があっても、通知の二重送信と送信漏れを避ける
- commands 層と通知実行層を疎結合に保つ
- 常時 polling による待機コストを抑える

## 対象イベント

現時点では少なくとも以下を対象とする。

- `presence_reminder`
- `queue_expired`
- `match_created`
- `season_completed`
- `season_top_rankings`
- `admin_operations_notification`

## 基本方針

- 通知の発火判断は service 層で行う
- worker のような command 起点ではない処理も、必要なら outbox event を作成してよい
- 通知の実行は runtime 層で行う
- commands 層は通知先コンテキストを取得して service 層へ渡す
- 通知先コンテキストは DB に保存し、再起動後も復元できるようにする
- outbox event は、publish 時に必要な配送先情報を失わないようにする
- `outbox_events` を通知の真実のソースとする
- PostgreSQL の `LISTEN/NOTIFY` は durable なキューではなく、「新着がある」ことを runtime に知らせる起床トリガーとして使う
- 低コスト化のため、通常時の配送開始は polling ではなく `LISTEN/NOTIFY` を主経路とする
- `LISTEN/NOTIFY` の取りこぼし対策として、低頻度の保険用 polling を併用する

補足:

- `outbox_events` は通知配送のための一時状態テーブルとして扱う
- システム全体の完全ダウン後に、未送信通知や配送状態を破棄してよい運用判断をした場合は、
  `outbox_events` を初期化対象に含めてよい
- ただし通常運用では、`outbox_events` は再起動後の配送回復に使うため、安易に削除しない

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

- 通常ユーザー操作の `presence_reminder` と `queue_expired` は、参加時に作成された在席確認 thread の `channel_id` 宛てに送る
- 通常ユーザー操作の `presence_reminder` と `queue_expired` の UI は [ui/matchmaking_presence_thread.md](ui/matchmaking_presence_thread.md) に従う
- 開発者コマンド操作の `presence_reminder` と `queue_expired` も、`/dev_join` 成功時に作成された在席確認 thread の `channel_id` 宛てに送る
- 開発者コマンド操作の `presence_reminder` と `queue_expired` も、公開チャンネルではなく在席確認 thread 内で行う
- `match_created` は、公開の `レート戦マッチ速報` チャンネル通知を維持したうえで、在席確認 thread がある参加者にだけその thread へ送る
- `season_completed` は、`system_announcements_channel` が設置済みのときだけその channel へ送る
- `season_top_rankings` は、`system_announcements_channel` が設置済みのときだけその channel へ送る
- `admin_operations_notification` は、`admin_operations_channel` が設置済みのときだけその channel へ送る
- 在席確認 thread 向けの `match_created` には、対象参加者への mention を付ける
- `match_created` を `レート戦マッチング` 親チャンネルや通常通知先 channel へフォールバックして送らない
- mention 形式は `<@discord_user_id>` を用いる

補足:

- thread も Discord 上では channel として扱い、`channel_id` で識別する
- `guild_id` は送信先の検証やログ用途のため保持してよい
- `レート戦マッチング` チャンネルの UI から参加した場合は、参加成功後に同チャンネル配下へ作成された在席確認 thread をタイマー通知の最新送信先として扱う

## 通知先コンテキスト

### 目的

- reminder / expire / match_created がコマンド実行後に発生しても、どこへ送るかを決定できるようにする
- startup sync や reconcile による回収時でも、元の送信先を復元できるようにする

### 最低限必要な情報

タイマー通知と `match_created` を channel 宛てに送るため、通知先コンテキストは少なくとも以下を保持できる必要がある。

- `channel_id`
- `guild_id` または `NULL`
- `mention_discord_user_id`
- `recorded_at`

補足:

- `channel_id` は、`presence_reminder`、`queue_expired`、`match_created` の配送先決定に使う。
- `mention_discord_user_id` は、開発者コマンド操作の `presence_reminder` と `queue_expired` の先頭 mention、または通常ユーザー向け UI の対象者識別に使う。

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
- 通常ユーザーの `join` では、`presence_reminder` と `queue_expired` の送信先として、参加時に `レート戦マッチング` チャンネル配下へ作成された在席確認 thread の `channel_id` を保存する
- 開発者コマンドの `join` でも、`presence_reminder` と `queue_expired` の送信先として、参加時に `レート戦マッチング` チャンネル配下へ作成された在席確認 thread の `channel_id` を保存する
- `match_created` の送信先としては、その時点で保存されている通知先コンテキストの `channel_id` を保存する
- 保存する `mention_discord_user_id` は、通常ユーザー操作では `join` を実行した Discord user ID、開発者コマンド操作では対象ユーザーの Discord user ID とする
- `レート戦マッチング` チャンネルの UI から参加した場合も、参加成功後に作成した在席確認 thread の `channel_id` を同じ形式で保存する

### present

- `present` 成功時に、対象の `waiting` 行の通知先コンテキストを上書きする
- 通常ユーザーの待機では、上書き後も `presence_reminder` と `queue_expired` の送信先は既存の在席確認 thread の `channel_id` を維持する
- 開発者コマンドの `present` でも、`presence_reminder` と `queue_expired` の送信先は既存の在席確認 thread の `channel_id` を維持する

### leave

- `leave` 成功時は後続通知の対象外になるため、通知先コンテキスト更新は必須ではない
- `leave` が遅すぎて同期的に `expired` になった場合も、非同期通知は送らない

## イベント別の配送ルール

### `presence_reminder`

- service 層は、対象 `queue_entry_id` に紐づく最新の通知先コンテキストの `channel_id` を使って event を作成する
- outbox payload には、その時点の送信先スナップショットを含める
- 送信先は payload 内の `destination.channel_id`
- 表示対象ユーザーは payload 内の `mention_discord_user_id`
- 通常ユーザー操作では、通知は在席確認 thread に送る
- 開発者コマンド操作でも、通知は在席確認 thread に送る
- メッセージ例:
  - `在席確認です。1分以内に在席更新がない場合はマッチングキューから外れます。`

### `queue_expired`

- service 層は、対象 `queue_entry_id` に紐づく最新の通知先コンテキストの `channel_id` を使って event を作成する
- outbox payload には、その時点の送信先スナップショットを含める
- 送信先は payload 内の `destination.channel_id`
- 表示対象ユーザーは payload 内の `mention_discord_user_id`
- 通常ユーザー操作では、通知は在席確認 thread に送る
- 開発者コマンド操作でも、通知は在席確認 thread に送る
- メッセージ例:
  - `期限切れでマッチングキューから外れました。`

### `match_created`

- `match_created` は 1 件の `match_id` ごとに 1 回のマッチ成立通知を表す
- `1v1` の 1 バッチ 2 マッチは、2 件の `match_created` 通知として扱う
- service 層は、参加した各 `queue_entry` の通知先コンテキストをもとに配送先を集約する
- 実際の Discord 送信 1 件ごとに 1 outbox event を作成する
- 各 event の payload には、送信先スナップショット、`match_format`、表示用チーム情報を含める

現時点の配送方針:

- 同じ match について、公開のマッチ速報チャンネルへ 1 回送る
- 在席確認 thread がある参加者ごとに、その thread へ 1 回送る
- `match_created` を `レート戦マッチング` 親チャンネルへ直接送らない
- 公開チャンネル向け `match_created` のメッセージ先頭には mention を付けない
- 在席確認 thread 向け `match_created` は、対象プレイヤーへの mention を先頭に付けてよい

メッセージ例:

```text
マッチ成立です。
Team A
<@123456789012345678>
<@234567890123456789>
Team B
<@345678901234567890>
<@456789012345678901>
```

### `admin_operations_notification`

- `admin_operations_notification` は admin 向け運用通知 1 件を表す。
- 初期スコープでは `daily worker` の起動通知だけを対象とする。
- service 層または worker は、通知先の `admin_operations_channel` が解決できた場合だけ event を作成する。
- 通知先チャンネルが解決できない場合、worker 本体は処理を継続し、通知だけをスキップしてよい。
- payload には、その時点の送信先スナップショットを含める。
- 送信先は payload 内の `destination.channel_id`
- 初期 `notification_kind` は `daily_worker_started` とする。
- メッセージ例:
  - `daily worker が起動しました。`

### `season_completed`

- `season_completed` は、1 シーズンの完了時に送る summary 通知 1 件を表す。
- `season_completed` は、`update_season_completion` が実際に `True` を返したときだけ作成する。
- マッチ finalize 経由でも日次 worker 経由でも、発火条件は `update_season_completion` の完了遷移に統一する。
- 同一 `season_id` の `season_completed` は 1 回だけ送る前提とし、重複 enqueue を避ける。
- service 層または worker は、通知先の `system_announcements_channel` が解決できた場合だけ event を作成する。
- 通知先チャンネルが解決できない場合でも、シーズン完了処理自体は成功扱いのまま継続し、通知だけを warning ログでスキップしてよい。
- 1 outbox event = 1 Discord メッセージの原則を維持する。
- payload には、その時点の送信先スナップショットを含める。
- 送信先は payload 内の `destination.channel_id`
- payload は少なくとも以下を含む。
  - `season_id`
  - `season_name`
  - `completed_at`
  - `destination`
- 本文は、該当シーズンの全マッチが完了したことが分かる簡潔なプレーンテキストとする。
- メッセージには、少なくとも `season_name` と `season_id` を含める。
- `completed_at` を簡潔に表示してよい。
- mention や button は付けない。

### `season_top_rankings`

- `season_top_rankings` は、1 シーズン完了時の形式別 Top 12 順位表通知 1 件を表す。
- `season_top_rankings` は、`season_completed` と別 event type とする。
- `admin_operations_notification` のような同一責務内の subtype 分岐とは異なり、season 系は summary 通知と順位表通知で payload、dedupe 単位、renderer の関心事が異なるため event type を分ける。
- `season_top_rankings` は、`update_season_completion` が実際に `True` を返したときだけ作成する。
- マッチ finalize 経由でも日次 worker 経由でも、発火条件は `update_season_completion` の完了遷移に統一する。
- 1 シーズン完了あたり、`season_completed` を 1 件 enqueue した後に、`season_top_rankings` を `1v1`、`2v2`、`3v3` で各 1 件 enqueue する。
- 投稿順は `season_completed` -> `1v1` -> `2v2` -> `3v3` の固定順とし、形式順は `MATCH_FORMAT_DEFINITIONS` に従う。
- 同一 `season_id` と `match_format` の組み合わせに対する `season_top_rankings` は 1 回だけ送る前提とし、重複 enqueue を避ける。
- service 層または worker は、通知先の `system_announcements_channel` が解決できた場合だけ event を作成する。
- 通知先チャンネルが解決できない場合でも、シーズン完了処理自体は成功扱いのまま継続し、通知だけを warning ログでスキップしてよい。
- 1 outbox event = 1 Discord メッセージの原則を維持する。
- payload には、その時点の送信先スナップショットを含める。
- 送信先は payload 内の `destination.channel_id`
- payload は少なくとも以下を含む。
  - `season_id`
  - `season_name`
  - `completed_at`
  - `match_format`
  - `entries`
  - `destination`
- `entries` は最大 12 件とし、各要素は少なくとも以下を含む。
  - `rank`
  - `display_name`
  - `rating`
- 完了したその `season_id` の `player_format_stats` を参照し、対象 `match_format` の Top 12 ランキングを送る。
- 対象プレイヤー、並び順、順位の定義は `leaderboard_season` と同じ規則を使う。
- 本文は簡潔形式とし、`season_id`、`season_name`、`match_format` と `順位 / ユーザー名 / rating` の Top 12 を表示する。
- `1d` / `3d` / `7d` の順位差分、ページング、button、thread は付けない。
- 各 `match_format` のランキング対象者が 0 人でも、その形式のメッセージは省略せず、`entries=[]` を許容し、本文には `対象者なし` 相当の 1 行を含める。
- mention や button は付けない。

## outbox payload の要件

### 共通

- publisher が Discord API を叩く時点で、配送先の解決に必要な情報を取得できること
- event payload は、少なくとも event 自体の内容と配送先解決に必要な情報を持つこと

### 推奨

配送先コンテキストは、`queue_entry` 側にも保持するが、publish 時に別テーブルを再参照しなくても配送できるよう、outbox payload に配送先スナップショットを直接入れる。

現時点の実装方針としては、次を想定する。

- `presence_reminder`
  - `queue_entry_id`
  - `player_id`
  - `revision`
  - `expire_at`
  - `destination`
  - `mention_discord_user_id`
- `queue_expired`
  - `queue_entry_id`
  - `player_id`
  - `revision`
  - `expire_at`
  - `destination`
  - `mention_discord_user_id`
- `match_created`
  - `match_id`
  - `match_format`
  - `queue_entry_ids`
  - `player_ids`
  - `destination`
  - `team_a_discord_user_ids`
  - `team_b_discord_user_ids`
  - `mention_discord_user_id` (在席確認 thread 向け payload のみ)
  - `match_operation_thread_parent_channel_id` (マッチ運営 thread 導線を解決する payload)
  - `create_match_operation_thread` (必要ならマッチ運営 thread を先に作成できる payload)
- `season_completed`
  - `season_id`
  - `season_name`
  - `completed_at`
  - `destination`
- `season_top_rankings`
  - `season_id`
  - `season_name`
  - `completed_at`
  - `match_format`
  - `entries`
  - `destination`
- `admin_operations_notification`
  - `notification_kind`
  - `worker_name`
  - `occurred_at`
  - `destination`

補足:

- `queue_entry` 側の通知先コンテキストは、`present` 後の最新状態管理と startup sync のために引き続き保持する
- publish 時の Discord 送信先解決は、基本的に outbox payload のスナップショットだけで完結させる
- `presence_reminder` と `queue_expired` の `destination` には、保存済み通知先コンテキストの channel スナップショットを入れる

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

- startup sync によって reminder / expire / match_created が回収されても、`presence_reminder`、`queue_expired`、`match_created` は保存済み通知先コンテキストから通知先を決定できる必要がある
- `join` commit 後にプロセスが落ちた場合でも、通知先コンテキストが DB に残っていれば、再起動後に reminder / expire を送れる
- `join` commit 後にマッチング試行前にプロセスが落ちた場合でも、参加者ごとの通知先コンテキストが残っていれば、startup sync が作成した `match_created` 通知を配送できる
- runtime 再起動時は、listener 待受開始後の catch-up、retry timer の再構築、保険用 polling により未配送 event を回収できるようにする

## commands 層への要求

- マッチングキュー系コマンドは、成功時に通知先コンテキストを service 層へ渡す
- 最低限、以下を取得できること
  - `interaction.user.id`
  - `interaction.guild_id`
- 通常ユーザーの `join` と開発者コマンドの `join` では、参加成功後に作成した在席確認 thread の `channel_id` を保存できること
- 開発者コマンドと通常ユーザーの `present` では、既存通知先コンテキストを参照し、在席確認 thread の `channel_id` を維持できること

## 未決定事項

- `LISTEN/NOTIFY` の channel 名を固定文字列にするか、設定値にするか
- 保険用 polling の間隔を固定値にするか、設定値にするか
- `match_created` を 1 channel に集約するか、参加者ごとに個別 channel へ送るか
- 同じ match に対して複数 channel へ送る際の文面を統一するか
- mention 対象を「コマンド実行者」に固定するか、「プレイヤー本人」に寄せるか
- `admin_operations_notification` の対象イベントを今後どこまで増やすか
