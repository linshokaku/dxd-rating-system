# 情報確認 thread UI 仕様

## UI 識別子

- `ui_type`: `info_thread`

## 目的

プレイヤー情報とランキング確認の結果を、各ユーザー専用の private thread に集約して表示する。

## 用語

- `実行ユーザー`: slash command または thread 内 button / pulldown を実際に操作した Discord ユーザー。
- `紐づけ対象ユーザー`: latest binding を持つユーザー。通常 `/info_thread` では実行ユーザー自身、`/dev_info_thread` では指定した `discord_user_id` を指す。

## 対象外

- `レート戦情報` チャンネルに設置する公開 button UI の詳細。
  - [info_channel.md](info_channel.md)
- `/info_thread`、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season`、`/dev_info_thread`、`/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` の slash command 入出力仕様。
  - [../commands/user-commands.md](../commands/user-commands.md)
  - [../commands/dev-commands.md](../commands/dev-commands.md)
- ランキングの並び順、順位差分、比較不能時の計算ルール。
  - [../leaderboard/ranking.md](../leaderboard/ranking.md)
- snapshot の生成と保持ルール。
  - [../leaderboard/snapshots.md](../leaderboard/snapshots.md)

## 前提

- `/info_thread` または `/dev_info_thread` が成功したとき、Bot は `レート戦情報` チャンネル配下に private thread を 1 つ作成する。
- `レート戦情報` チャンネルの公開 button UI は thread 作成導線だけを担当し、この `info_thread` は作成後の情報表示先として使う。
- `/info_thread`、`/dev_info_thread`、`/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season`、`/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` では、コマンドの実行チャンネルに関わらず同じ `レート戦情報` チャンネル配下を使う。
- 対象ユーザーが実ユーザーなら、情報確認用 thread には対象ユーザー本人、admin、Bot を参加させる。
- 対象ユーザーがダミーユーザーなら、情報確認用 thread には admin と Bot のみを参加させる。
- 各紐づけ対象ユーザーに対して、最新 1 件の情報確認 thread 紐づけだけを持つ。
- `/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season`、`/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` は、新しい thread を作成せず、保存済みの `thread_id` を表示先として使う。
- 通常 `/info_thread` と `/dev_info_thread` は別の binding を持たず、同じ latest binding を共有する。
- Bot が親チャンネルで private thread を作成でき、thread 内へメッセージ投稿できることを前提とする。

## thread 作成ルール

- `/info_thread` と `/dev_info_thread` は必須引数 `command_name` として、`leaderboard`、`leaderboard_season`、`player_info`、`player_info_season` のいずれかを受け取る。
- `/dev_info_thread` は、追加で `discord_user_id` を受け取り、そのユーザーを紐づけ対象ユーザーとして扱う。
- `/info_thread` と `/dev_info_thread` の業務処理が成功した後に thread を作成する。
- `/info_thread` と `/dev_info_thread` の実行直後には、対応する `command_name` 本文を自動では投稿しない。
- thread 作成後は、紐づけ対象ユーザーの情報確認先として `thread_id` を保存する。
- 紐づけ対象ユーザーに既存の `thread_id` が保存されていても、毎回新しい thread を作成する。
- 新しい thread を作成した場合は、古い thread を表示先として再利用しない。
- `command_name` ごとの別管理や、通常 `/info_thread` と dev 系コマンド用の別管理は行わない。
- 推奨 thread 名は `情報-<display_name>` とする。

## active thread 判定

- `/info_thread` または `/dev_info_thread` で作成された private thread のうち、active thread は、実行ユーザーに現在紐づいている最新 1 件の `thread_id` を持つ thread とする。
- この仕様で定義する thread 内 button を押したときは、押下元 thread の `thread_id` と、実行ユーザーに現在紐づいている最新の `thread_id` を比較する。
- `thread_id` が一致した場合だけ、その button 操作を正当な操作として扱い、対応するコマンド相当の処理を実行する。
- `thread_id` が一致しない場合は、その thread は active でないものとして扱い、対応するコマンド相当の処理は実行しない。
- この仕様で定義する button は、押下直後に押下ユーザーへ ephemeral defer を返し、最終結果は押下ユーザーだけに見える followup で返す。
- この仕様で定義する button は、一度押した後すみやかに押下元メッセージ上の component 全体を disabled にする。
- active でない thread で button が押された場合は、押下したユーザーにだけ見えるメッセージで `このスレッドは現在の情報確認用スレッドではありません。最新の情報確認用スレッドを利用してください。` を返す。
- この active / inactive 判定ルールは、`leaderboard` 用 button だけでなく、今後この仕様で定義する `/info_thread` 由来 private thread 内 button 全般に適用する。
- `/dev_info_thread` で作成した thread であっても、thread 内 button / pulldown は紐づけ対象ユーザー本人の操作だけを想定する。
- 管理者は thread 内 UI で対象ユーザーを代理実行せず、必要な場合は `/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` を使う。

補足:

- 同じユーザーが再度 `/info_thread` または `/dev_info_thread` を実行して新しい thread を作成した後は、古い thread 上に残った button は active でないものとして無効化できる。

## 初回表示と thread 内 UI

- thread 作成直後に、情報確認用 thread であることを案内するメッセージを送る。
- 初回メッセージの文面と、その thread に表示する button / pulldown UI の種類は `command_name` に応じて変える。
- 以下の初期 UI は、紐づけ対象ユーザー本人が操作する前提で定義する。

### `command_name=player_info`

- 現在シーズンのプレイヤー情報確認用 thread として案内する。
- 初回メッセージには、`プレイヤー情報を表示` button を表示する。
- `プレイヤー情報を表示` button を押したときは、紐づけ対象ユーザー本人による `/player_info` と同等の処理を行う。
- `プレイヤー情報を表示` button を押した後すみやかに、押下元メッセージ上の component 全体を disabled にする。
- `player_info` 用の初期 UI には、選択 UI やページ送り UI は置かない。
- `player_info` の結果メッセージには追加 button は付けない。

### `command_name=player_info_season`

- シーズン別プレイヤー情報確認用 thread として案内する。
- 初回メッセージには、`season_id` pulldown と `プレイヤー情報を表示` button を表示する。
- `season_id` pulldown には、`start_at <= now()` を満たす開始済みシーズンのうち、最新 25 件だけを表示する。
- `season_id` pulldown の並び順は、`start_at` の新しい順、同値なら `season_id` の大きい順とする。
- `season_id` pulldown の各選択肢は、`label=season_name`、`description=season_id: <id>`、`value=season_id` とする。
- `プレイヤー情報を表示` button を押したときは、選択された `season_id` を使って、紐づけ対象ユーザー本人による `/player_info_season <season_id>` と同等の処理を行う。
- `プレイヤー情報を表示` button を押した後すみやかに、押下元メッセージ上の component 全体を disabled にする。
- `season_id` を選ばずに `プレイヤー情報を表示` button を押した場合は、情報表示は行わず、押下ユーザーに `シーズンを選択してください。再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。` を返す。
- `player_info_season` 用の初期 UI には、追加の選択 UI やページ送り UI は置かない。
- `player_info_season` の結果メッセージには追加 button は付けない。

### `command_name=leaderboard`

- 現在シーズンのランキング確認用 thread として案内する。
- 初回メッセージには、`match_format` pulldown と `ランキングを表示` button を表示する。
- `match_format` pulldown の選択肢は `/leaderboard` と同じ `1v1`、`2v2`、`3v3` とする。
- `ランキングを表示` button を押したときは、選択された `match_format` を使って、紐づけ対象ユーザー本人による `/leaderboard <match_format> page:1` と同等の処理を行う。
- `ランキングを表示` button を押した後すみやかに、その初回メッセージ上の pulldown と button はどちらも disabled にする。
- `match_format` を選ばずに `ランキングを表示` button を押した場合は、ランキング表示は行わず、押下ユーザーに `試合形式を選択してください。再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。` を返す。
- `leaderboard` 用の初期 UI には `page` pulldown は置かない。
- 2 ページ目以降の表示は、ランキング結果メッセージ末尾に表示する `次のページ` button で行う。

### `command_name=leaderboard_season`

- シーズン別ランキング確認用 thread として案内する。
- 初回メッセージには、`season_id` pulldown、`match_format` pulldown、`ランキングを表示` button を表示する。
- `season_id` pulldown には、`start_at <= now()` を満たす開始済みシーズンのうち、最新 25 件だけを表示する。
- `season_id` pulldown の並び順は、`start_at` の新しい順、同値なら `season_id` の大きい順とする。
- `season_id` pulldown の各選択肢は、`label=season_name`、`description=season_id: <id>`、`value=season_id` とする。
- `match_format` pulldown の選択肢は `/leaderboard_season` と同じ `1v1`、`2v2`、`3v3` とする。
- `ランキングを表示` button を押したときは、選択された `season_id` と `match_format` を使って、紐づけ対象ユーザー本人による `/leaderboard_season <season_id> <match_format> page:1` と同等の処理を行う。
- `ランキングを表示` button を押した後すみやかに、その初回メッセージ上の pulldown と button はすべて disabled にする。
- `season_id` を選ばずに `ランキングを表示` button を押した場合は、ランキング表示は行わず、押下ユーザーに `シーズンを選択してください。再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。` を返す。
- `match_format` を選ばずに `ランキングを表示` button を押した場合は、ランキング表示は行わず、押下ユーザーに `試合形式を選択してください。再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。` を返す。
- `season_id` と `match_format` の両方を選ばずに `ランキングを表示` button を押した場合は、ランキング表示は行わず、押下ユーザーに `シーズンと試合形式を選択してください。再度操作するには、情報確認チャンネルのボタンから新しい情報確認用スレッドを作成してください。` を返す。
- `leaderboard_season` 用の初期 UI には `page` pulldown は置かない。
- 2 ページ目以降の表示は、ランキング結果メッセージ末尾に表示する `次のページ` button で行う。

## `/player_info` と `/dev_player_info` による表示

### 参照対象

- `/player_info` の表示対象は、現在シーズンにおける実行ユーザー自身のプレイヤー情報とする。
- `/dev_player_info` の表示対象は、現在シーズンにおける指定 `discord_user_id` のプレイヤー情報とする。
- 表示先は、対象ユーザーに現在紐づいている latest binding の thread とする。

### 表示形式

- thread には、既存の `player_info` 本文をそのまま投稿する。
- 本文には、各 `match_format` ごとに `rating`、`games_played`、`wins`、`losses`、`draws`、`last_played_at` を含める。
- `/dev_player_info` でも本文フォーマット自体は `/player_info` と同一にする。

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

## `/player_info_season` と `/dev_player_info_season` による表示

### 参照対象

- `/player_info_season` の表示対象は、指定 `season_id` における実行ユーザー自身のプレイヤー情報とする。
- `/dev_player_info_season` の表示対象は、指定 `season_id` における指定 `discord_user_id` のプレイヤー情報とする。
- 表示先は、対象ユーザーに現在紐づいている latest binding の thread とする。

### 表示形式

- thread には、既存の `player_info_season` 本文をそのまま投稿する。
- 本文には、`season_id`、`season_name`、各 `match_format` ごとの `rating`、`games_played`、`wins`、`losses`、`draws`、`last_played_at` を含める。
- `/dev_player_info_season` でも本文フォーマット自体は `/player_info_season` と同一にする。

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

## `/leaderboard` と `/dev_leaderboard` によるランキング表示

### 参照対象

- `/leaderboard` の表示対象は、現在シーズンかつ指定 `match_format` の current leaderboard とする。
- `/dev_leaderboard` の表示対象も、現在シーズンかつ指定 `match_format` の current leaderboard とする。
- 現在ランキングは、稼働中シーズンの `player_format_stats` を参照する。
- 順位差分は、`1日前`、`3日前`、`7日前` の `leaderboard_snapshots` を参照する。
- 1 ページあたりの表示件数は 20 件とする。
- 表示先は、対象ユーザーに現在紐づいている latest binding の thread とする。

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
- ランキング結果メッセージの末尾には、そのページの次ページが存在する場合だけ `次のページ` button を付ける。
- `次のページ` button を押したときは、表示中メッセージの `match_format` と `page` を引き継いで `/leaderboard <match_format> page:n+1` と同等の処理を行う。
- `次のページ` button を押した後すみやかに、押下元ランキングメッセージ上の button は disabled にする。
- 次ページが存在しない最終ページでは `次のページ` button を表示しない。
- この `次のページ` button は、thread 内の `ランキングを表示` button から表示したランキング結果だけでなく、slash command `/leaderboard <match_format> page:n` または `/dev_leaderboard <match_format> page:n <discord_user_id>` を直接実行して thread に投稿したランキング結果にも同様に付ける。

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

## `/leaderboard_season` と `/dev_leaderboard_season` によるランキング表示

### 参照対象

- `/leaderboard_season` の表示対象は、開始済みの指定 `season_id` かつ指定 `match_format` の leaderboard とする。
- `/dev_leaderboard_season` の表示対象も、開始済みの指定 `season_id` かつ指定 `match_format` の leaderboard とする。
- シーズン別ランキングは、指定 `season_id` の `player_format_stats` を参照する。
- `leaderboard_snapshots` は参照しない。
- 1 ページあたりの表示件数は 20 件とする。
- 表示先は、対象ユーザーに現在紐づいている latest binding の thread とする。

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
- ランキング結果メッセージの末尾には、そのページの次ページが存在する場合だけ `次のページ` button を付ける。
- `次のページ` button を押したときは、表示中メッセージの `season_id`、`match_format`、`page` を引き継いで `/leaderboard_season <season_id> <match_format> page:n+1` と同等の処理を行う。
- `次のページ` button を押した時点で、押下元ランキングメッセージ上の button は disabled にする。
- 次ページが存在しない最終ページでは `次のページ` button を表示しない。
- この `次のページ` button は、thread 内の `ランキングを表示` button から表示したランキング結果だけでなく、slash command `/leaderboard_season <season_id> <match_format> page:n` または `/dev_leaderboard_season <season_id> <match_format> page:n <discord_user_id>` を直接実行して thread に投稿したランキング結果にも同様に付ける。

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

- 紐づけ対象ユーザーが実ユーザーなら、thread 内のメッセージは対象ユーザー本人、admin、Bot に見える。
- 紐づけ対象ユーザーがダミーユーザーなら、thread 内のメッセージは admin と Bot に見える。
- thread 内 button の成功・失敗通知は、押下したユーザーにだけ見える返答として扱ってよい。
- 同じユーザーが `/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` を繰り返し実行した場合、最新紐づけ先の同じ thread に結果を追記してよい。
- 管理者が `/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` を繰り返し実行した場合も、対象ユーザーの最新紐づけ先の同じ thread に結果を追記してよい。
- `command_name` ごとの別 thread 管理は行わないため、`leaderboard` 用に作成した thread に後から `/player_info` の結果が表示されてもよい。
- `/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` が binding を見つけられない場合は、実行者に `先に /info_thread または /dev_info_thread を実行してください。` を返す。
- `/dev_player_info`、`/dev_player_info_season`、`/dev_leaderboard`、`/dev_leaderboard_season` が bound thread を利用できない場合は、実行者に `情報確認用スレッドが見つかりません。先に /info_thread または /dev_info_thread を実行してください。` を返す。
- 同じユーザーが再度 `/info_thread` または `/dev_info_thread` を実行して新しい thread を作成した後は、古い thread へ情報を表示しない。

## 関連仕様

- 公開チャンネル側の UI は [info_channel.md](info_channel.md) を参照する。
- 情報確認用チャンネルの用途と権限は [registered_channels.md](registered_channels.md) を参照する。
- コマンド入出力仕様は [../commands/user-commands.md](../commands/user-commands.md) を参照する。
- 開発者向けコマンド仕様は [../commands/dev-commands.md](../commands/dev-commands.md) を参照する。
- ランキング計算仕様は [../leaderboard/ranking.md](../leaderboard/ranking.md) を参照する。
