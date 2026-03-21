# プレイヤー利用制限仕様（案）

## 目的

admin がプレイヤー単位で Bot の利用権限を制限できるようにする。

初期版では、以下の 2 種類だけを扱う。

- マッチキューへの参加権限
- 試合の観戦権限

本仕様は、`1v1`、`2v2`、`3v3` の全フォーマット共通で適用する。

## スコープ

本仕様で扱うのは以下である。

- admin による利用制限の付与
- admin による利用制限の解除
- 固定期間または永久の制限期間
- 制限中ユーザーに対する `/join` と `/match_spectate` の拒否
- 制限付与時に、すでに有効なキュー参加または観戦応募があった場合の扱い
- 制限履歴の保持

本仕様では扱わないもの:

- Discord サーバー自体の role / channel 権限
- 試合参加中プレイヤーの勝敗報告権限
- ペナルティとの自動連動
- 自動通知の文面詳細

## 用語

- `restriction_type`
  - 利用制限の種別
- `queue_join`
  - マッチキューへの参加権限の制限
- `spectate`
  - 試合の観戦権限の制限

## 制限種別

### `queue_join`

- `1v1`、`2v2`、`3v3` の全フォーマットを横断して適用する
- 各フォーマット内の全 `queue_name` に同時適用する
- 制限中は `/join` と `/dev_join` を拒否する
- すでに `waiting` 状態のキュー参加がある場合、その既存参加は維持する
- すでに成立済みの試合参加者であることまでは取り消さない

補足:

- 禁止対象は新しい `/join` アクションのみとする
- 既存の `waiting` 行に対する `present` と `leave` は従来どおり許可する

### `spectate`

- 全試合に対する観戦応募権限へ同時適用する
- 制限中は `/match_spectate` と開発者向けの観戦応募コマンドを拒否する
- すでに `active` な観戦応募がある場合、その既存参加は維持する
- 試合参加者としての権限には影響しない

補足:

- 禁止対象は新しい `/match_spectate` アクションのみとする

## 制限期間

admin が指定できる制限期間は以下に限定する。

| 表示値 | 内部表現例 | 意味 |
| --- | --- | --- |
| 1日 | `1d` | `now() + interval '1 day'` まで有効 |
| 3日 | `3d` | `now() + interval '3 day'` まで有効 |
| 7日 | `7d` | `now() + interval '7 day'` まで有効 |
| 14日 | `14d` | `now() + interval '14 day'` まで有効 |
| 28日 | `28d` | `now() + interval '28 day'` まで有効 |
| 56日 | `56d` | `now() + interval '56 day'` まで有効 |
| 84日 | `84d` | `now() + interval '84 day'` まで有効 |
| 永久 | `permanent` | 失効時刻なし |

共通ルール:

- 制限開始時刻は PostgreSQL の `now()` を基準にする
- 一時制限は `expires_at` を持つ
- 永久制限は `expires_at = NULL` とする
- commit 完了時点から即時有効とする

## 有効判定

ある制限が有効である条件は以下とする。

- `revoked_at IS NULL`
- かつ `expires_at IS NULL` または `expires_at > now()`

補足:

- 期限切れはバックグラウンドで状態更新しなくてもよい
- 参照時に上記条件を満たさなければ、無効として扱う
- 初期版では、同一 `(player_id, restriction_type)` に対する有効な制限は 0 件または 1 件であることをアプリケーションの不変条件とする

## admin 操作

### 制限付与

admin が対象ユーザーへ指定種別の利用制限を付与する。

想定入力:

- `user`
  - Discord 上の実ユーザーを指定する
- `dummy_user`
  - ダミーユーザーを `<dummy_{dummy_user_id}>` 形式で指定する
  - `user` と `dummy_user` はどちらか一方のみ指定する
- `restriction_type`
- `duration`
- `reason`
  - 初期版では任意入力

成功条件:

- 実行者が `admin` 権限を持つ
- `user` または `dummy_user` の指定が妥当である
- 対象ユーザーがプレイヤー登録済みである
- 対象ユーザーに同種別の有効な制限が存在しない

処理:

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーを取得する
4. 同種別の有効な制限があるか確認する
5. 新しい制限行を作成する
6. `created_at = now()` を設定する
7. `expires_at` を期間に応じて設定する
8. `created_by_admin_discord_user_id` を設定する
9. `reason` を保存する
10. commit する

