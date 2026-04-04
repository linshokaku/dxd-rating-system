# 登録済みユーザー向けチャンネル仕様

## 目的

レーティングシステムに登録したユーザーが利用するチャンネル群と、各チャンネルの閲覧権限、書き込み権限、Bot が作成する private thread の役割を定義する。

## スコープ

- 登録後に利用できるチャンネルの種類と用途。
- 各チャンネルの推奨チャンネル名。
- 各チャンネルの閲覧権限、書き込み権限、thread 作成権限。
- Bot が専用親チャンネル配下で作成する private thread の用途。

## 対象外

- 各 UI component の表示文言や payload の詳細。
- slash command や outbox の通知配送仕様。
- Discord サーバー全体の category 構成。
- 登録済みユーザー向け権限を Discord 上でどの role にどう割り当てるかの詳細実装。

## 用語

- 登録済みユーザー:
  - レーティングシステム上でプレイヤー登録が完了している Discord ユーザー。
- 登録済みユーザー向け閲覧権限:
  - 登録済みユーザーだけに付与する閲覧権限。
  - Discord 上の実装としては、専用 role を付与して channel permission overwrite に使う方式を推奨する。
- 推奨 role 名:
  - `レート戦参加者`

## チャンネル一覧

| 論理名 | 推奨チャンネル名 | 主な用途 |
| --- | --- | --- |
| `matchmaking_channel` | `レート戦マッチング` | マッチングキュー参加 UI の設置、`/join`・`/dev_join`・参加 UI 起点の在席確認 thread と試合連絡 thread の親チャンネル |
| `matchmaking_news_channel` | `レート戦マッチ速報` | マッチ成立アナウンスと観戦 button の設置 |
| `info_channel` | `レート戦情報` | `/info_thread` で作成する情報確認 thread の親チャンネル |
| `system_announcements_channel` | `レート戦アナウンス` | admin からのシステム告知 |
| `admin_contact_channel` | `運営連絡・フィードバック` | admin への連絡、問い合わせ、フィードバック |

## 共通権限ルール

### `matchmaking_channel` / `matchmaking_news_channel` / `info_channel` / `system_announcements_channel`

- 登録済みユーザー、admin、Bot が閲覧できる。
- 未登録ユーザーは閲覧できない。
- 一般ユーザーはメッセージ送信できない。
- 一般ユーザーは public thread を作成できない。
- 一般ユーザーは private thread を作成できない。
- Bot は運用上必要なメッセージ送信と private thread 作成を行える。
- admin は保守上必要なメッセージ送信を行える。

補足:

- この仕様でいう一般ユーザーとは、admin ではない人間ユーザー全般を指す。
- つまり登録済みユーザーも、これら 4 チャンネルでは閲覧と UI 操作だけを行い、通常メッセージ送信や thread 作成は行わない。

### `admin_contact_channel`

- 誰でも閲覧できる。
- 誰でもメッセージ送信できる。
- admin と Bot は返信できる。
- 初期版では通常メッセージでのやり取りを想定し、thread 作成権限は必須としない。

## チャンネル別仕様

### `matchmaking_channel`

- 推奨チャンネル名は `レート戦マッチング` とする。
- このチャンネルには、マッチングキュー参加用の常設メッセージを 1 つ以上設置する。
- 登録済みユーザーは、設置された UI で試合形式と階級を選び、参加ボタンからマッチングキューへ参加できる。
- 現時点のマッチング UI は、試合形式プルダウン、階級プルダウン、参加ボタンのみで構成する。
- 在席更新とキュー退出は、このチャンネルの UI には含めない。
- `/join`、`/dev_join`、またはこのチャンネルの参加 UI からキュー参加した場合、Bot は在席確認用の private thread をこのチャンネル配下に作成する。
- 在席確認 thread は、対象ユーザーが実ユーザーなら対象ユーザー本人、admin、Bot だけが閲覧できる。
- 対象ユーザーがダミーユーザーの `/dev_join` では、在席確認 thread は admin と Bot だけが閲覧できる。
- 在席確認 thread では、在席確認、離席、在席確認リマインド、キュー期限切れに関する連絡を行える。
- Bot は、マッチ成立時に参加者向けの private thread をこのチャンネル配下に作成し、観戦応募成功者を後から追加できるようにする。
- 試合連絡 thread は、試合参加者、後から追加された観戦者、admin、Bot が閲覧できる。
- 試合連絡 thread は、チーム分け通知、親募集、勝敗報告、承認、結果確定、観戦合流などの連絡用途を想定する。
- 試合連絡 thread の button UI 詳細は [match_operation_thread.md](match_operation_thread.md) を参照する。

