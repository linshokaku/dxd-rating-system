# 情報確認 thread UI 仕様

## UI 識別子

- `ui_type`: `info_thread`

## 目的

プレイヤー情報とランキング確認の結果を、各ユーザー専用の private thread に集約して表示する。

## 対象外

- `レート戦情報` チャンネルに設置する公開 button UI の詳細。
  - [info_channel.md](info_channel.md)
- `/info_thread`、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` の slash command 入出力仕様。
  - [../commands/user-commands.md](../commands/user-commands.md)
- ランキングの並び順、順位差分、比較不能時の計算ルール。
  - [../leaderboard/ranking.md](../leaderboard/ranking.md)
- snapshot の生成と保持ルール。
  - [../leaderboard/snapshots.md](../leaderboard/snapshots.md)

## 前提

- `/info_thread` が成功したとき、Bot は `レート戦情報` チャンネル配下に private thread を 1 つ作成する。
- `レート戦情報` チャンネルの公開 button UI は thread 作成導線だけを担当し、この `info_thread` は作成後の情報表示先として使う。
- `/info_thread`、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` では、コマンドの実行チャンネルに関わらず同じ `レート戦情報` チャンネル配下を使う。
- Bot は、情報確認用 thread に実行ユーザーと admin を参加させる。
- thread の閲覧対象は、少なくとも実行ユーザー、admin、Bot とする。
- 各ユーザーに対して、最新 1 件の情報確認 thread 紐づけだけを持つ。
- `/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` は、新しい thread を作成せず、保存済みの `thread_id` を表示先として使う。
- Bot が親チャンネルで private thread を作成でき、thread 内へメッセージ投稿できることを前提とする。

## thread 作成ルール

- `/info_thread` は必須引数 `command_name` として、`leaderboard`、`leaderboard_season`、`player_info`、`player_info_season` のいずれかを受け取る。
- `/info_thread` の業務処理が成功した後に thread を作成する。
- `/info_thread` 実行直後には、対応する `command_name` 本文を自動では投稿しない。
- thread 作成後は、実行ユーザーの情報確認先として `thread_id` を保存する。
- 実行ユーザーに既存の `thread_id` が保存されていても、毎回新しい thread を作成する。
- 新しい thread を作成した場合は、古い thread を表示先として再利用しない。
- 推奨 thread 名は `情報-<display_name>` とする。

## 初回表示と予定 UI

- thread 作成直後に、情報確認用 thread であることを案内するメッセージを送る。
- 初回メッセージの文面と、その thread に将来設置する button / pulldown UI の種類は `command_name` に応じて変える。

### `command_name=player_info`

- 現在シーズンのプレイヤー情報確認用 thread として案内する。
- 初回メッセージでは、将来この thread 内の button から `/player_info` と同等の操作を行えるようにすることを案内する。
- 将来 UI は、`/player_info` と同等の処理を起動する button のみを置く。

### `command_name=player_info_season`

- シーズン別プレイヤー情報確認用 thread として案内する。
- 初回メッセージでは、将来この thread 内の `season_id` pulldown と button から `/player_info_season` と同等の操作を行えるようにすることを案内する。
- 将来 UI は、`season_id` pulldown と実行 button を置く。

### `command_name=leaderboard`

- 現在シーズンのランキング確認用 thread として案内する。
- 初回メッセージでは、将来この thread 内の `match_format` pulldown、`page` pulldown、button から `/leaderboard` と同等の操作を行えるようにすることを案内する。
- 将来 UI は、`match_format` pulldown、`page` pulldown、実行 button を置く。

### `command_name=leaderboard_season`

- シーズン別ランキング確認用 thread として案内する。
- 初回メッセージでは、将来この thread 内の `season_id` pulldown、`match_format` pulldown、`page` pulldown、button から `/leaderboard_season` と同等の操作を行えるようにすることを案内する。
- 将来 UI は、`season_id` pulldown、`match_format` pulldown、`page` pulldown、実行 button を置く。

## `/player_info` による表示

### 参照対象

- 表示対象は、現在シーズンにおける実行ユーザー自身のプレイヤー情報とする。

### 表示形式

- thread には、既存の `player_info` 本文をそのまま投稿する。
- 本文には、各 `match_format` ごとに `rating`、`games_played`、`wins`、`losses`、`draws`、`last_played_at` を含める。

表示例:

```text
プレイヤー情報
1v1
rating: 1500.00
games_played: 0
wins: 0
losses: 0
draws: 0
last_played_at: -
2v2
...
3v3
...
```

## `/player_info_season` による表示

### 参照対象

- 表示対象は、指定 `season_id` における実行ユーザー自身のプレイヤー情報とする。

### 表示形式

- thread には、既存の `player_info_season` 本文をそのまま投稿する。
- 本文には、`season_id`、`season_name`、各 `match_format` ごとの `rating`、`games_played`、`wins`、`losses`、`draws`、`last_played_at` を含める。

表示例:

```text
プレイヤー情報
season_id: 1
season_name: season 1
1v1
rating: 1500.00
games_played: 0
wins: 0
losses: 0
draws: 0
last_played_at: -
2v2
...
3v3
...
```

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

## 可視性と運用

- thread 内のメッセージは、実行ユーザー、admin、Bot に見える。
- 同じユーザーが `/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` を繰り返し実行した場合、最新紐づけ先の同じ thread に結果を追記してよい。
- `command_name` ごとの別 thread 管理は行わないため、`leaderboard` 用に作成した thread に後から `/player_info` の結果が表示されてもよい。
- 同じユーザーが再度 `/info_thread` を実行して新しい thread を作成した後は、古い thread へ情報を表示しない。

## 関連仕様

- 公開チャンネル側の UI は [info_channel.md](info_channel.md) を参照する。
- 情報確認用チャンネルの用途と権限は [registered_channels.md](registered_channels.md) を参照する。
- コマンド入出力仕様は [../commands/user-commands.md](../commands/user-commands.md) を参照する。
- ランキング計算仕様は [../leaderboard/ranking.md](../leaderboard/ranking.md) を参照する。