正常時の応答例:

- `指定したユーザーのキュー参加を7日間制限しました。`
- `指定したユーザーの観戦を永久制限しました。`

エラー時の応答例:

- 実行者に `admin` 権限がない場合
  - `このコマンドは管理者のみ実行できます。`
- 対象ユーザーが未登録の場合
  - `指定したユーザーは未登録です。`
- 同種別の有効な制限がすでに存在する場合
  - `指定したユーザーにはすでに同種別の制限が有効です。`
- 内部エラーが発生した場合
  - `利用制限の設定に失敗しました。管理者に確認してください。`

### 制限解除

admin が対象ユーザーの利用制限を解除する。

想定入力:

- `user`
  - Discord 上の実ユーザーを指定する
- `dummy_user`
  - ダミーユーザーを `<dummy_{dummy_user_id}>` 形式で指定する
  - `user` と `dummy_user` はどちらか一方のみ指定する
- `restriction_type`

共通ルール:

- 期間指定は受け付けない
- 解除後は、その種別について全期間の利用権限を許可する
- 解除対象に有効な制限がなかった場合も、冪等に成功扱いとしてよい

処理:

1. トランザクションを開始する
2. `pg_advisory_xact_lock(player_id)` を取得する
3. 対象プレイヤーを取得する
4. 対象の有効な制限行を取得する
5. 行があれば `revoked_at = now()` を設定する
6. 行があれば `revoked_by_admin_discord_user_id` を設定する
7. commit する

正常時の応答例:

- `指定したユーザーのキュー参加制限を解除しました。`
- `指定したユーザーの観戦制限を解除しました。`

## コマンド案

コマンド名は暫定で以下を想定する。

- `/admin_restrict_user`
  - `restriction_type`
  - `duration`
  - `user` または `dummy_user`
  - `reason` 任意
- `/admin_unrestrict_user`
  - `restriction_type`
  - `user` または `dummy_user`

`restriction_type` の選択肢:

- `queue_join`
- `spectate`

`duration` の選択肢:

- `1d`
- `3d`
- `7d`
- `14d`
- `28d`
- `56d`
- `84d`
- `permanent`

## 一般ユーザー操作への影響

### `/join`

成功条件に以下を追加する。

- 実行者に `queue_join` の有効な制限が存在しない

制限中の応答例:

- `現在キュー参加を制限されています。`

### `/match_spectate`

成功条件に以下を追加する。

- 実行者に `spectate` の有効な制限が存在しない

制限中の応答例:

- `現在観戦を制限されています。`

### 開発者向けコマンド

- `/dev_join` にも `queue_join` 制限を適用する
- 開発者向けの観戦応募コマンドにも `spectate` 制限を適用する
- `/dev_present`、`/dev_leave` は制限確認の主対象にしない
  - 禁止対象が `/join` のみであるため

## データモデル案

利用制限は `player_access_restrictions` のようなテーブルで管理する。

最低限必要なカラム:

- `id`
- `player_id`
- `restriction_type`
- `created_at`
- `expires_at`
- `revoked_at`
- `created_by_admin_discord_user_id`
- `revoked_by_admin_discord_user_id`
- `reason`

制約・index 案:

- `player_id` は `players.id` を参照する
- `restriction_type` は enum または制約付き文字列で管理する
- `player_id, restriction_type, created_at` に index を張る
- 同一 `(player_id, restriction_type)` の有効制限は 1 件までにする
  - 初期版では transaction 内の確認と `pg_advisory_xact_lock(player_id)` で担保する

保持方針:

- 解除時に物理削除しない
- 期限切れ後も履歴として保持する

## 他仕様への影響

- [../matching/common.md](../matching/common.md)
  - `join` の成功条件と失敗時応答に利用制限チェックを追加する
- [../matches/spectating.md](../matches/spectating.md)
  - 観戦応募の成功条件と失敗時応答に利用制限チェックを追加する
- [../commands/user-commands.md](../commands/user-commands.md)
  - 一般ユーザー向けエラーメッセージを追加する
- [../commands/dev-commands.md](../commands/dev-commands.md)
  - admin コマンドを追加する
  - `dev_join` と開発者向け観戦応募コマンドへ利用制限チェックを追加する

## 要確認事項

- 制限付与時の `reason` を必須入力にするか
- 同種別の有効制限がある場合に、初期版はエラーにするか、上書きにするか
