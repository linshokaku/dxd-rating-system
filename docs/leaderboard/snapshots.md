# ランキング snapshot 仕様

## 目的

- ランキングの `1日前`、`3日前`、`7日前` 比較に必要な過去順位を、read-only な参照だけで取得できるようにする
- Web 側で重い再計算や複雑な時系列復元を行わずに済むようにする

## 基本方針

- snapshot は Bot の正史データではなく、ランキング表示のための派生データとする
- snapshot は Bot と同じ PostgreSQL に保持してよい
- Web サービスは snapshot を read-only で参照する
- snapshot は日次で作成する
- 一度作成した過去 snapshot は、admin による試合結果補正があっても更新しない

意図:

- 現在ランキングは常に最新状態を反映する
- 過去 snapshot は「その時点で観測されたランキング」として扱う
- 結果補正に伴う snapshot の再生成や巻き戻しを避け、実装を単純に保つ

## 保存先

snapshot は、`leaderboard_snapshots` のような専用テーブルに保存する。

1 行の単位は以下とする。

- `snapshot_date`
- `season_id`
- `match_format`
- `player_id`

## 保存項目

少なくとも以下を保持する。

- `snapshot_date`
- `season_id`
- `match_format`
- `player_id`
- `rank`
- `rating`
- `games_played`
- `created_at`

補足:

- `display_name` は snapshot に複製しない
- 表示名は現在の `players` テーブルのキャッシュを参照してよい
- snapshot は順位比較に必要な最小限の値に絞る

## snapshot 日付

- `snapshot_date` は JST の日付で持つ
- 時刻ではなく日付単位で扱う

例:

- 2026-03-21 JST の日次実行で作る snapshot の `snapshot_date` は `2026-03-21`

## 対象プレイヤー

各 `match_format` の snapshot に含めるのは、snapshot 生成時点で以下を満たすプレイヤーのみとする。

- `games_played > 0`

並び順と順位計算は [ranking.md](ranking.md) と同じ規則を使う。

補足:

- snapshot 生成対象は、生成時点で稼働中のシーズンに属する `player_format_stats` に限る

## 生成タイミング

- snapshot は 1 日 1 回生成する
- 実行時刻は `JST 00:05` 以降の早い時刻を推奨する

初期方針:

- 1 回の実行で `1v1`、`2v2`、`3v3` の全フォーマット分をまとめて作成する
- 同じ `snapshot_date` かつ同じ `season_id` のデータがすでに存在する場合は、その日の再生成を行わずに終了してよい

## 生成の原則

- snapshot 生成は決定的であること
- その日の生成に使う並び順と順位計算は、現在ランキングと同一であること
- 日次ジョブの再実行で過去 snapshot を上書きしないこと

## 保持期間

- snapshot の保持期間は `180日` とする
- 最新 180 日分を保持し、それより古い `snapshot_date` は削除してよい

補足:

- 保持期間は日付ベースで扱う
- 容量よりもシンプルな運用を優先し、長期アーカイブは初期仕様に含めない

## admin 補正時の扱い

admin による試合結果補正が行われた場合でも、既存の snapshot は更新しない。

その結果:

- 現在ランキングは補正後の `player_format_stats` を反映する
- 過去 snapshot は補正前の順位を含んだまま残りうる
- 順位変化量は「現在順位」と「保存済み snapshot 順位」の差として計算する
- シーズン終了後に旧シーズンの結果修正が行われても、旧シーズンの snapshot は再生成しない

これは仕様として許容し、実装簡素化を優先する。

## 運用構成

snapshot 生成ジョブは、Bot 本体プロセスへ内包せず、同じリポジトリの別プロセスとして実装することを推奨する。

Railway での初期方針:

- コードベースは Bot と同一リポジトリに置く
- デプロイ先は Bot とは別の Railway service とする
- この service は同じ PostgreSQL を参照し、snapshot 生成と古い snapshot の削除だけを行う
- Discord Gateway 接続や slash command の待受は行わない

意図:

- Bot の再起動やスケール変更と、日次 snapshot 実行を分離する
- 運用単位は分けつつ、コードベースは増やさない

## Web 側の参照方法

Web 側は、少なくとも以下の 2 系統を読めればよい。

- 現在ランキング用の、稼働中シーズンの `player_format_stats`
- 過去比較用の `leaderboard_snapshots`

これにより、Web サービスは DB への read 権限だけでランキング表示を完結できる。
