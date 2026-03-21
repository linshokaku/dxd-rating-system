# レーティング共通仕様

## 目的

`1v1`、`2v2`、`3v3` の各フォーマットで独立した Elo 系レーティングを管理する。

## 共通方針

- 各プレイヤーはフォーマットごとに独立したレートと戦績を持つ
- レート更新は結果確定時にのみ行う
- 無効試合はレート対象外とする
- 計算は内部では浮動小数で持ち、表示時だけ丸めてよい

## 保持データ

フォーマット別の保持値は `player_format_stats` に持たせる。

最低限:

- `rating`
- `games_played`
- `wins`
- `losses`
- `draws`
- `last_played_at`

## 試合履歴

将来の結果修正と再計算に備え、レート対象試合ごとに少なくとも以下を永続化する。

- `match_format`
- `rated_at`
- `final_result`
- 各参加プレイヤーの `rating_before`
- 各参加プレイヤーの `games_played_before`
- 各参加プレイヤーの `wins_before`
- 各参加プレイヤーの `losses_before`
- 各参加プレイヤーの `draws_before`

## 初期値

新規プレイヤーの各フォーマットの初期値は以下とする。

- `rating = 1500`
- `games_played = 0`
- `wins = 0`
- `losses = 0`
- `draws = 0`

## 試合結果の表現

Team A 視点の実結果 `y` は以下とする。

- Team A 勝ち: `y = 1`
- 引き分け: `y = 0.5`
- Team B 勝ち: `y = 0`
- 無効試合: レート計算対象外

## K の設計

各プレイヤーの K は、そのフォーマットにおける `games_played` で決定する。

```text
if games_played < 20:
    K = 40
elif games_played < 100:
    K = 32
else:
    K = 24
```

## 実装上の注意

- 同一試合内では、更新前レートから計算した値だけを使う
- 参加者全員の更新量を先に計算し、その後まとめて反映する
- 無効試合ではレートと戦績を一切更新しない
- `last_played_at` は勝ち、負け、引き分け時にだけ更新してよい

## フォーマット別仕様

- `1v1`: [1v1.md](1v1.md)
- `2v2`: [2v2.md](2v2.md)
- `3v3`: [3v3.md](3v3.md)
