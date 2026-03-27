# 対応フォーマット仕様

## 目的

Bot が扱う対戦フォーマットを定義し、各フォーマットの共通パラメータ、独立管理の範囲、データモデル方針を定める。

## 対応フォーマット

現時点で対応する `match_format` は以下の 3 つとする。

| `match_format` | 形式 | `team_size` | `batch_size` | `players_per_batch` | 初期挙動 |
| --- | --- | ---: | ---: | ---: | --- |
| `1v1` | 1 人 vs 1 人 | 1 | 2 | 4 | 4 人集めて 2 試合同時に作る |
| `2v2` | 2 人 vs 2 人 | 2 | 1 | 4 | 4 人集めて 1 試合作る |
| `3v3` | 3 人 vs 3 人 | 3 | 1 | 6 | 6 人集めて 1 試合作る |

`players_per_batch` は以下で求める。

```text
players_per_batch = team_size * 2 * batch_size
```

## 独立管理の範囲

各プレイヤーは、各 `season_id` と各 `match_format` の組み合わせごとに独立した以下の値を持つ。

- `rating`
- `games_played`
- `wins`
- `losses`
- `draws`

補足:

- `1v1` の試合は `1v1` の保持値だけを更新する
- `2v2` の試合は `2v2` の保持値だけを更新する
- `3v3` の試合は `3v3` の保持値だけを更新する
- 同一 `match_format` でも `season_id` が違えば保持値は別物とする
- キュー参加条件の判定には、参加時点で稼働中のシーズンの `rating` を用いる

現時点では、以下は独立管理の対象に含めない。

- ペナルティ集計

ペナルティはフォーマット共通の運用指標として扱う。

## キュー参加の基本方針

- 1 人のプレイヤーが同時に参加できるキューは、全フォーマット・全階級を通して 1 つだけとする
- したがって、`status = 'waiting'` のキュー行はプレイヤー単位で 1 件までとする
- `/join` と `レート戦マッチング` チャンネルの参加 UI は `match_format` と `queue_name` を受け取り、その組み合わせで参加先キューを解決する

## バッチの考え方

### 初期仕様

- `1v1` は `batch_size = 2` とし、1 回のマッチング試行で 2 試合を同時に生成する
- `2v2` と `3v3` は `batch_size = 1` とし、1 回のマッチング試行で 1 試合だけ生成する

### 将来拡張

将来的に人口が増えた場合、`2v2` と `3v3` についても `batch_size > 1` を導入できる設計にしてよい。

ただし初期仕様では、以下のみを確定仕様とする。

- `1v1` の `batch_size = 2`
- `2v2` の `batch_size = 1`
- `3v3` の `batch_size = 1`

`2v2` と `3v3` の `batch_size > 1` を導入する場合は、複数試合同時最適化は行わず、以下の逐次構築方式を採用する。

1. バッチ内の全プレイヤーを、対象フォーマットのレート降順でソートする
2. 先頭から、`2v2` なら 4 人ずつ、`3v3` なら 6 人ずつ切り出して試合を作る
3. 各試合の内部では、その試合に含まれるプレイヤーだけを対象に、期待勝率が `0.5` に最も近くなるようチーム分けする

補足:

- バッチ間での全体最適化は行わない
- 公平性評価は各試合単位で独立に行う

## データモデル方針

既存 DB 互換性は考慮せず、以下の分離を推奨する。

### `players`

`players` はプレイヤー登録の基底情報を持つ。
表示用途の軽量な補助情報は、必要に応じて同じテーブルへ持たせてよい。

最低限:

- `id`
- `discord_user_id`
- `created_at`

補足:

- Bot が保持する表示名キャッシュの詳細は [players/identity.md](players/identity.md) を参照する

### `player_format_stats`

フォーマット別のレートと戦績は `player_format_stats` のような専用テーブルで持つ。

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
- `created_at`
- `updated_at`

制約:

- `UNIQUE (player_id, season_id, match_format)`

意図:

- `players` を登録情報に限定し、フォーマット追加時の拡張を容易にする
- `1v1`、`2v2`、`3v3` の独立管理を素直に表現する
- シーズンごとのレートと戦績をそのまま永続化できるようにする

### `match_queue_entries`

キュー行には少なくとも以下を持たせる。

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

補足:

- `match_format` は冗長に見えても持たせてよい
- `queue_class_id` からも導けるが、検索条件と監査を簡潔にできる
- `season_id` は持たせない
- シーズン切替時も既存の `waiting` 行はそのまま残してよい

### `matches`

試合本体には少なくとも以下を持たせる。

- `id`
- `match_format`
- `queue_class_id`
- `started_season_id`
- `created_at`

補足:

- `match_id` 単位で試合進行を管理する
- `1v1` の 1 バッチ 2 試合は、2 件の `matches` 行として持つ
- 初期仕様では batch 専用テーブルは必須ではない
- `created_at` を `started_at` とみなし、その時刻を含むシーズンを `started_season_id` に保存する

### 試合関連テーブル

以下のテーブルは `match_id` を通して `match_format` に従属する。

- `match_participants`
- `active_match_states`
- `active_match_player_states`
- `match_reports`
- `finalized_match_results`
- `finalized_match_player_results`

### 拡張余地

将来的にバッチ単位の監査や通知を強めたくなった場合のみ、`matching_batches` のような補助テーブルを追加してよい。

初期段階では、バッチはマッチングアルゴリズム上の概念に留め、永続化は必須としない。
