# マッチングキュー仕様

## 目的

Discord Bot 上で 3v3 対戦向けのマッチングキューを管理する。

- プレイヤーはキューに参加できる
- プレイヤーは在席を更新できる
- プレイヤーはキューから退出できる
- 一定時間在席更新がないプレイヤーは expire される
- expire の 1 分前に、まだ `matched` していないプレイヤーへ在席確認リマインドを送る
- 複数プロセスで Bot が起動していても、二重 expire や二重通知を避ける

## 前提

- Bot は複数プロセスで同時起動される
- すべてのプロセスが expire 処理と在席確認リマインド処理を担当する
- 正しさは DB の状態とロックで担保する
- in-memory のタイマーは補助的な仕組みとし、DB を真実のソースとする
- 時刻判定は PostgreSQL の `now()` を基準にする

## 想定データ

キューは `match_queue_entries` のようなテーブルで管理する。

最低限必要なカラム:

- `id`
- `player_id`
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

- `status = 'waiting'` の行は、1プレイヤーにつき 1 件まで
- PostgreSQL の部分ユニークインデックスを利用する
  - 例: `UNIQUE (player_id) WHERE status = 'waiting'`

補足:

- `queue_class_id` は、どのマッチングキューへ参加したかを表す内部識別子である
- キュー定義の正は DB ではなくアプリケーションコード上の定数とする
- ユーザーは `queue_class_id` を直接指定せず、`/join` の `queue_name` から解決する

## テーブル分類

運用上、このプロジェクトのテーブルは「完全ダウン後に破棄してよい一時状態」と
「再作成してはいけない永続データ」を分けて扱う。

### 一時状態テーブル

- `match_queue_entries`
- `active_match_states`
- `active_match_player_states`
- `outbox_events`

意図:

- `match_queue_entries` は現在の待機状態を表す一時状態である
- `active_match_states` と `active_match_player_states` は進行中試合の一時状態を表す
- `outbox_events` は未送信または送信済み通知の配送状態を表す一時状態である

補足:

- システムが完全にダウンし、待機中キューや未通知イベント、進行中試合の一時状態などの
  一時状態を破棄してよいと判断した場合、上記テーブルは初期化対象として扱ってよい

### 永続データテーブル

- `players`
- `matches`
- `match_participants`
- `match_reports`
- `finalized_match_results`
- `finalized_match_player_results`
- `match_admin_overrides`
- `player_penalties`
- `player_penalty_adjustments`
- `alembic_version`

意図:

- `players` はプレイヤー登録情報とレーティングを保持する永続データである
- `matches`、`match_participants`、`finalized_match_results`、
  `finalized_match_player_results` は対戦履歴および
  将来のレーティング再計算に必要な永続データである
- `match_reports`、`match_admin_overrides`、`player_penalties`、
  `player_penalty_adjustments` は監査・運用補正に必要な永続データである
- `alembic_version` はマイグレーション状態を保持する管理用テーブルである

## 共通ルール

- `join`、`present`、`leave` はプレイヤー単位で `pg_advisory_xact_lock(player_id)` を取得する
- 在席確認リマインドと `expire` は対象キュー行を `SELECT ... FOR UPDATE` でロックする
- 複数件を expire するバッチ cleanup は `FOR UPDATE SKIP LOCKED` を使う
- `join` と `present` では `revision` を更新する
- `last_reminded_revision` は、現在の `revision` に対して在席確認リマインドを送ったかどうかを表す
- 古いタイマーが起きても、`revision` が一致しなければ no-op とする
- `join` と `present` の commit 後には、在席確認リマインドタスクと expire タスクの両方を登録する
- `join` 成功後には、別トランザクションで必ずマッチング試行を行う
- `left`、`expired`、`matched` になった行は再利用しない
- 再 join 時は新しい行を作る

## キュー定義と参加条件

### キュー定義

- キュー定義は `docs/match_class.md` に従う
- 初期状態では `low` と `high` の 2 キューを用意する
- アプリケーションコード上では `queue_name -> queue_class_id` を解決できる定数定義を持つ

### 参加条件の評価タイミング

- プレイヤーの参加可能条件は `join` 時にのみチェックする
- `join` 時点のプレイヤーのレーティングで判定する
- `join` 成功後にレーティングが変化しても、待機中は再判定しない
- `present`、`leave`、起動時再同期、マッチ作成直前では参加条件を再チェックしない

