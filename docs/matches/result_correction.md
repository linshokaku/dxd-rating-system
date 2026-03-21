# 試合結果修正方式仕様

## 目的

結果確定済みの試合について、試合結果が誤っていた場合に、レーティングおよび関連状態を一貫した方法で修正するための仕様を定める。

本仕様は `1v1`、`2v2`、`3v3` のすべてに適用する。

## 基本方針

- 修正対象は結果確定済みの試合のみとする
- 修正対象試合以後の全試合を再計算する
- 再計算対象は、修正対象と同じ `match_format` の試合に限定する

補足:

- レーティングと戦績はフォーマットごとに独立している
- そのため `1v1` の結果修正は `1v1` の保持値だけへ影響し、`2v2` と `3v3` には影響しない

## 対象となる修正

本仕様で扱う試合結果の修正は、対象試合の結果を次のいずれかへ書き換える場合のみとする。

- 勝ち
- 負け
- 引き分け
- 無効試合

本仕様では、以下は扱わない。

- 試合日時の変更
- 試合順序の変更
- 参加者の変更
- 試合の追加
- 試合の削除

## 用語

### `rated_at`

レーティング順序を決める不変の時刻とする。  
admin による結果修正後も変更しない。

### `finalized_at`

最新の最終結果更新時刻であり、admin 修正で更新されうる。

### 試合開始時点状態

各試合の `rated_at` 時点における、その試合の更新直前の `player_format_stats` を指す。

具体的には以下の値である。

- `rating_before`
- `games_played_before`
- `wins_before`
- `losses_before`
- `draws_before`

## 永続化先

### `matches`

- `match_id`
- `match_format`
- `queue_class_id`

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

## 現在状態

再計算の対象となる現在状態は、対象 `match_format` の `player_format_stats` とする。

## 試合順序

各 `match_format` の内部で、以下の順序を固定する。

1. `rated_at` 昇順
2. `match_id` 昇順

逆順巻き戻しでは、この逆順で処理する。

## 無効試合の扱い

無効試合は、レーティング計算上は試合が行われなかったものとして扱う。

そのため無効試合では以下を行わない。

- レート更新
- `games_played` の加算
- `wins` の加算
- `losses` の加算
- `draws` の加算

## 修正時の処理手順

1. 修正対象試合を特定し、結果を書き換える
2. 対象 `match_format` の現在の `player_format_stats` をワーキング状態として取得する
3. 最新試合から修正対象試合の直後までを逆順にたどる
4. 各試合について、参加プレイヤーのワーキング状態を `*_before` で上書きする
5. 修正対象試合から最新試合までを時系列順に再計算する
6. 再計算後の最終状態を対象 `match_format` の `player_format_stats` へ保存する

## 再計算時に参照する仕様

- `1v1` は [../rating/1v1.md](../rating/1v1.md)
- `2v2` は [../rating/2v2.md](../rating/2v2.md)
- `3v3` は [../rating/3v3.md](../rating/3v3.md)

## 整合性要件

同じ試合集合、同じ試合順序、同じ試合結果に対して、常に同じ結果を返さなければならない。

そのため以下を満たすこと。

- 試合順序が固定されていること
- 巻き戻し対象が固定されていること
- 再計算手順が決定的であること

## 実装フェーズ補足

初期実装では、admin による試合結果修正を先に実装し、レーティング再計算は将来タスクとして扱ってよい。

その場合でも、将来の再計算に備えて以下は先に永続化しておく。

- `match_format`
- `rated_at`
- `rating_before`
- `games_played_before`
- `wins_before`
- `losses_before`
- `draws_before`