補足:

- 在席確認 thread の詳細な作成ルールと可視性は、[matchmaking_presence_thread.md](matchmaking_presence_thread.md) を参照する。

推奨 thread 名の例:

- 在席確認 thread:
  - `在席確認-<display_name>`
- 試合連絡 thread:
  - `試合-<match_id>`

### `matchmaking_news_channel`

- 推奨チャンネル名は `レート戦マッチ速報` とする。
- Bot は、マッチ成立ごとにアナウンスメッセージを 1 件投稿する。
- 各アナウンスメッセージには、試合形式、試合階級、チーム分けを表示し、その試合への観戦 button を設置する。
- このチャンネルのアナウンスでは、試合参加者への mention ではなく表示名テキストを使う。
- 一般ユーザーはこのチャンネルへ通常メッセージを送らない。
- 一般ユーザーは thread を作成しない。
- 動的なアナウンス UI の詳細は [matchmaking_news_match_announcement.md](matchmaking_news_match_announcement.md) を参照する。

### `info_channel`

- 推奨チャンネル名は `レート戦情報` とする。
- このチャンネルには、情報確認導線を示す常設メッセージを 1 つ以上設置する。
- 常設メッセージには、`/info_thread command_name:<literal>` と同等の操作が行えるボタン UI を含める。
- 常設ボタンは、少なくとも `leaderboard`、`leaderboard_season`、`player_info`、`player_info_season` の 4 種類の info thread 作成導線を持つ。
- 登録済みユーザーは、このチャンネルを閲覧できる。
- 一般ユーザーはこのチャンネルへ通常メッセージを送らない。
- 一般ユーザーは public thread を作成しない。
- 一般ユーザーは private thread を作成しない。
- `/info_thread` が成功した場合、Bot はこのチャンネル配下に情報確認用の private thread を作成する。
- 情報確認 thread は、実行ユーザー本人、admin、Bot が閲覧できる。
- `/player_info`、`/player_info_season`、`/leaderboard`、`/leaderboard_season` の結果は、このチャンネル配下の情報確認 thread に集約する。
- public 側の常設ボタンは thread 作成導線として扱い、private thread 側の button / pulldown UI は情報取得導線として扱う。
- 情報確認 thread の詳細仕様は [info_thread.md](info_thread.md) を参照する。

### `system_announcements_channel`

- 推奨チャンネル名は `レート戦アナウンス` とする。
- admin だけが告知メッセージを投稿できるチャンネルとする。
- Bot は必要に応じて補助的なアナウンス投稿を行ってよい。
- 一般ユーザーはこのチャンネルへ通常メッセージを送らない。
- 一般ユーザーは thread を作成しない。

### `admin_contact_channel`

- 推奨チャンネル名は `運営連絡・フィードバック` とする。
- 連絡、問い合わせ、改善提案、フィードバックの受付窓口として使う。
- 誰でも通常メッセージを書き込める。
- 登録前ユーザーからの問い合わせも受け付けられるよう、公開チャンネルとして扱ってよい。

## 登録前後の見え方

- 未登録ユーザーは、少なくとも登録導線用チャンネルと `admin_contact_channel` を閲覧できる状態を想定する。
- 未登録ユーザーは、`matchmaking_channel`、`matchmaking_news_channel`、`info_channel`、`system_announcements_channel` には入れない。
- 登録完了後は、登録済みユーザー向け閲覧権限の対象となり、`matchmaking_channel`、`matchmaking_news_channel`、`info_channel`、`system_announcements_channel` を閲覧できる。

## 関連仕様

- 登録 UI の詳細は [register.md](register.md) を参照する。
- マッチングチャンネル UI の詳細は [matchmaking_channel.md](matchmaking_channel.md) を参照する。
- 在席確認 thread UI の詳細は [matchmaking_presence_thread.md](matchmaking_presence_thread.md) を参照する。
- 試合運営 thread UI の詳細は [match_operation_thread.md](match_operation_thread.md) を参照する。
- 情報確認 thread UI の詳細は [info_thread.md](info_thread.md) を参照する。
- マッチ速報アナウンス UI の詳細は [matchmaking_news_match_announcement.md](matchmaking_news_match_announcement.md) を参照する。
- UI 全体の共通方針は [common.md](common.md) を参照する。
- UI 設置チャンネル管理コマンドの詳細は [setup_channel.md](setup_channel.md) を参照する。