### 中間階級追加後の判定

- 中間階級追加後の参加可能条件は `docs/match_class.md` で定義した `target_rating` と半開区間ルールに従う
- 実装では、`queue_class_id` に対応するキュー定義を読み、そのプレイヤーの `join` 時点レートだけで参加可否を判定する

## 状態遷移

- `waiting -> waiting`
  - `present`
- `waiting -> left`
  - `leave`
- `waiting -> expired`
  - `expire`
- `waiting -> matched`
  - マッチ成立

補足:

- `present` や `leave` 実行時点で `expire_at <= now()` の場合、その操作は expire に負ける
- `join` 時に既存 `waiting` 行が期限切れなら、先にその行を `expired` にしてから新しい行を作る

## join 仕様

### 成功条件

- プレイヤーが登録済みである
- 指定した `queue_name` が有効である
- `queue_name` から `queue_class_id` を解決できる
- `join` 時点のプレイヤーのレーティングが、そのキューの参加条件を満たす
- 有効な `waiting` 行を持っていない

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 入力された `queue_name` を `queue_class_id` へ解決する
4. 対象プレイヤーの現在レーティングで、そのキューへの参加可否を判定する
5. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
6. 行があり、かつ `expire_at > now()` なら失敗する
7. 行があり、かつ `expire_at <= now()` なら、その行を `expired` に更新する
8. 新しい `waiting` 行を作成する
9. `queue_class_id = resolved_queue_class_id` を設定する
10. `joined_at = now()`
11. `last_present_at = now()`
12. `expire_at = now() + interval '5 minutes'`
13. `revision = 1`
14. `last_reminded_revision = NULL` にする
15. commit する
16. commit 後に以下 2 種類の単発タスクを登録する
17. `remind_at = expire_at - interval '1 minute'` の在席確認リマインドタスク
18. `expire_at` の expire タスク
19. commit 後に、別トランザクションで参加先 `queue_class_id` を対象にマッチング試行を行う

### 失敗時の応答

- 指定した `queue_name` が存在しない場合は失敗
  - 応答例: `指定したキューは存在しません。`
- その時点のレーティングでは指定キューへ参加できない場合は失敗
  - 応答例: `現在のレーティングではそのキューに参加できません。`
- すでにキュー参加中なら失敗
- 応答例: `すでにキュー参加中です。`

### 成功時の応答

- 応答例: `キューに参加しました。5分間マッチングします。`
- `join` への応答は常に先に返す
- `join` をきっかけにマッチ成立した場合は、後続の別通知で `マッチ成立` を伝える

## present 仕様

### 成功条件

- `status = 'waiting'` の有効なキュー行が存在する

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
4. 行がなければ失敗する
5. 行があっても `expire_at <= now()` なら、その場で `expired` に更新して commit する
6. 行が有効なら `last_present_at = now()` に更新する
7. `expire_at = now() + interval '5 minutes'` に更新する
8. `revision = revision + 1` に更新する
9. `last_reminded_revision = NULL` に更新する
10. commit する
11. commit 後に新しい `revision` を使って以下 2 種類の単発タスクを登録する
12. `remind_at = expire_at - interval '1 minute'` の在席確認リマインドタスク
13. `expire_at` の expire タスク

### 補足

- `present` は現在参加中の `waiting` 行に対して暗黙適用する
- `queue_name` の入力は受け取らない
- `queue_class_id` や参加可能条件は再判定しない
- 古いタスクは cancel できれば cancel する
- cancel できなくても `revision` 不一致で no-op にできるため、正しさは保てる

### 失敗時の応答

- キューに参加していない場合
  - `キューに参加していません。`
- 期限切れだった場合
  - `期限切れのためキューから外れました。`

### 成功時の応答

- 応答例: `在席を更新しました。次の期限は5分後です。`

## leave 仕様

### 成功条件

- `status = 'waiting'` の有効なキュー行が存在する

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
4. 行がなければ、冪等に成功扱いとするか、失敗扱いとする
5. 行があっても `expire_at <= now()` なら、その場で `expired` に更新して commit する
6. 行が有効なら `status = 'left'` に更新する
7. `removed_at = now()` を設定する
8. `removal_reason = 'user_leave'` を設定する
9. commit する
10. commit 後にローカルの在席確認リマインドタスクと expire タスクを cancel する

### 推奨 UX

