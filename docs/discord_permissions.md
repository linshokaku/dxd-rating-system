# Discord Bot 必要権限

## 目的

現行実装の Bot を Discord サーバーで運用するために必要な OAuth scope、Gateway Intents、サーバー権限を整理する。

## スコープ

- slash command の登録と実行
- UI 設置チャンネルの作成、初期メッセージ配置、撤収
- 登録完了時の `レート戦参加者` role 付与
- outbox 経由のチャンネル通知と DM 通知
- `admin_operations_channel` への運用通知投稿

## 対象外

- 将来実装するかもしれない thread 自動生成機能
- Discord サーバー全体の role 設計
- admin 権限を誰に付与するかという運用判断

## OAuth2 scope

Bot 招待時に少なくとも以下を付与する。

- `bot`
- `applications.commands`

## Gateway Intents

- 現行実装は `discord.Intents.default()` で起動する。
- 現時点では特権 Intent は使っていない。
  - `Message Content Intent` は不要
  - `Server Members Intent` は不要
  - `Presence Intent` は不要

補足:

- slash command と component interaction を中心に使っているため、メッセージ本文の監視は行っていない。
- 登録時の role 付与は interaction に含まれる実行者情報を使っており、member 一覧の購読は前提にしていない。

## サーバー権限

### 必須

| 権限 | 用途 | 理由 |
| --- | --- | --- |
| `View Channels` | UI チャンネル利用、通知配送 | 管理対象チャンネルを参照し、通知先チャンネルへ投稿するため |
| `Send Messages` | 初期メッセージ配置、通知配送 | UI 初期メッセージ、マッチ通知、各種案内を投稿するため |

### セットアップ機能込みで必要

| 権限 | 用途 | 理由 |
| --- | --- | --- |
| `Manage Channels` | `/admin_setup_custom_ui_channel`、`/admin_setup_ui_channels`、`/admin_cleanup_ui_channels`、`/admin_teardown_ui_channels` | 管理対象チャンネルの作成、permission overwrite 設定、cleanup、削除を行うため |
| `Manage Roles` | 登録済みユーザー向け role の作成と付与 | `レート戦参加者` role を作成し、登録完了ユーザーへ自動付与するため |
| `Manage Threads` | 管理 UI チャンネルの初期 permission overwrite 設定 | 現行実装が Bot 自身の channel overwrite に `manage_threads=True` を含めているため |

補足:

- `Manage Channels` と `Manage Roles` と `Manage Threads` は、組み込みのセットアップ運用を使う場合の必須権限とする。
- これらを Discord 側で手動運用する場合、Bot の一部機能だけを制限して動かすことはできるが、対応する admin コマンドや登録後ロール付与は失敗する。

## DM 送信

- ユーザーへの DM 送信自体に Discord サーバー権限は不要。
- ただし、通知先ユーザーが DM を拒否している場合、DM 通知は失敗する。

## 現時点では不要

以下は現行コードでは使っていないため、Bot に必須とはしない。

- `Administrator`
- `Manage Messages`
- `Create Public Threads`
- `Create Private Threads`
- `Send Messages in Threads`
- `Read Message History`
- `Attach Files`
- `Embed Links`
- `Mention @everyone, @here, and All Roles`

補足:

- `docs/ui/registered_channels.md` では将来の private thread 利用を仕様として定義しているが、現行コードはまだ thread 自動作成を実装していない。
- ただし、現行の UI セットアップ実装は Bot 自身の channel overwrite に `Manage Threads` を含めているため、この権限は現時点でも必要とする。
- そのため、thread 系権限のうち `Create Public Threads` `Create Private Threads` `Send Messages in Threads` は将来必要になる可能性がある権限として扱い、現時点の必須権限には含めない。
- Bot は現在 plain text のみを送信しており、添付ファイルや embed は使っていない。

## 運用上の注意

- Bot 内でいう `admin` は Discord の `Administrator` 権限ではなく、環境変数 `SUPER_ADMIN_USER_IDS` に含めた Discord user ID で判定する。
- `Manage Roles` を使って `レート戦参加者` role を付与するには、Bot の role をその role より上位に置く必要がある。
- UI チャンネルの実際の閲覧可否は、Bot のサーバー権限に加えて channel permission overwrite の影響を受ける。
- `admin_operations_channel` の実際の閲覧可否は、`SUPER_ADMIN_USER_IDS` に含まれる各ユーザーへの個別 permission overwrite で決まる前提とする。
- `/admin_setup_custom_ui_channel`、`/admin_setup_ui_channels`、`/admin_cleanup_ui_channels`、`/admin_teardown_ui_channels` は、事前に判定できた不足権限をレスポンスへ表示してよい。
- これらのコマンドは、Discord API が返した `403 Forbidden` の `status` / `error code` / `text` をレスポンスへ補足表示してよい。

## 関連仕様

- UI チャンネル構成は [ui/registered_channels.md](ui/registered_channels.md) を参照する。
- admin 専用運用チャンネルの詳細は [ui/admin_operations_channel.md](ui/admin_operations_channel.md) を参照する。
- マッチングチャンネル UI の詳細は [ui/matchmaking_channel.md](ui/matchmaking_channel.md) を参照する。
- `レート戦情報` の公開 button UI の詳細は [ui/info_channel.md](ui/info_channel.md) を参照する。
- UI 設置コマンドは [ui/setup_channel.md](ui/setup_channel.md) を参照する。
- コマンド全体の仕様は [commands/user-commands.md](commands/user-commands.md) と [commands/dev-commands.md](commands/dev-commands.md) を参照する。
