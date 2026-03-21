# シーズン仕様

## 目的

- レーティングと戦績をシーズン単位で分離して管理できるようにする
- 次シーズンを事前作成し、シーズン切替直後も運用を止めずに継続できるようにする
- 次シーズン開始時の極端な実力差マッチングを緩和するため、限定的なレート引き継ぎを行う

## スコープ

本仕様が扱うのは以下とする。

- シーズン期間の定義
- シーズンの事前作成
- シーズン名の管理
- シーズン単位の `player_format_stats`
- 次シーズン初期レートへの carryover
- 試合とシーズンの紐付け
- シーズン完了フラグ

本仕様では、以下は扱わない。

- Web UI の画面設計
- シーズン結果の告知文面
- 管理者向け権限モデルの詳細

## 基本方針

- シーズンは全フォーマット共通で切り替える
- レートと戦績は `season_id` と `match_format` の組み合わせごとに独立して持つ
- シーズン切替時にマッチングキューはリセットしない
- 試合の所属シーズンは、その試合の開始時刻で決める
- 次シーズンの carryover は厳密性よりも実装の単純さを優先する
- 一度確定した carryover は、その後の旧シーズン試合結果修正では更新しない

## シーズン期間

- シーズン時刻は JST で扱う
- シーズンの期間は半開区間 `[start_at, end_at)` とする
- シーズン切替時刻は毎月 `14日 00:00 JST` とする

例:

- `2026-04-14 00:00 JST <= t < 2026-05-14 00:00 JST` を 1 シーズンとする
- 次シーズンは `2026-05-14 00:00 JST` に開始し、`2026-06-14 00:00 JST` に終了する

## 稼働中シーズンと次シーズン

- 稼働中シーズンは `start_at <= now < end_at` を満たす 1 件とする
- 次シーズンは、稼働中シーズンの `end_at` と同じ `start_at` を持つ直後の 1 件とする
- Bot から受け付けるシーズン操作は rename のみとする
- シーズン追加は Bot 本体ではなく日次 cron worker が行う

## `seasons`

シーズン本体は `seasons` のようなテーブルで持つ。

最低限:

- `id`
- `name`
- `start_at`
- `end_at`
- `completed`
- `completed_at`
- `created_at`
- `updated_at`

補足:

- 初期のデフォルト名は `delta` とする
- `name` は admin による rename で更新できる
- `start_at` と `end_at` は作成後に変更しない
- シーズンの追加や削除は Bot コマンドからは行わない

## 日次 worker による事前作成

日次 worker は 1 日 1 回、少なくとも以下を行う。

1. 稼働中シーズンを取得する
2. その稼働中シーズンに対する次シーズンが存在するか確認する
3. 存在しなければ、以下の値で新しいシーズンを 1 件だけ作成する
4. `end_at <= now()` を満たし、まだ `completed = false` のシーズンについて、`completed` を立てられる状態かどうかを再判定する

- `start_at = active_season.end_at`
- `end_at = start_at の翌月 14日 00:00 JST`
- `name = 'delta'`

補足:

- これは事前作成であり、切替自体は `start_at` 到達で自動的に起こる
- 次シーズンがすでに存在する場合は no-op とする
- `completed` 判定は、試合確定時の即時判定に加えて、日次 worker による補完判定も行う

## シーズン名 rename

- admin はシーズン名だけを変更できる
- rename は `season_id` を指定して行う
- rename はレート、戦績、期間、完了フラグへ影響しない

## `player_format_stats`

シーズン別かつフォーマット別のレートと戦績は `player_format_stats` に持たせる。

最低限:

- `player_id`
- `season_id`
- `match_format`
- `rating`
- `games_played`
- `wins`
- `losses`
- `draws`
- `last_played_at`
- `carryover_status`
- `carryover_source_season_id`
- `carryover_source_rating`
- `created_at`
- `updated_at`

制約:

- `UNIQUE (player_id, season_id, match_format)`

`carryover_status` は少なくとも以下を持つ。

- `pending`
- `applied`
- `not_applied`

意味:

- `pending`: そのシーズン行は作成済みだが、carryover 判定がまだ行われていない
- `applied`: carryover を適用済みである
- `not_applied`: carryover 対象外であり、初期値 `1500` を確定済みである

## レート行の作成タイミング

### シーズン作成時

新しいシーズンを作成したら、既存の全プレイヤーについて、その `season_id` に紐づく全 `match_format` 分の `player_format_stats` を作成する。

初期値:

- `rating = 1500`
- `games_played = 0`
- `wins = 0`
- `losses = 0`
- `draws = 0`
- `last_played_at = NULL`
- `carryover_status = 'pending'`

### 新規登録時