- `leave` は現在参加中の `waiting` 行に対して暗黙適用する
- `queue_name` の入力は受け取らない
- `waiting` 行がない場合は冪等に成功扱いにする
- 応答例: `キューから退出しました。`

### 期限切れだった場合の応答

- `すでに期限切れでキューから外れています。`

## 在席確認リマインド仕様

### 目的

- `expire_at` の 1 分前時点で、まだ `waiting` のプレイヤーへ在席確認を促す通知を送る
- 1 回の `join` または `present` に対して、リマインドは最大 1 回だけ送る

### 発火条件

- `status = 'waiting'`
- `expire_at - interval '1 minute' <= now() < expire_at`
- `last_reminded_revision IS NULL` または `last_reminded_revision != revision`

### 発火元

- `join` 後に登録された単発在席確認リマインドタスク
- `present` 後に登録された単発在席確認リマインドタスク
- プロセス起動時・再起動時の再同期処理

### 単発タスクの引数

- `queue_entry_id`
- `expected_revision`
- `remind_at`

`remind_at` は `expire_at - interval '1 minute'` とする。

### 処理

1. トランザクションを開始する
2. 対象キュー行を `FOR UPDATE` で取得する
3. 行がなければ no-op
4. `status != 'waiting'` なら no-op
5. `revision != expected_revision` なら no-op
6. `expire_at <= now()` なら no-op
7. `expire_at - interval '1 minute' > now()` なら no-op
8. `last_reminded_revision = revision` なら no-op
9. 条件をすべて満たした場合のみ `last_reminded_revision = revision` に更新する
10. 通知用 outbox を使う場合は同一トランザクション内でイベントを作る
11. commit する
12. commit 後、勝者プロセスのみが在席確認リマインド通知を送る

### 重要な性質

- すべてのプロセスが同じプレイヤーに対する在席確認リマインドタスクを持っていてもよい
- 実際にリマインド通知を確定できるのは、ロックを取って条件を満たした 1 プロセスのみ
- `present` によって `revision` が進むと、次の 5 分サイクルで再び 1 回だけリマインド可能になる
- `matched`、`left`、`expired` になった行にはリマインドしない

### 通知内容

- 応答例: `在席確認です。1分以内に在席更新がない場合はマッチングキューから外れます。`

## expire 仕様

### 発火元

- `join` 後に登録された単発タスク
- `present` 後に登録された単発タスク
- プロセス起動時の cleanup
- 保険用の定期 reconcile ループ

### 単発タスクの引数

- `queue_entry_id`
- `expected_revision`
- `expire_at`

### 処理

1. トランザクションを開始する
2. 対象キュー行を `FOR UPDATE` で取得する
3. 行がなければ no-op
4. `status != 'waiting'` なら no-op
5. `revision != expected_revision` なら no-op
6. `expire_at > now()` なら no-op
7. 条件をすべて満たした場合のみ `status = 'expired'` に更新する
8. `removed_at = now()` を設定する
9. `removal_reason = 'timeout'` を設定する
10. 通知用 outbox を使う場合は同一トランザクション内でイベントを作る
11. commit する
12. commit 後、勝者プロセスのみが expire 通知を送る

### 重要な性質

- すべてのプロセスが同じプレイヤーに対する expire タスクを持っていてもよい
- 実際に `expired` に遷移できるのは、ロックを取って条件を満たした 1 プロセスのみ
- 他プロセスは no-op になる

## 単発 reminder / expire タスクの一時エラー時 retry 仕様

### 目的

- DB の一時的な切断や接続回復中の失敗で、単発 reminder / expire タスクを取りこぼしにくくする
- retry 方針を `AsyncioMatchingQueueTaskScheduler` と service 層の間で疎結合に保つ
- retry 中に DB transaction や row lock を保持しない

### 適用対象

- `join` / `present` 後に登録される単発在席確認リマインドタスク
- `join` / `present` 後に登録される単発 expire タスク

起動時再同期や reconcile から同期的に呼ばれる処理は、この節の in-memory retry の対象外とする。

### handler と scheduler の責務分担

