# マッチングキュー共通仕様

## 目的

`1v1`、`2v2`、`3v3` で共通のマッチングキュー管理を定義する。

本仕様が扱うのは以下である。

- キュー参加
- 在席更新
- キュー退出
- 在席確認リマインド
- 期限切れ
- マッチング試行の共通制御

フォーマットごとのマッチ構築方法は以下を参照する。

- [1v1.md](1v1.md)
- [2v2.md](2v2.md)
- [3v3.md](3v3.md)

## 前提

- Bot は複数プロセスで同時起動されうる
- 正しさは DB の状態とロックで担保する
- in-memory のタイマーは補助とし、DB を真実のソースとする
- 時刻判定は PostgreSQL の `now()` を基準にする

## 想定データ

キューは `match_queue_entries` のようなテーブルで管理する。

最低限必要なカラム:

- `id`
- `player_id`
- `match_format`
- `queue_class_id`
- `status`
- `joined_at`
- `last_present_at`
- `expire_at`
- `revision`
- `last_reminded_revision`
- `removed_at`
- `removal_reason`

`status` は少なくとも以下を持つ。

- `waiting`
- `left`
- `expired`
- `matched`

制約:

- `status = 'waiting'` の行は、1 プレイヤーにつき 1 件まで
- PostgreSQL の部分ユニークインデックスを利用する
  - 例: `UNIQUE (player_id) WHERE status = 'waiting'`

補足:

- 同時参加禁止はフォーマット横断で適用する
- `queue_class_id` は [queue_classes.md](queue_classes.md) の定義へ対応する

## テーブル分類

### 一時状態テーブル

- `match_queue_entries`
- `active_match_states`
- `active_match_player_states`
- `outbox_events`

### 永続データテーブル

- `seasons`
- `players`
- `player_format_stats`
- `matches`
- `match_participants`
- `match_reports`
- `finalized_match_results`
- `finalized_match_player_results`
- `match_admin_overrides`
- `player_penalties`
- `player_penalty_adjustments`
- `alembic_version`

## 共通ルール

- `join`、`present`、`leave` はプレイヤー単位で `pg_advisory_xact_lock(player_id)` を取得する
- 在席確認リマインドと `expire` は対象キュー行を `SELECT ... FOR UPDATE` でロックする
- cleanup バッチは `FOR UPDATE SKIP LOCKED` を使う
- `join` と `present` では `revision` を更新する
- `join` と `present` の commit 後には、在席確認リマインドタスクと `expire` タスクの両方を登録する
- `join` 成功後には、別トランザクションで参加先 `queue_class_id` を対象にマッチング試行を行う
- `left`、`expired`、`matched` になった行は再利用しない
- 再 join 時は新しい行を作る

## キュー定義と参加条件

### キュー定義

- キュー定義は [queue_classes.md](queue_classes.md) に従う
- キュー定義はフォーマットごとに表示順を保った順序付きで管理する
- 初期状態では各フォーマットに `beginner`、`regular`、`master` の 3 階級を持つ
- `beginner` はレート `1600` 未満、`master` は `1600` 以上、`regular` は全レート参加可能とする
- キュー定義の正は DB ではなくアプリケーションコード上の設定とする

### 参加条件の評価タイミング

- プレイヤーの参加可能条件は `join` 時にのみチェックする
- 参加条件判定に使うのは、参加時点で稼働中のシーズンに属する `player_format_stats.rating` である
- 対象シーズン行の `carryover_status = 'pending'` なら、参加条件判定の前に carryover を確定する
- `join` 成功後にそのフォーマットのレートが変化しても、待機中は再判定しない
- `present`、`leave`、起動時再同期、マッチ作成直前では参加条件を再チェックしない

### シーズン切替時の扱い

- シーズン切替時に `waiting` 行は削除しない
- シーズン切替前に参加した待機行も、そのままマッチング対象に含める
- 既存の待機行に対して、シーズン切替を理由とした参加条件の再判定は行わない
- そのため、待機中にシーズンが切り替わると、`queue_class_id` とマッチ作成時点の実際のレート帯がずれる場合がある
- このずれは仕様として許容し、過度な複雑化を避ける

## 状態遷移

- `waiting -> waiting`
  - `present`
- `waiting -> left`
  - `leave`
- `waiting -> expired`
  - `expire`
- `waiting -> matched`
  - マッチ成立

## `join`

### 入力

- `match_format`
- `queue_name`

### 成功条件

