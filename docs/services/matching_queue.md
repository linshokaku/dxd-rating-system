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
- 有効な `waiting` 行を持っていない

### 処理

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーの `status = 'waiting'` 行を `FOR UPDATE` で取得する
4. 行があり、かつ `expire_at > now()` なら失敗する
5. 行があり、かつ `expire_at <= now()` なら、その行を `expired` に更新する
6. 新しい `waiting` 行を作成する
7. `joined_at = now()`
8. `last_present_at = now()`
9. `expire_at = now() + interval '5 minutes'`
10. `revision = 1`
11. `last_reminded_revision = NULL` にする
12. commit する
13. commit 後に以下 2 種類の単発タスクを登録する
14. `remind_at = expire_at - interval '1 minute'` の在席確認リマインドタスク
15. `expire_at` の expire タスク
16. commit 後に、別トランザクションで `try_create_matches()` を実行する

### 失敗時の応答

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

## マッチング試行仕様

### 目的

- `join` 成功後に、現在の待機人数でマッチ成立可能かを即時に試す
- すでに 6 人以上の `waiting` 行がある場合、可能な限り連続でマッチを作成する

### 発火元

- `join` 成功後
- プロセス起動時・再起動時の再同期処理

### 実行方針

- `join` と同一トランザクションでは実行しない
- `join` commit 後に別トランザクションで `try_create_matches()` を実行する
- `try_create_matches()` の失敗によって `join` 自体は失敗させない

### 候補抽出条件

- `status = 'waiting'`
- `expire_at > now()`
- `joined_at` の古い順を優先する

### ロック方針

- 候補行は `ORDER BY joined_at, id`
- `SELECT ... FOR UPDATE SKIP LOCKED` で取得する
- 6 人に満たない場合は no-op で終了する
- 6 人以上いる限りループして複数マッチを作ってよい

### 成立時の処理

1. 候補 6 人をロックして取得する
2. 対象プレイヤーのマッチを作成する
3. 対応するキュー行を `matched` に更新する
4. commit する
5. commit 後に `マッチ成立` 通知を送る

### 通知方針

- `join` の応答は先に `キューに参加しました。` を返す
- その後、同じ `join` を起点にマッチ成立した場合は、別通知で `マッチ成立` を送る
- `join` 応答を `マッチ成立` 通知で置き換えない

### 補足

- `join` 後に登録された在席確認リマインドタスクや expire タスクは、マッチ成立後に起きても `status = 'matched'` 判定で no-op にできる
- 起動時再同期でも `try_create_matches()` を実行し、すでに 6 人以上待機しているケースを取りこぼさない

## 起動時・再起動時の仕様

各プロセスは起動時に以下を行う。

1. `status = 'waiting' and expire_at <= now()` の行を cleanup する
2. cleanup は `FOR UPDATE SKIP LOCKED` で少しずつ取得して `expired` に更新する
3. 必要なら outbox に expire 通知イベントを積む
4. 次に別トランザクションで `try_create_matches()` を実行する
5. その後、`status = 'waiting' and expire_at > now()` の行を取得する
6. それぞれについて、在席確認リマインドの送信有無を確認する
7. `expire_at - interval '1 minute' <= now() < expire_at` かつ (`last_reminded_revision IS NULL` または `last_reminded_revision != revision`) の行は、その場で在席確認リマインド処理を試みる
8. `now() < expire_at - interval '1 minute'` の行だけ、`remind_at = expire_at - interval '1 minute'` の単発在席確認リマインドタスクを登録する
9. `last_reminded_revision = revision` の行には、在席確認リマインドタスクを再登録しない
10. すべての行について、ローカルの単発 expire タスクを登録する

補足:

- プロセスが落ちた場合でも、基本的にはすぐに新しいプロセスが起動し、総プロセス数は減らない想定とする
- そのため、在席確認リマインドタスクと expire タスクは通常、起動時再同期と通常イベント処理で復旧される前提とする
- `join` や `present` の commit 後にプロセスが落ちると、まれにタスク未登録のままになる可能性がある
- `join` の commit 後にマッチング試行前にプロセスが落ちても、起動時再同期の `try_create_matches()` で回収する
- 起動時再同期で最終 1 分に入っている行を見つけた場合は、即時に在席確認リマインド処理を試みる
- reconcile ループはその取りこぼしを回収するための保険としてのみ入れる
- reconcile ループは 5 分おきに実行する
- reconcile ループで cleanup が発生した場合は、想定外の取りこぼしが起きていたことを示すため warning log を出す

## 通知仕様