- scheduler は handler の具体的な失敗理由を解釈しない
- scheduler は SQLAlchemy や psycopg の具体例外型に依存しない
- service 層の task handler は、成功または no-op の場合は通常どおり戻り値を返す
- service 層の task handler は、「同じ引数で後から再試行してよい一時失敗」だけを包括的な retryable 例外へ変換して raise する
- retryable 例外の名前は実装で決めてよいが、仮称 `RetryableTaskError` とする
- retryable 例外へ変換する際は、元例外を `raise ... from exc` で保持する
- scheduler は retryable 例外だけを捕捉し、指数バックオフで同じ task を再登録する
- retryable 例外以外は恒久失敗として扱い、error log を出して task を終了する

### retry 対象の例

- DB 接続断
- 接続回復中の一時失敗
- connection pool から取得した接続の失効
- transaction の開始、commit、rollback 時に発生する一時的な接続系失敗

### retry 対象外の例

- `asyncio.CancelledError`
- SQL 文の誤りやスキーマ不整合
- 一意制約違反などのデータ整合性違反
- 実装バグや設定ミスに起因する恒久失敗

### backoff 方式

- retry の待機時間は指数バックオフとする
- 初回待機は `1s` とする
- 以後、失敗のたびに待機時間を 2 倍にする
- 上限は `512s` とする
- 想定する待機列は `1s, 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s, 512s` とする
- `512s` 到達後は、成功または task の無効化まで `512s` 間隔で再試行する
- 初期段階では jitter は入れない
- backoff 係数は outbox の retry 方針とそろえる

### retry 状態の保持

- retry 状態は in-memory で保持する
- reminder / expire のために DB へ専用の retry 状態は保存しない
- 1 つの `queue_entry_id` と task 種別に対して、同時に有効な待機 task は常に 1 つだけとする
- `join` / `present` により同じ `queue_entry_id` の新しい task が登録された場合、古い待機 task と retry 待機は cancel して置き換える
- `leave`、`matched`、`expired`、runtime stop 時は pending retry も含めて cancel する

### task 種別ごとの打ち切り条件

- `presence_reminder` は、元の `remind_at + interval '1 minute'` 以上へ次回 retry がずれ込む場合は新たな retry を登録しない
- これは `presence_reminder` が有効なのは元の `expire_at` までであり、`remind_at + interval '1 minute'` がその上限に等しいためである
- `expire` は期限超過後も意味を失わないため、ローカルの retry 締切は設けない
- `expire` の retry は、成功するか、他経路で `waiting` 以外に変化して次回実行が no-op になるまで継続してよい

### 冪等性と安全性

- commit 成否が不明な接続断が起きても、retry は同じ引数で安全に再実行できることを前提とする
- `presence_reminder` は `last_reminded_revision` 判定により、同じ revision への二重通知を防ぐ
- `presence_reminder` の outbox event は `dedupe_key` により重複生成を防ぐ
- `expire` は `status`、`revision`、`expire_at`、`FOR UPDATE` により二重 expire を防ぐ
- 複数プロセスが同じ行に対して retry しても、実際に状態を確定できるのは条件を満たした 1 プロセスのみである

## マッチング試行仕様

### 目的

- `join` 成功後に、参加先キューの待機人数でマッチ成立可能かを即時に試す
- 各 `queue_class_id` ごとに独立してマッチを作成する
- すでに 6 人以上の `waiting` 行がある場合、可能な限り連続でマッチを作成する

### 発火元

- `join` 成功後
- プロセス起動時・再起動時の再同期処理

### 実行方針

- `join` と同一トランザクションでは実行しない
- `join` commit 後に別トランザクションで、参加先 `queue_class_id` を対象にマッチング試行を行う
- `try_create_matches()` の失敗によって `join` 自体は失敗させない
- 起動時・再起動時の再同期では、定義済みの全 `queue_class_id` を対象にマッチング試行を行ってよい

### 候補抽出条件

- `status = 'waiting'`
- `expire_at > now()`
- 対象 `queue_class_id` と一致する
- `joined_at` の古い順を優先する

### ロック方針

- 候補行は対象 `queue_class_id` に絞った上で `ORDER BY joined_at, id`
- `SELECT ... FOR UPDATE SKIP LOCKED` で取得する
- 6 人に満たない場合は no-op で終了する
- 同じ `queue_class_id` で 6 人以上いる限りループして複数マッチを作ってよい

### 成立時の処理

