# AGENTS.md

## Project Overview
このリポジトリは、Discord のゲームコミュニティサーバー向けのレーティングシステムを提供する Bot を実装するためのものです。

## Product Assumptions
現時点では以下を前提とします。

- ゲームコミュニティ内の **1つのゲーム** 「DragxDrive」を対象とする
- 基本は **3v3対戦** を対象とする
- 結果報告は Bot コマンド経由で行う
- 対戦結果は参加者が登録する
- レーティング方式は **Elo** を初期採用する
- 初期レートは固定値 1500 とする
- Discord user ID をプレイヤー識別子とする
- 正しさ・保守性を優先し、早すぎる最適化は避ける

## Technical Constraints
- Hosting: Railway
- Database: Railway Postgres
- App: Discord Bot
- Scheduled jobs: Railway Cron Job
- Environment variables で設定を注入する
- Bot token や DB 接続情報をコードに埋め込まない
- 本番では PostgreSQL を前提とする
- ローカル開発でも PostgreSQL を用いる
- 依存は必要最小限にする
- README.md に起動方法を必ず記載する
- テストの実行方法と lint の実行方法は README.md を参照する

---

## Suggested Tech Stack
以下を推奨します。別案を採用する場合は、理由が明確なときのみ。

- Language: Python
- Discord library: `discord.py`
- DB access: `SQLAlchemy`
- Migration: `Alembic`
- Config: `pydantic-settings`
- PostgreSQL driver: `psycopg`
- Logging: Python 標準 logging
- Dependency management: `uv`

基本方針:
- 奇をてらった技術選定はしない
- ドキュメントの多い一般的なライブラリを優先する
- 導入コストより運用しやすさを重視する

## Directory Guidance
```text
src/
  bot/
    commands/
    services/
    runtime/
    db/
    models/
    config.py
    main.py
  jobs/
tests/
alembic/
README.md
AGENTS.md
````

ルール:

* 責務の分離はする
* ただし過剰なレイヤ分割は避ける
* 初期段階で DDD 風の大規模構成にはしない
* 1ファイルが巨大化しすぎたら分割する

---

## Coding Guidelines

* 読みやすさを優先する
* 不必要に抽象化しない
* まず動く最小構成を作る
* 関数名・変数名は明確にする
* マジックナンバーは定数化する
* 例外処理を雑に握りつぶさない
* ログを適切に残す
* 型ヒントを使う

---

## Bot Behavior Guidelines

* ユーザーに返すメッセージは簡潔で分かりやすくする
* エラー時は、何が悪いかを可能な範囲で伝える
* 内部エラーの詳細をそのまま Discord に出しすぎない
* 管理者向け操作と一般ユーザー向け操作は区別する
* 同一試合の二重登録をなるべく防ぐ

---

## Database Guidelines

* PostgreSQL 前提で設計する
* 現時点は試作段階のため、コード変更時に既存 DB 状態との互換性は必須ではない
* コード変更後に DB を再構築する前提で進めてよい
* マイグレーション管理を行う
* スキーマ変更は Alembic 等で管理する
* 本番DBに対して危険なDROP系変更は慎重に扱う
* Railway の DATABASE_URL を利用する
* 接続設定は環境変数から読む

---

## Environment Variables

サービスごとに最低限、以下を利用する想定です。

Bot service:
* `DISCORD_BOT_TOKEN`
* `DATABASE_URL`
* `SUPER_ADMIN_USER_IDS` (optional)
* `LOG_LEVEL`

Cron Job:
* `DATABASE_URL`
* `LOG_LEVEL`

`.env` はローカル専用とし、秘密情報はコミットしないこと。

---

## Railway Deployment Guidelines

* Railway 上でそのまま起動できる構成を維持する
* Bot 本体とは別に、定期実行処理は `src/jobs/` 配下のモジュールとして実装する
* 定期実行は Railway Cron Job を利用し、スケジュールは Railway 側で管理する
* Bot と Cron Job の起動コマンドを README.md に明記する
* DB 接続先は Railway Postgres を前提とする
* 環境変数未設定時は起動時に明確に失敗させる
* 本番で必要な migration 実行手順を明記する

--- 

## モデル更新
```bash
./migrate.sh "describe schema change"
```

生成された migration は `alembic/versions/` で確認してください。
許可なく、生成されたmigrationファイルを手更新しないでください。

---

## テスト
```bash
./test.sh
```

`pytest` に渡したいオプションもそのまま指定できます。

```bash
./test.sh -k registration -q
```

---

## Lint
```bash
./lint.sh
```