- プレイヤーが登録済みである
- 指定した `match_format` が有効である
- 指定した `match_format` と `queue_name` の組み合わせが有効である
- 指定フォーマットの現在レートが、そのキューの参加条件を満たす
- 有効な `waiting` 行を持っていない

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 入力された `match_format` と `queue_name` を `queue_class_id` へ解決する
4. 対象プレイヤーの `player_format_stats` から、その `match_format` の現在レートを取得する
5. 取得した行の `carryover_status = 'pending'` なら、先に carryover を確定する
6. 参加可否を判定する
7. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
8. 行があり、かつ `expire_at > now()` なら失敗する
9. 行があり、かつ `expire_at <= now()` なら、その行を `expired` に更新する
10. 新しい `waiting` 行を作成する
11. `match_format = input_match_format` を設定する
12. `queue_class_id = resolved_queue_class_id` を設定する
13. `joined_at = now()`
14. `last_present_at = now()`
15. `expire_at = now() + interval '5 minutes'`
16. `revision = 1`
17. `last_reminded_revision = NULL`
18. commit する
19. commit 後に在席確認リマインドタスクと `expire` タスクを登録する
20. commit 後に、別トランザクションで参加先 `queue_class_id` を対象にマッチング試行を行う

### 失敗時の応答

- `match_format` が無効な場合
  - `指定したフォーマットは存在しません。`
- `queue_name` が存在しない場合
  - `指定したキューは存在しません。`
- 指定フォーマットの現在レートでは参加できない場合
  - `現在のレーティングではそのキューに参加できません。現在レート: {rating}`
- すでにキュー参加中の場合
  - `すでにキュー参加中です。`

### 成功時の応答

- `キューに参加しました。5分間マッチングします。`

## `present`

### 成功条件

- `status = 'waiting'` の有効なキュー行が存在する

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
4. 行がなければ失敗する
5. `expire_at <= now()` なら、その場で `expired` に更新して commit する
6. 行が有効なら `last_present_at = now()` に更新する
7. `expire_at = now() + interval '5 minutes'` に更新する
8. `revision = revision + 1` に更新する
9. `last_reminded_revision = NULL` に更新する
10. commit する
11. commit 後に新しい `revision` で在席確認リマインドタスクと `expire` タスクを登録する

### 補足

- `present` は現在参加中の `waiting` 行に対して暗黙適用する
- `match_format` や `queue_name` の入力は受け取らない
- 参加条件は再判定しない
- 在席確認 thread の `在席報告` ボタンも、同じ `present` 処理を呼び出す

### 失敗時の応答

- キューに参加していない場合
  - `キューに参加していません。`
- 期限切れだった場合
  - `期限切れのためキューから外れました。`

### 成功時の応答

- `在席を更新しました。次の期限は5分後です。`

## `leave`

### 成功条件

- `status = 'waiting'` の有効なキュー行が存在する

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
4. 行がなければ冪等に成功扱いとする
5. `expire_at <= now()` なら、その場で `expired` に更新して commit する
6. 行が有効なら `status = 'left'` に更新する
7. `removed_at = now()` を設定する
8. `removal_reason = 'user_leave'` を設定する
9. commit する
10. commit 後にローカルの在席確認リマインドタスクと `expire` タスクを cancel する

### 推奨 UX

- `leave` は現在参加中の `waiting` 行に対して暗黙適用する
- `match_format` や `queue_name` の入力は受け取らない
- `waiting` 行がない場合は冪等に成功扱いにする
- 応答例: `キューから退出しました。`
- 在席確認 thread の `離席` ボタンも、同じ `leave` 処理を呼び出す

## 在席確認リマインド

### 目的

- `expire_at` の 1 分前時点で、まだ `waiting` のプレイヤーへ在席確認を促す
- 1 回の `join` または `present` に対して、リマインドは最大 1 回だけ送る

### 発火条件

- `status = 'waiting'`
- `expire_at - interval '1 minute' <= now() < expire_at`
- `last_reminded_revision IS NULL` または `last_reminded_revision != revision`

### 処理

1. トランザクションを開始する
2. 対象キュー行を `FOR UPDATE` で取得する
3. 行がなければ no-op
4. `status != 'waiting'` なら no-op
5. `revision != expected_revision` なら no-op
6. `expire_at <= now()` なら no-op
7. `expire_at - interval '1 minute' > now()` なら no-op
8. `last_reminded_revision = revision` なら no-op
9. 条件を満たした場合のみ `last_reminded_revision = revision` に更新する
10. 必要なら同一トランザクション内で outbox event を作る
11. commit する