1. 候補 6 人をロックして取得する
2. `docs/match_class.md` の全探索ルールに従って、`q` ベース期待勝率が最も 50% に近い 3v3 分割を選ぶ
3. 同値が複数ある場合は、最初に見つかった候補を採用する
4. 採用した 2 チームに対して、どちらを Team A / Team B とするかをランダムに決定する
5. 各チーム内の並び順を、マッチ構築時点レーティングの降順、同率なら待機列順で確定する
6. 対象プレイヤーのマッチを作成する
7. 対応するキュー行を `matched` に更新する
8. commit する
9. commit 後に `マッチ成立` 通知を送る

### 通知方針

- `join` の応答は先に `キューに参加しました。` を返す
- その後、同じ `join` を起点にマッチ成立した場合は、別通知で `マッチ成立` を送る
- `join` 応答を `マッチ成立` 通知で置き換えない

### マッチ成立通知仕様

- マッチ成立通知は、そのマッチに参加したプレイヤーが `join` / `present` していたすべての channel に送る
- 同じ match に複数人が同じ channel から参加していた場合、その channel への通知は 1 回でよい
- マッチ成立通知の先頭には、特定ユーザーへの mention を付けない
- 通知本文には、少なくとも以下の順でチーム構成を含める
  1. `マッチ成立です。`
  2. `Team A`
  3. Team A のメンバー一覧
  4. `Team B`
  5. Team B のメンバー一覧
- Team A / Team B はマッチごとにランダムに決定されたラベルをそのまま使う
- 各チーム内の表示順は、マッチ構築時点レーティングの降順、同率なら待機列順とする
- Team A / Team B のメンバーは、Discord 上の通常ユーザーであれば mention 形式で表記してよい
- ダミーユーザーは mention せず、`<dummy_{dummy_id}>` の形式で表記する

メッセージ例:

```text
マッチ成立です。
Team A
<@123456789012345678>
<@234567890123456789>
<dummy_101>
Team B
<@345678901234567890>
<dummy_102>
<dummy_103>
```

### 補足

- `join` 後に登録された在席確認リマインドタスクや expire タスクは、マッチ成立後に起きても `status = 'matched'` 判定で no-op にできる
- 起動時再同期でも `try_create_matches()` を実行し、すでに 6 人以上待機しているケースを取りこぼさない

## 起動時・再起動時の仕様

各プロセスは起動時に以下を行う。

1. `status = 'waiting' and expire_at <= now()` の行を cleanup する
2. cleanup は `FOR UPDATE SKIP LOCKED` で少しずつ取得して `expired` に更新する
3. 必要なら outbox に expire 通知イベントを積む
4. 次に別トランザクションで、定義済みの全 `queue_class_id` を対象にマッチング試行を行う
5. その後、`status = 'waiting' and expire_at > now()` の行を取得する
6. それぞれについて、在席確認リマインドの送信有無を確認する
7. `expire_at - interval '1 minute' <= now() < expire_at` かつ (`last_reminded_revision IS NULL` または `last_reminded_revision != revision`) の行は、その場で在席確認リマインド処理を試みる
8. `now() < expire_at - interval '1 minute'` の行だけ、`remind_at = expire_at - interval '1 minute'` の単発在席確認リマインドタスクを登録する
9. `last_reminded_revision = revision` の行には、在席確認リマインドタスクを再登録しない
10. すべての行について、ローカルの単発 expire タスクを登録する

補足:

- プロセスが落ちた場合でも、基本的にはすぐに新しいプロセスが起動し、総プロセス数は減らない想定とする
- そのため、在席確認リマインドタスクと expire タスクは通常、起動時再同期と通常イベント処理で復旧される前提とする
- 単発 task の retry 待機状態は in-memory のみで保持し、プロセス再起動時には失われる
- `join` や `present` の commit 後にプロセスが落ちると、まれにタスク未登録のままになる可能性がある
- `join` の commit 後にマッチング試行前にプロセスが落ちても、起動時再同期の `try_create_matches()` で回収する
- 起動時再同期で最終 1 分に入っている行を見つけた場合は、即時に在席確認リマインド処理を試みる
- retry 待機中にプロセスが落ちた場合でも、起動時再同期と reconcile で待機行から task を再構築できることを前提とする
- reconcile ループはその取りこぼしを回収するための保険としてのみ入れる
- reconcile ループは 5 分おきに実行する
- reconcile ループで cleanup が発生した場合は、想定外の取りこぼしが起きていたことを示すため warning log を出す

## 通知仕様

