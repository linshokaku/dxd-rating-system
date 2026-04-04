# ランキング確認 thread UI 仕様

## UI 識別子

- `ui_type`: `leaderboard_thread`

## 目的

指定したフォーマットの現在シーズンランキングまたは指定シーズンランキングを、各ユーザー専用の private thread に集約して表示する。

## 対象外

- `/leaderboard_thread`、`/leaderboard`、`/leaderboard_season` の slash command 入出力仕様。
  - [../commands/user-commands.md](../commands/user-commands.md)
- ランキングの並び順、順位差分、比較不能時の計算ルール。
  - [../leaderboard/ranking.md](../leaderboard/ranking.md)
- snapshot の生成と保持ルール。
  - [../leaderboard/snapshots.md](../leaderboard/snapshots.md)

## 前提

- `/leaderboard_thread` が成功したとき、Bot は `レート戦ランキング` チャンネル配下に private thread を 1 つ作成する。
- `/leaderboard_thread`、`/leaderboard`、`/leaderboard_season` では、コマンドの実行チャンネルに関わらず同じ `レート戦ランキング` チャンネル配下を使う。
- Bot は、ランキング確認用 thread に実行ユーザーと admin を参加させる。
- thread の閲覧対象は、少なくとも実行ユーザー、admin、Bot とする。
- 各ユーザーに対して、最新 1 件のランキング確認 thread 紐づけだけを持つ。
- `/leaderboard` と `/leaderboard_season` は、新しい thread を作成せず、保存済みの `thread_id` を表示先として使う。
- Bot が親チャンネルで private thread を作成でき、thread 内へメッセージ投稿できることを前提とする。

## thread 作成ルール

- `/leaderboard_thread` の業務処理が成功した後に thread を作成する。
- thread 作成後は、実行ユーザーのランキング確認先として `thread_id` を保存する。
- 実行ユーザーに既存の `thread_id` が保存されていても、毎回新しい thread を作成する。
- 新しい thread を作成した場合は、古い thread を表示先として再利用しない。
- 推奨 thread 名は `ランキング-<display_name>` とする。

## 初回表示

- thread 作成直後に、ランキング確認用 thread であることを案内するメッセージを送る。
- 案内メッセージには、少なくとも以下を含める。
  - この thread がランキング確認専用であること。
  - `/leaderboard match_format:<format> page:<n>` で表示できること。
  - `/leaderboard_season season_id:<id> match_format:<format> page:<n>` でシーズン別ランキングを表示できること。
  - `match_format` には `1v1`、`2v2`、`3v3` を指定できること。

## `/leaderboard` によるランキング表示

### 参照対象

- 表示対象は、現在シーズンかつ指定 `match_format` の current leaderboard とする。
- 現在ランキングは、稼働中シーズンの `player_format_stats` を参照する。
- 順位差分は、`1日前`、`3日前`、`7日前` の `leaderboard_snapshots` を参照する。
- 1 ページあたりの表示件数は 20 件とする。

### 表示形式

- thread に投稿するヘッダには、少なくとも以下を含める。
  - `season`
  - `match_format`
  - `page`
  - そのページで表示している件数範囲
- ランキング本体は、順位の昇順で 1 行ずつ表示する。
- 各行の表示順は、`順位 / ユーザー名 / rating / 1d / 3d / 7d` とする。
- `rating` は小数点以下 2 桁で表示してよい。
- 順位差分は `+3`、`0`、`-2` のように表現してよい。
- 比較不能な順位差分は `-` で表示する。

表示例:

```text
ランキング
season: 202604delta
match_format: 3v3
page: 2
items: 21-40

21 / Alice / 1623.40 / +1 / - / +4
22 / Bob / 1618.75 / 0 / -2 / -
23 / Carol / 1609.10 / -1 / +2 / +5
```

## `/leaderboard_season` によるランキング表示

### 参照対象

- 表示対象は、開始済みの指定 `season_id` かつ指定 `match_format` の leaderboard とする。
- シーズン別ランキングは、指定 `season_id` の `player_format_stats` を参照する。
- `leaderboard_snapshots` は参照しない。
- 1 ページあたりの表示件数は 20 件とする。

### 表示形式

- thread に投稿するヘッダには、少なくとも以下を含める。
  - `season_id`
  - `season_name`
  - `match_format`
  - `page`
  - そのページで表示している件数範囲
- ランキング本体は、順位の昇順で 1 行ずつ表示する。
- 各行の表示順は、`順位 / ユーザー名 / rating` とする。
- `rating` は小数点以下 2 桁で表示してよい。
- `1d`、`3d`、`7d` の順位差分列は表示しない。

表示例:

```text
ランキング
season_id: 12
season_name: 202603delta
match_format: 3v3
page: 2
items: 21-40

21 / Alice / 1623.40
22 / Bob / 1618.75
23 / Carol / 1609.10
```

### 可視性と運用

- thread 内のメッセージは、実行ユーザー、admin、Bot に見える。
- 同じユーザーが `/leaderboard` を繰り返し実行した場合、最新紐づけ先の同じ thread に結果を追記してよい。
- 同じユーザーが `/leaderboard_season` を繰り返し実行した場合も、最新紐づけ先の同じ thread に結果を追記してよい。
- 同じユーザーが再度 `/leaderboard_thread` を実行して新しい thread を作成した後は、古い thread へランキングを表示しない。

## 関連仕様

- ランキング表示用チャンネルの用途と権限は [registered_channels.md](registered_channels.md) を参照する。
- コマンド入出力仕様は [../commands/user-commands.md](../commands/user-commands.md) を参照する。
- ランキング計算仕様は [../leaderboard/ranking.md](../leaderboard/ranking.md) を参照する。
