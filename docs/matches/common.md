# 試合運営共通仕様

## 目的

`1v1`、`2v2`、`3v3` に共通する試合進行、親決定、勝敗報告、承認、結果確定、ペナルティ反映を定める。

## スコープ

Bot が管理する範囲:

- マッチ成立後の親募集と親決定
- 親決定時刻を起点としたタイマー管理
- 試合参加者による勝敗報告の受付
- 勝敗報告の仮判定と承認期間の管理
- 勝敗結果と各プレイヤーの報告状態、承認状態の確定
- 勝敗誤報告ペナルティと勝敗無報告ペナルティの承認期間終了後の自動付与
- admin による結果上書きとペナルティ加算、減算
- 条件に応じた admin 通知

Bot が管理しない範囲:

- 親決定後の部屋 ID 報告
- 部屋立て遅延の自動判定
- 試合ミス、遅刻、切断の自動判定
- 画像証拠の収集と真偽判定
- 運用判断による即時の無効試合化

## フォーマット共通パラメータ

各試合は `match_format` を持ち、以下の値を導く。

```text
team_size(match_format)
participant_count(match_format) = team_size * 2
```

例:

- `1v1`: `team_size = 1`, `participant_count = 2`
- `2v2`: `team_size = 2`, `participant_count = 4`
- `3v3`: `team_size = 3`, `participant_count = 6`

## 3v3 仕様からの読み替えと補正

`3v3` 向け仕様をそのまま `1v1`、`2v2` に広げると、固定人数に依存した文言が矛盾するため、以下のように一般化する。

- `6 人` という記述は `participant_count(match_format)` に読み替える
- `6 人全員が報告` は `全参加者が報告` に読み替える
- `6 人の中からランダムに親を決定` は `参加者全員の中からランダムに親を決定` に読み替える
- `報告したプレイヤーが 2 人以下なら admin 通知` は `報告したプレイヤー数 < team_size(match_format)` に置き換える

最後の置き換えは必須である。  
`1v1` で旧条件の `2 人以下` をそのまま使うと、両者が報告していても常に admin 通知対象になってしまうためである。

## シーズン所属

- `started_at` は `matches.created_at` とみなす
- 試合の所属シーズンは、`started_at` を含むシーズンで決める
- `matches` には `started_season_id` を保存する
- シーズン切替後に結果確定や admin 修正が行われても、レート更新先は `started_season_id` の `player_format_stats` とする

## 試合フロー

### 1. マッチ成立

- マッチングキューで十分人数がそろった時点でマッチを作成する
- その時点の `created_at` から `started_season_id` を決定する
- Bot は Team A / Team B の組み分けを通知する

### 2. 親募集

- マッチ成立直後から、参加者は親に立候補できる
- 親募集期間は 5 分とする
- 複数人が立候補した場合は先着順で親を決定する
- 5 分以内に立候補者が現れなかった場合は、Bot が参加者の中からランダムに親を決定する

### 3. 親決定

- 親が決まった時刻を `parent_decided_at` とする
- 以後の試合タイマーはすべて `parent_decided_at` を基準に計測する
- 親決定後の部屋立てと部屋 ID 共有は運用で行い、Bot では管理しない

### 4. 試合進行

- 部屋立て、試合開始、ゲーム間インターバル、試合終了は運用で進行する
- Bot は試合中の進行そのものは管理しない
- ただし、勝敗報告の受付開始時刻、締切時刻、承認期間はタイマーで管理する

### 5. 勝敗報告

- 勝敗報告は、その試合参加者のみが行える
- UX 上の投票先は以下の 4 種類とする
  - 勝ち
  - 負け
  - 引き分け
  - 無効試合
- プレイヤーは自分視点で勝ち負けを報告する
- Bot は所属チームに応じて内部結果へ正規化する
  - Team A の `勝ち` は `Team A の勝ち`
  - Team A の `負け` は `Team B の勝ち`
  - Team B の `勝ち` は `Team B の勝ち`
  - Team B の `負け` は `Team A の勝ち`
  - `引き分け` と `無効試合` は両チーム共通
- `無効試合` だけは `match_created_at` の時点から受け付ける
- `勝ち`、`負け`、`引き分け` は `report_open_at` 以後に受け付ける
- 1 プレイヤーは 1 件の最新報告だけを持つ
- 通常の報告受付期間中は、自分の報告を何度でも上書きできる

### 6. 承認期間

- 親が決定済みであり、かつ全参加者の最新報告を正規化した結果が全会一致している場合は、承認期間に入らず即時に結果確定する
- それ以外では、次のいずれかを満たした時点で勝敗結果を仮判定し、5 分間の承認期間に入る
  - 親が決定済みであり、かつ全参加者が勝敗報告を行った
  - 親が決定済みであり、かつ勝敗報告の締切時刻に到達した
- 承認期間中は、勝敗報告の変更や新規報告は受け付けない
- 承認期間中に操作できるのは、最新の仮判定に対して `incorrect` または `not_reported` になっているプレイヤーによる承認のみとする
- 承認期間中に承認したプレイヤーは、自動ペナルティ付与対象から外れる
- 承認対象者が全員承認した時点で、承認期間の終了時刻を待たずに即時に結果確定する
- Bot は、承認できない場合は証拠を提示したうえで admin へ連絡するよう通達する