- 在席確認リマインド通知と expire 通知は非同期に送る
- `join` を起点にマッチ成立した場合も、`join` 応答とは別に非同期通知で伝える
- マッチ成立通知は、そのマッチの参加者が `join` / `present` していたすべての channel に送る
- マッチ成立通知では特定ユーザーへの mention を付けず、Team A / Team B の構成が分かる本文を送る
- 二重送信防止と送信漏れ対策のため、outbox パターンの採用を推奨する
- outbox を使う場合は、少なくとも `presence_reminder`、`queue_expired`、`match_created` を区別できるようにする
- `join` 時の内部 cleanup は通常、通知対象にしない
- 起動時再同期でリマインド送信対象を見つけた場合も、通常のリマインド通知として扱う
- `present` や `leave` が遅すぎてその場で `expired` になった場合は、同期応答で伝えるため、非同期通知は不要

## ログ仕様

- 起動時 cleanup の件数は info log に出してよい
- 単発タスクによる通常の在席確認リマインドは info log を基本とする
- 起動時再同期で即時に在席確認リマインドを送った場合も info log を基本とする
- 単発タスクによる通常の expire は info log を基本とする
- 単発 reminder / expire task が retryable 例外で失敗した場合は warning log を出す
- warning log には少なくとも以下を含める
  - task 種別
  - `queue_entry_id`
  - `expected_revision`
  - `failure_count`
  - `next_retry_at`
  - 例外型
- retry 後に task が成功した場合は、何回目で回復したか分かる info log を出してよい
- `presence_reminder` が締切超過で retry を打ち切った場合は info log を出してよい
- reconcile ループによって cleanup が実行された場合は warning log を出す
- warning log には少なくとも以下を含める
  - 実行プロセスが保険 cleanup を行ったこと
  - cleanup 件数
  - 対象 `queue_entry_id` または `player_id`

## 実装メモ

- in-memory タイマーは精度向上のための仕組みであり、整合性担保の主役ではない
- 整合性は `status`、`expire_at`、`revision`、DB ロックで守る
- マッチ成立処理を入れる場合も、`waiting -> matched` はトランザクション内で更新し、expire と競合しないよう `FOR UPDATE` を前提にする
- マッチ作成候補は必ず同じ `queue_class_id` 内からだけ選び、異なるキューをまたいで混在させない

## テスト項目

### join

- [x] 未登録プレイヤーの `join` が失敗すること
- [x] 初回 `join` で `waiting` 行が作成され、`queue_class_id`、`joined_at`、`last_present_at`、`expire_at`、`revision = 1`、`last_reminded_revision = NULL` が設定されること
- [ ] 無効な `queue_name` を指定した `join` が失敗すること
- [ ] 参加条件を満たさないキューへの `join` が失敗すること
- [x] 有効な `waiting` 行がある状態での重複 `join` が失敗すること
- [x] 期限切れの `waiting` 行が残っている状態で `join` すると、古い行が `expired` になり、新しい `waiting` 行が作られること
- [x] `join` 後に在席確認リマインドタスクと expire タスクが登録されること
- [ ] `join` 後に別トランザクションで `try_create_matches()` が呼ばれること
- [ ] `try_create_matches()` が失敗しても `join` 自体は成功のまま残ること
- [ ] `join` の応答が先に返り、その後に別通知で `マッチ成立` を送れること

### present

- [x] 有効な `waiting` 行に対する `present` で `last_present_at` と `expire_at` が更新され、`revision` が増加し、`last_reminded_revision = NULL` に戻ること
- [ ] `present` が現在参加中のキュー行へ暗黙適用され、`queue_class_id` を変更しないこと
- [x] `present` 後に新しい在席確認リマインドタスクと expire タスクが登録されること
- [x] `waiting` 行が存在しない場合の `present` が失敗すること
- [x] `expire_at <= now()` の行に対する `present` は `expired` に遷移して timeout 応答になること
- [x] 古い `revision` を持つ reminder / expire タスクが起きても no-op になること

### leave

- [x] 有効な `waiting` 行に対する `leave` で `left` に遷移し、`removed_at` と `removal_reason = 'user_leave'` が設定されること
- [ ] `leave` が現在参加中のキュー行へ暗黙適用されること
- [x] `waiting` 行がない場合の `leave` が冪等に成功扱いできること
- [x] `expire_at <= now()` の行に対する `leave` は `left` ではなく `expired` になること
- [x] `leave` 後にローカルの在席確認リマインドタスクと expire タスクが cancel されること

### 在席確認リマインド

