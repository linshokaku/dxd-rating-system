# admin専用運用チャンネル仕様

## 目的

- super admin が運用相談を行うための専用チャンネルを定義する。
- Bot や cron worker からの低ノイズな運用通知の受け皿を定義する。
- 公開の問い合わせ窓口である `admin_contact_channel` と、admin 内部運用の場を分離する。

## スコープ

- `admin_operations_channel` の用途。
- 推奨チャンネル名。
- 閲覧権限、書き込み権限、thread 方針。
- Bot / worker から送る運用通知の初期スコープ。
- `SUPER_ADMIN_USER_IDS` と UI セットアップコマンドとの関係。

## 対象外

- このチャンネル上で使う専用 button UI や thread UI。
- worker 実装の詳細なクラス構成。
- 通常ログの転送。
- Discord サーバー全体の category 構成。

## 基本方針

- 論理名は `admin_operations_channel` とする。
- 推奨チャンネル名は `運営専用` とする。
- singleton の private text channel として扱う。
- このチャンネルは `SUPER_ADMIN_USER_IDS` に含まれるユーザーと Bot だけが閲覧できる。
- 既存の `admin_contact_channel` は公開の問い合わせ窓口として維持し、このチャンネルで置き換えない。
- 初期版では通常メッセージだけを使い、thread 作成は前提にしない。

## 権限

- `@everyone` には閲覧権限を付与しない。
- 登録済みユーザー向け role にも閲覧権限を付与しない。
- `SUPER_ADMIN_USER_IDS` に含まれる各 Discord user ID に対応するサーバーメンバーへ、個別の permission overwrite で閲覧権限を付与する。
- super admin は、このチャンネルで通常メッセージ送信と slash command 実行を行ってよい。
- Bot は、初期メッセージ配置、運用通知投稿、保守上必要なメッセージ送信を行ってよい。

補足:

- このチャンネルの可視性は Discord の `Administrator` 権限ではなく、環境変数 `SUPER_ADMIN_USER_IDS` をもとに決める。
- 複数人の super admin を同時に可視対象として扱えることを前提とする。

## 用途

- super admin 同士の相談。
- Bot の内部運用に関する簡潔な通知の受信。
- cron worker の起動など、アプリケーションとして意味のある動作通知の受信。

`admin_contact_channel` との役割分担:

- `admin_contact_channel`:
  - ユーザーから運営への公開窓口。
- `admin_operations_channel`:
  - 運営内部の相談と、Bot / worker の運用通知の受け皿。

## 通知方針

- このチャンネルには通常ログを転送しない。
- 通知対象は、アプリケーションとして意味のある動作通知だけに限定する。
- 初期スコープでは `daily worker` の起動通知だけを対象とする。
- `daily worker` は、起動時にこのチャンネルへ 1 通だけ通知してよい。
- 通知文面は簡潔にし、ジョブごとの進捗や詳細ログは含めない。

初期通知の例:

```text
daily worker が起動しました。
開始時刻: 2026-04-05 00:00 JST
```

## outbox 経路

- worker から Discord へ直接投稿するのではなく、既存の outbox 経路を使う前提とする。
- 運用通知用 event type は `admin_operations_notification` とする。
- payload は少なくとも以下を持つ前提とする。
  - `notification_kind`
  - `worker_name`
  - `occurred_at`
  - `destination.channel_id`
- 初期 `notification_kind` は `daily_worker_started` のみとする。

## worker 側の扱い

- worker は、`admin_operations_channel` の managed UI 記録を参照して通知先を解決してよい。
- 通知先チャンネルが見つからない場合、worker はローカルログへ warning を残して本来の処理を継続する。
- つまり、このチャンネル未設置は worker のジョブ失敗条件に含めない。

## セットアップ方針

- `admin_operations_channel` は Bot の managed UI 対象に含める。
- `/admin_setup_custom_ui_channel` で個別作成できる前提とする。
- `/admin_setup_ui_channels` の一括作成対象にも含める。
- 初期メッセージは、用途説明と「このチャンネルは super admin 専用である」ことが分かる簡潔な案内文とする。

## 関連仕様

- チャンネル一覧全体は [registered_channels.md](registered_channels.md) を参照する。
- UI 設置コマンドは [setup_channel.md](setup_channel.md) を参照する。
- 非同期通知配送は [../outbox.md](../outbox.md) を参照する。
- Bot に必要な権限は [../discord_permissions.md](../discord_permissions.md) を参照する。