### 7. 結果確定

- 次のいずれかを満たした時点で、試合は確定状態へ移行する
  - 親が決定済みであり、かつ全参加者の最新報告を正規化した結果が全会一致した
  - 承認期間が終了した
  - 承認対象者が全員承認した
- 確定後は参加者による勝敗報告の変更や承認を受け付けない
- 承認期間終了後、`incorrect` または `not_reported` のプレイヤーのうち、承認期間中に承認していないプレイヤーへ自動ペナルティを反映する
- 条件に応じて admin へ確認通知を送る

## タイマー仕様

### 親募集締切

- `match_created_at + 5 分`

### 勝敗報告受付開始

- `parent_decided_at + 7 分`
- `勝ち`、`負け`、`引き分け` はこの時刻より前は受け付けない
- `無効試合` は `match_created_at` の時点から受け付ける

### 勝敗報告締切

- `parent_decided_at + 27 分`

### 承認期間

- 開始条件
  - 親が決定済みであり、かつ全参加者の勝敗報告がそろった時点
  - または親が決定済みであり、かつ `parent_decided_at + 27 分`
- ただし、全参加者の最新報告を正規化した結果が全会一致している場合は、承認期間に入らず即時に結果確定する
- 終了時刻
  - 原則 `approval_started_at + 5 分`
  - ただし、承認対象者が全員承認した場合はその時点で終了し、即時に結果確定する

## 勝敗判定ルール

### 通常の判定

- 各プレイヤーの最新報告を内部結果へ正規化して集計する
- 最も票数の多い選択肢を、その時点の基本結果とする

### 同票時の判定

最大票数の選択肢が複数ある場合は、次の順で仮判定または確定結果を決める。

1. 誰も報告していない場合は、無効試合を採用する
2. 親が最新報告を行っており、その正規化後の報告先が同票候補の中に含まれていれば、親の報告を採用する
3. それでも決まらない場合は、無効試合とし、admin への連絡を行う

## 各プレイヤーの報告状態

結果確定時、各プレイヤーの報告状態を以下のいずれかで記録する。

- `correct`
  - 最新報告を正規化した結果が、確定結果と一致している
- `incorrect`
  - 最新報告を正規化した結果が、確定結果と一致しない
- `not_reported`
  - 承認期間開始時点までに報告を提出していない

## 各プレイヤーの承認状態

- `not_required`
  - 承認不要
- `pending`
  - 承認待ち
- `approved`
  - 承認済み
- `not_approved`
  - 承認期限切れ

## admin 通知条件

以下のいずれかに該当する場合、Bot は admin に確認通知を送る。

- 承認期間開始時点で勝敗報告を行ったプレイヤー数が `team_size(match_format)` 未満である
- 勝敗報告を行ったプレイヤーが片方のチームにしか存在しない
- 同票が解消できず、仮決定結果を無効試合として扱う

通知タイミング:

- 承認期間開始時点の状態を基準に判定する
- 通知は承認期間終了後の結果確定時に行う
- admin 通知は結果確定アナウンスの後に送る

## ペナルティ仕様

### 自動適用するペナルティ

- 勝敗誤報告
  - 結果確定時に `incorrect` かつ `approved` ではないプレイヤーへ `+1`
- 勝敗無報告
  - 結果確定時に `not_reported` かつ `approved` ではないプレイヤーへ `+1`

### 自動適用しない違反

- 部屋立て遅延
- 試合ミス
- 遅刻
- 切断

これらは運用で判断し、admin が手動で加算、減算する。

手動ペナルティの対象指定:

- 実ユーザーは Discord の `user` 指定で選ぶ
- ダミーユーザーは `<dummy_{dummy_user_id}>` 形式の `dummy_user` を指定する

## 運用上の違反種別

- 部屋立て遅延
- 試合ミス
- 遅刻
- 切断
- 勝敗誤報告
- 勝敗無報告

## コマンド方針

- 試合に紐づく操作コマンドは、少なくとも `match_id` を引数に取る
- `match_id` から `match_format` を解決できるため、試合操作コマンドに `match_format` の追加引数は不要とする
- 詳細なコマンド入出力は [../commands/user-commands.md](../commands/user-commands.md) と [../commands/dev-commands.md](../commands/dev-commands.md) に従う

## 必要な管理データ

### `matches`

- `match_id`
- `match_format`
- `queue_class_id`
- `started_season_id`
- `created_at`

### `active_match_states`

- `match_id`
- `parent_player_id`
- `parent_decided_at`
- `report_open_at`
- `report_deadline_at`
- `approval_started_at`
- `approval_deadline_at`
- `provisional_result`
- `admin_review_required`
- `state`

### `active_match_player_states`

- `match_id`
- `player_id`
- `report_status`
- `approval_status`
- `locked_at`
- `approved_at`

### `finalized_match_results`

- `match_id`
- `final_result`
- `rated_at`
- `finalized_at`

### `finalized_match_player_results`

- `match_id`
- `player_id`
- `team`
- `rating_before`
- `games_played_before`
- `wins_before`
- `losses_before`
- `draws_before`
- `report_status`
- `approval_status`
- `auto_penalty_type`
- `auto_penalty_applied`

## 状態遷移

- `waiting_for_parent`
- `waiting_for_result_reports`
- `awaiting_result_approvals`
- `finalized`