- [x] `expire_at - 1分` に達した `waiting` 行に対して在席確認リマインドが 1 回だけ送られること
- [x] 同じ `revision` に対して reminder タスクが複数回起きても、実際の通知は 1 回だけであること
- [x] `matched`、`left`、`expired` の行にはリマインドが送られないこと
- [x] `present` で `revision` が進んだあとは、新しい 5 分サイクルで再度 1 回だけリマインド可能になること
- [x] 起動時再同期で最終 1 分に入っており、かつ未通知の行があれば即時にリマインド処理を試みること
- [x] 起動時再同期で `last_reminded_revision = revision` の行には reminder タスクを再登録しないこと
- [ ] 一時的な DB エラーで単発 reminder task が失敗した場合、retryable 例外に変換されて指数バックオフで再試行されること
- [ ] reminder retry でも同じ revision に対する outbox event が重複生成されないこと
- [ ] reminder retry が元の `expire_at` を超える場合は追加 retry されないこと
- [ ] reminder retry 待機中に `present` で revision が進んだ場合、古い retry 待機が cancel されること

### expire

- [x] `expire_at <= now()` の `waiting` 行が `expired` に遷移し、`removed_at` と `removal_reason = 'timeout'` が設定されること
- [x] `status != 'waiting'`、`revision` 不一致、`expire_at > now()` の場合に expire が no-op になること
- [ ] 同じ行に対して複数プロセスが expire を実行しても、実際に `expired` へ遷移できるのは 1 プロセスだけであること
- [ ] `present` / `leave` と expire が境界タイミングで競合した場合、期限切れ側が一貫して勝つこと
- [x] 通常の expire が info log を出すこと
- [x] reconcile による cleanup が発生した場合に warning log を出すこと
- [ ] 一時的な DB エラーで単発 expire task が失敗した場合、retryable 例外に変換されて指数バックオフで再試行されること
- [ ] expire retry により回復した場合でも、`expired` 遷移と outbox event 生成は 1 回だけであること

### マッチング試行

- [x] 待機人数が 6 人未満のとき、`try_create_matches()` が no-op で終了すること
- [ ] 異なる `queue_class_id` にいるプレイヤー同士が同じマッチへ混在しないこと
- [x] 6 人ちょうどの待機で 1 マッチが作成され、対象のキュー行が `matched` になること
- [x] 12 人以上の待機で 1 回の `try_create_matches()` が複数マッチを連続生成できること
- [x] 候補抽出が `joined_at, id` の古い順で行われること
- [x] `expire_at <= now()` の行が候補から除外されること
- [ ] チーム分けが `q` ベース期待勝率の最適化で決まること
- [ ] 同値の候補が複数ある場合に、最初に見つかった候補が採用されること
- [ ] Team A / Team B の割り当てがランダムに行われ、その結果が試合中固定されること
- [ ] 各チーム内の並び順がレーティング降順、同率なら待機列順になること
- [ ] 複数プロセスが同時に `try_create_matches()` を走らせても、同じプレイヤーが二重にマッチへ入らないこと
- [ ] `join` を起点にマッチ成立した場合でも、`join` 応答は先に返り、`マッチ成立` は別通知になること
- [x] `matched` になった行に対して後から reminder / expire タスクが起きても no-op になること

### 起動時・再起動時

- [x] 起動時に期限切れ行の cleanup が行われること
- [x] 起動時に `try_create_matches()` が実行され、すでに 6 人以上待機しているケースを回収できること
- [x] 起動時に reminder 対象の行へ即時リマインドできること
- [x] 起動時に将来期限の `waiting` 行へ reminder タスクと expire タスクが再登録されること
- [ ] 単発 task の retry 待機中にプロセスが落ちても、起動時再同期で reminder / expire task を再構築できること
- [ ] `join` commit 後にプロセスが落ちてタスク未登録になっても、起動時再同期で復旧できること
- [ ] `join` commit 後にプロセスが落ちてマッチング試行が走らなくても、起動時再同期の `try_create_matches()` で回収できること

### outbox / 通知

- [x] `presence_reminder`、`queue_expired`、`match_created` のイベント種別が正しく生成されること
- [x] 同一事象に対して outbox イベントが重複生成されないこと
- [x] `join` 時の内部 cleanup では通知イベントを作らないこと
- [x] `present` / `leave` が遅すぎて同期的に `expired` になった場合、非同期通知イベントを作らないこと