新規プレイヤー登録時には、その時点で `end_at > now()` を満たす全シーズンについて、全 `match_format` 分の `player_format_stats` を作成する。

意図:

- 稼働中シーズンだけでなく、事前作成済みの次シーズン以降にも行を先に用意しておく

## carryover の確定

### 基本方針

- carryover は、そのシーズン行のレートが初めて必要になった時点で遅延確定する
- 一度 `pending` 以外になった行は、その後の旧シーズン試合結果修正では再計算しない
- carryover 判定は `match_format` ごとに独立して行う

### 確定トリガ

少なくとも以下の処理では、対象シーズン・対象フォーマットの `player_format_stats` が `pending` なら、先に carryover 判定を行う。

- `/join` の参加条件判定
- マッチ作成時のチーム分け
- 試合結果確定時のレート更新
- admin による結果修正時の再計算

### 判定元

- 判定元は、直前シーズンの同一 `match_format` の `player_format_stats` とする
- 直前シーズン行が存在しない場合は carryover しない
- 直前シーズンの `games_played < 5` の場合は carryover しない

### 計算式

carryover を適用する場合の初期レートは以下とする。

```text
carryover_rating = min(
    1750,
    1500 + round(max(0, previous_rating - 1500) * 0.35),
)
```

### 確定時の更新

carryover を適用する場合:

- `rating = carryover_rating`
- `carryover_status = 'applied'`
- `carryover_source_season_id = previous_season_id`
- `carryover_source_rating = previous_rating`

carryover を適用しない場合:

- `rating = 1500`
- `carryover_status = 'not_applied'`
- `carryover_source_season_id = NULL`
- `carryover_source_rating = NULL`

## 試合とシーズンの関係

### 試合開始時刻

- `started_at` は `matches.created_at` とみなす
- 試合の所属シーズンは、`started_at` を含むシーズンで決める

### `matches`

試合本体には少なくとも以下を持たせる。

- `id`
- `match_format`
- `queue_class_id`
- `started_season_id`
- `created_at`

補足:

- `started_season_id` は `created_at` から毎回逆算せず、試合作成時に保存する
- これにより、後続処理は常に同じシーズンへ更新できる

### シーズン跨ぎキュー

- シーズン切替時に `match_queue_entries` はリセットしない
- シーズン切替前に `join` した待機行も、そのまま待機を継続できる
- 待機中プレイヤーの参加条件は、シーズン切替後も再判定しない
- そのため、待機行の `queue_class_id` と、マッチ作成時点の実際のレート帯がずれる場合がある
- このずれは仕様として許容し、実装簡素化を優先する

### マッチ作成時

- マッチ作成時には、その時点の `created_at` に基づいて `started_season_id` を決定する
- チーム分けや期待勝率計算に使うレートは、`started_season_id` の `player_format_stats` を参照する
- 参照する行が `pending` なら、先に carryover を確定する

### 結果確定時

- 試合結果確定時のレート更新先は、稼働中シーズンではなく `started_season_id` の `player_format_stats` とする
- したがって、前シーズン開始の試合が次シーズン開始後に finalize されても、前シーズンのレートが更新される
- 各試合の finalize 処理のたびに、その `started_season_id` について `completed` を立てられる状態かどうかを再判定する

### admin 結果修正時

- admin による結果修正の再計算対象も、対象試合の `started_season_id` に限定する
- 旧シーズンの結果修正は、その旧シーズンの `player_format_stats` を更新する
- ただし、すでに確定済みの次シーズン carryover は更新しない

## 完了フラグ

- `completed` は、終了済みシーズンについて「そのシーズン所属の全試合が一度は finalized された」ことを表す
- `completed` を `true` にできるのは `end_at <= now()` のシーズンだけとする
- `completed` を立てるかどうかの判定は、試合確定時に毎回行う
- さらに、日次で動く cron worker 側からも `completed` を立てられる状態かどうかを判定する
- 具体的には、各試合の finalize 後に、その `started_season_id` に属する未確定試合が残っているかを確認する
- 日次 worker では、`end_at <= now()` かつ `completed = false` のシーズンについて、同様に未確定試合の有無を確認する
- 未確定試合が 0 件であり、かつ `end_at <= now()` を満たす場合に `completed = true` とする
- `completed_at` は `completed = true` にした時刻を保存する
- 一度 `completed = true` になったシーズンは、admin による結果修正後も `false` に戻さない

意図:

- Web UI では `completed = true` のシーズンだけを一覧表示対象にできる
- 一方で、完了後の軽微な結果修正は引き続き許容する

## 表示名の扱い

- 終了済みシーズンの閲覧でも、当時の表示名 snapshot は保持しない
- シーズン別ランキング表示時の表示名は、現在の `players.display_name` キャッシュを参照してよい