### 通知内容

- `在席確認です。1分以内に在席更新がない場合はマッチングキューから外れます。`

### 通知方式

- 通常ユーザー操作で作成された待機では、タイマー通知は参加時に作成された在席確認 thread に送る。
- 在席確認 thread でのリマインド UI は [../ui/matchmaking_presence_thread.md](../ui/matchmaking_presence_thread.md) を参照する。
- 開発者コマンド操作で作成された待機では、従来どおり通知先コンテキストに保存された最新の `channel_id` へ通常のテキストメッセージとして送る。
- 開発者コマンド操作の通知メッセージ先頭には、対象ユーザーへの mention を付ける。

## `expire`

### 発火元

- `join` 後に登録された単発タスク
- `present` 後に登録された単発タスク
- プロセス起動時の cleanup
- 保険用の定期 reconcile ループ

### 処理

1. トランザクションを開始する
2. 対象キュー行を `FOR UPDATE` で取得する
3. 行がなければ no-op
4. `status != 'waiting'` なら no-op
5. `revision != expected_revision` なら no-op
6. `expire_at > now()` なら no-op
7. 条件を満たした場合のみ `status = 'expired'` に更新する
8. `removed_at = now()` を設定する
9. `removal_reason = 'timeout'` を設定する
10. 必要なら同一トランザクション内で outbox event を作る
11. commit する

### 通知内容

- `期限切れでマッチングキューから外れました。`

### 通知方式

- 期限切れ通知は、在席確認リマインドと同じ通知方式を使う。
- 通常ユーザー操作で作成された待機では、期限切れ通知は在席確認 thread に送る。
- 在席確認 thread での期限切れ後 UI は [../ui/matchmaking_presence_thread.md](../ui/matchmaking_presence_thread.md) を参照する。
- 開発者コマンド操作で作成された待機では、従来どおり通知先コンテキストに保存された最新の `channel_id` へ通常のテキストメッセージとして送る。
- 開発者コマンド操作の通知メッセージ先頭には、対象ユーザーへの mention を付ける。

## 単発 reminder / expire タスクの retry 方針

- `join` / `present` 後に登録される単発 reminder / expire タスクには in-memory retry を持たせてよい
- retryable な一時失敗だけを service 層で包括例外へ変換し、scheduler はその例外だけを指数バックオフで再登録する
- retry 状態は in-memory で持ち、DB に専用状態は保存しない
- `leave`、`matched`、`expired`、runtime stop 時は pending retry も含めて cancel する
- `presence_reminder` は元の `expire_at` を過ぎる retry を新規登録しない
- `expire` は他経路で no-op になるまで retry を継続してよい

## マッチング試行

### 目的

- `join` 成功後に、参加先キューでマッチ成立可能かを即時に試す
- 各 `queue_class_id` ごとに独立してマッチを作成する
- すでに十分な待機人数がある場合、可能な限り連続でマッチを作成する

### 発火元

- `join` 成功後
- プロセス起動時・再起動時の再同期処理

### 実行方針

- `join` と同一トランザクションでは実行しない
- `join` commit 後に別トランザクションで、参加先 `queue_class_id` を対象に実行する
- `try_create_matches()` の失敗によって `join` 自体は失敗させない

### 候補抽出条件

- `status = 'waiting'`
- `expire_at > now()`
- 対象 `queue_class_id` と一致する
- `joined_at` の古い順を優先する

### ロック方針

- 候補行は対象 `queue_class_id` に絞った上で `ORDER BY joined_at, id`
- `SELECT ... FOR UPDATE SKIP LOCKED` で取得する
- 必要人数は、対象フォーマットの `players_per_batch` とする
- 必要人数に満たない場合は no-op で終了する

### 成立時の処理

1. 候補プレイヤーをロックして取得する
2. 対象 `match_format` に応じて、対応するフォーマット別仕様でマッチ構築する
3. 作成したすべての試合について `matches`、`match_participants`、必要な active state を作成する
4. 対応するキュー行を `matched` に更新する
5. commit する
6. commit 後に、作成された各 `match_id` ごとに `match_created` 通知を送る

### 通知方針

- `join` の応答は先に返す
- `join` をきっかけにマッチ成立した場合は、後続の別通知で `マッチ成立` を伝える
- `join` 応答を `マッチ成立` 通知で置き換えない

### 補足

- `matched` になった行に対して reminder / expire タスクが起きても no-op にできる
- 起動時再同期でも `try_create_matches()` を実行し、十分人数がそろっているケースを取りこぼさない