- 在席確認リマインド通知と expire 通知は非同期に送る
- `join` を起点にマッチ成立した場合も、`join` 応答とは別に非同期通知で伝える
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
- reconcile ループによって cleanup が実行された場合は warning log を出す
- warning log には少なくとも以下を含める
  - 実行プロセスが保険 cleanup を行ったこと
  - cleanup 件数
  - 対象 `queue_entry_id` または `player_id`

## 実装メモ

- in-memory タイマーは精度向上のための仕組みであり、整合性担保の主役ではない
- 整合性は `status`、`expire_at`、`revision`、DB ロックで守る
- マッチ成立処理を入れる場合も、`waiting -> matched` はトランザクション内で更新し、expire と競合しないよう `FOR UPDATE` を前提にする

## テスト項目

### join

- [x] 未登録プレイヤーの `join` が失敗すること
- [x] 初回 `join` で `waiting` 行が作成され、`joined_at`、`last_present_at`、`expire_at`、`revision = 1`、`last_reminded_revision = NULL` が設定されること
- [x] 有効な `waiting` 行がある状態での重複 `join` が失敗すること
- [x] 期限切れの `waiting` 行が残っている状態で `join` すると、古い行が `expired` になり、新しい `waiting` 行が作られること
- [x] `join` 後に在席確認リマインドタスクと expire タスクが登録されること
- [ ] `join` 後に別トランザクションで `try_create_matches()` が呼ばれること
- [ ] `try_create_matches()` が失敗しても `join` 自体は成功のまま残ること
- [ ] `join` の応答が先に返り、その後に別通知で `マッチ成立` を送れること

### present

- [x] 有効な `waiting` 行に対する `present` で `last_present_at` と `expire_at` が更新され、`revision` が増加し、`last_reminded_revision = NULL` に戻ること
- [x] `present` 後に新しい在席確認リマインドタスクと expire タスクが登録されること
- [x] `waiting` 行が存在しない場合の `present` が失敗すること
- [x] `expire_at <= now()` の行に対する `present` は `expired` に遷移して timeout 応答になること
- [x] 古い `revision` を持つ reminder / expire タスクが起きても no-op になること

### leave

- [x] 有効な `waiting` 行に対する `leave` で `left` に遷移し、`removed_at` と `removal_reason = 'user_leave'` が設定されること
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

### expire

- [x] `expire_at <= now()` の `waiting` 行が `expired` に遷移し、`removed_at` と `removal_reason = 'timeout'` が設定されること
- [x] `status != 'waiting'`、`revision` 不一致、`expire_at > now()` の場合に expire が no-op になること
- [ ] 同じ行に対して複数プロセスが expire を実行しても、実際に `expired` へ遷移できるのは 1 プロセスだけであること
- [ ] `present` / `leave` と expire が境界タイミングで競合した場合、期限切れ側が一貫して勝つこと
- [x] 通常の expire が info log を出すこと
- [x] reconcile による cleanup が発生した場合に warning log を出すこと

### マッチング試行

- [x] 待機人数が 6 人未満のとき、`try_create_matches()` が no-op で終了すること
- [x] 6 人ちょうどの待機で 1 マッチが作成され、対象のキュー行が `matched` になること
- [x] 12 人以上の待機で 1 回の `try_create_matches()` が複数マッチを連続生成できること
- [x] 候補抽出が `joined_at, id` の古い順で行われること
- [x] `expire_at <= now()` の行が候補から除外されること
- [ ] 複数プロセスが同時に `try_create_matches()` を走らせても、同じプレイヤーが二重にマッチへ入らないこと
- [ ] `join` を起点にマッチ成立した場合でも、`join` 応答は先に返り、`マッチ成立` は別通知になること
- [x] `matched` になった行に対して後から reminder / expire タスクが起きても no-op になること

### 起動時・再起動時

- [x] 起動時に期限切れ行の cleanup が行われること
- [x] 起動時に `try_create_matches()` が実行され、すでに 6 人以上待機しているケースを回収できること
- [x] 起動時に reminder 対象の行へ即時リマインドできること
- [x] 起動時に将来期限の `waiting` 行へ reminder タスクと expire タスクが再登録されること
- [ ] `join` commit 後にプロセスが落ちてタスク未登録になっても、起動時再同期で復旧できること
- [ ] `join` commit 後にプロセスが落ちてマッチング試行が走らなくても、起動時再同期の `try_create_matches()` で回収できること

### outbox / 通知

- [x] `presence_reminder`、`queue_expired`、`match_created` のイベント種別が正しく生成されること
- [x] 同一事象に対して outbox イベントが重複生成されないこと
- [x] `join` 時の内部 cleanup では通知イベントを作らないこと
- [x] `present` / `leave` が遅すぎて同期的に `expired` になった場合、非同期通知イベントを作らないこと
