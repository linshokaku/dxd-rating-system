# dxd-rating-system

## 技術スタック
- Python 3.12+
- discord.py
- SQLAlchemy
- Alembic
- PostgreSQL (psycopg)
- pydantic-settings
- uv

## レーティング仕様
- 初期レートは全フォーマット共通で `1500`
- 基本 K は `games_played` に応じて `40 / 32 / 24` を使う
- 実効 K は `基本 K x 対戦人数係数` とし、`1v1=1`、`2v2=2`、`3v3=3`
- この補正により、等レート同士では各フォーマットの 1 人あたり変動幅が近くなる

詳細仕様は [docs/README.md](docs/README.md) と [docs/rating/common.md](docs/rating/common.md) を参照してください。

## ディレクトリ構成
```text
src/
  dxd_rating/
    apps/
      bot/
        main.py
      worker/
        daily.py
        force_end_season.py
    contexts/
      common/
      players/
      restrictions/
      matchmaking/
      matches/
    platform/
      config/
      db/
      discord/
      runtime/
    shared/
tests/
  apps/
  contexts/
  platform/
alembic/
README.md
AGENTS.md
```
実装は `contexts/` と `platform/` に分け、`tests/` も同じ責務境界に合わせています。

## 環境変数
Bot service:
- `DISCORD_BOT_TOKEN`
- `DATABASE_URL`
- `MATCHMAKING_GUIDE_URL` (必須。例: `https://github.com/linshokaku/dxd-rating-system/blob/main/docs/README.md`)
- `TERMS_URL` (必須。例: `https://github.com/linshokaku/dxd-rating-system/blob/main/docs/users/terms.md`)
- `DEVELOPMENT_MODE` (任意。`true` にすると Bot は開発モードで起動)
- `SUPER_ADMIN_USER_IDS` (任意。カンマ区切りの Discord user ID。例: `123456789012345678,234567890123456789`)
- `LOG_LEVEL` (任意。未指定時は `INFO`)

Cron Job:
- `DATABASE_URL`
- `LOG_LEVEL` (任意。未指定時は `INFO`)

ローカルでは `.env.example` をコピーして `.env` を作成してください。

```bash
cp .env.example .env
```

## セットアップ
ローカル開発用の PostgreSQL は `docker compose` で起動できます。

```bash
docker compose up -d db
```

```env
DATABASE_URL=postgresql://user:password@localhost:5432/dxd_rating
```

## ローカル起動手順
```bash
cp .env.example .env
docker compose down -v
docker compose up -d db
uv sync --extra dev
sleep 1
uv run alembic upgrade head
DEVELOPMENT_MODE=true uv run python -m dxd_rating.apps.bot.main
```

開発モードで Bot を起動する場合:

```bash
DEVELOPMENT_MODE=true uv run python -m dxd_rating.apps.bot.main
```

開発モードでは `/admin_setup_ui_channels` が作成する UI チャンネルをすべて private channel として作成します。
加えて、親募集・結果報告・承認の各タイマーも開発用の短い設定で動作します。
setup 系コマンドが作成する managed UI channel は、固定カテゴリ `レート戦` 配下に自動で配置されます。カテゴリが存在しない場合は Bot が作成します。

本番モードでは一般ユーザー向け slash command を公開せず、基本操作は Bot が管理するボタン UI 経由で行う想定です。
本番運用前に、管理者が `/admin_setup_ui_channels` を実行して required UI channels を作成してください。
既存の managed UI channel を初期状態へ作り直したい場合は、`/admin_resetup_ui_channel` を利用してください。

## Cron Job
定期実行処理は `src/dxd_rating/apps/worker/` 配下に置き、Railway の Cron Job からコマンド実行する想定です。
現時点の日次エントリーポイントは次のとおりです。

```bash
uv run python -m dxd_rating.apps.worker.daily
```

このジョブは Bot 本体とは別プロセスで動作し、現在は DB 接続確認と今後の定期処理を追加するための雛形を提供します。
Railway ではこのコマンドを Cron Job に設定し、スケジュール自体は Railway 側で管理してください。
このジョブは現在、DB 接続確認、シーズン保守、ランキング snapshot の日次生成と古い snapshot の削除を行います。

テスト目的で active season を強制終了する手動 CLI app も用意しています。

```bash
uv run python -m dxd_rating.apps.worker.force_end_season
```

このコマンドは DB を直接更新し、実行時点の active season の `end_at` と次 season の `start_at` を同じ時刻へ変更します。
テスト用途専用であり、season completion 判定、次 season 作成、snapshot 更新、通知 enqueue は行いません。

## Railway デプロイ
本番は Railway 上で Bot service と daily worker service の 2 service 構成を前提とします。

- Bot service は常駐プロセスとして `uv run python -m dxd_rating.apps.bot.main` を起動します。
- daily worker service は Cron Job として `uv run python -m dxd_rating.apps.worker.daily` を実行します。
- どちらも同じリポジトリを参照し、同じ Railway Postgres を利用します。

### リポジトリ側で管理する Railway 設定
このリポジトリには Railway Config as Code 用の設定ファイルを含めています。

- Bot service: `/railway/bot.json`
- daily worker service: `/railway/daily.json`
- Python version pin: `/.python-version`

`bot.json` では以下をコード管理します。

- builder: `RAILPACK`
- pre-deploy migration: `uv run alembic upgrade head`
- start command: `uv run python -m dxd_rating.apps.bot.main`
- restart policy: `ON_FAILURE`, max retries `10`

`daily.json` では以下をコード管理します。

- builder: `RAILPACK`
- start command: `uv run python -m dxd_rating.apps.worker.daily`
- cron schedule: `10 15 * * *` (UTC)
- restart policy: `ON_FAILURE`, max retries `3`

`10 15 * * *` は毎日 `00:10 JST` です。
snapshot 生成仕様の「`JST 00:05` 以降の早い時刻」に合わせ、この時刻を初期値とします。

### Railway 上で行う初期設定
以下は Railway 側での手動設定が必要です。

1. Railway project を作成する
2. Railway Postgres を追加する
3. GitHub repository と接続する
4. Bot service を作成する
5. daily worker service を作成する
6. Bot service の Custom Config File に絶対パス `/railway/bot.json` を設定する
7. daily worker service の Custom Config File に絶対パス `/railway/daily.json` を設定する
8. 各 service に必要な環境変数を設定する

環境変数の値そのものはこのリポジトリでは自動化しません。
必要な変数名は README 先頭の「環境変数」セクションを参照してください。

Bot service に最低限必要なもの:

- `DISCORD_BOT_TOKEN`
- `DATABASE_URL`
- `MATCHMAKING_GUIDE_URL`
- `TERMS_URL`
- `LOG_LEVEL` (任意)
- `SUPER_ADMIN_USER_IDS` (任意)
- `DEVELOPMENT_MODE` は本番では未設定または `false`

daily worker service に最低限必要なもの:

- `DATABASE_URL`
- `LOG_LEVEL` (任意)

### デプロイ手順
schema change を含む deploy では Bot service を先に deploy してください。
Bot service の deploy では `preDeployCommand` により `uv run alembic upgrade head` が自動実行されます。

推奨手順:

1. Bot service を deploy する
2. migration が成功し、deployment が `Active` になることを確認する
3. daily worker service を deploy する
4. daily worker service の手動実行または初回 cron 実行が成功することを確認する

daily worker service には `preDeployCommand` を設定していません。
migration の責務は Bot service に集約します。

### 初回デプロイ後の確認
初回 deploy 後は、Discord サーバー上で管理者が `/admin_setup_ui_channels` を実行して required UI channels を作成してください。

その後、少なくとも以下を確認してください。

- Bot logs で Discord 接続まで進んでいること
- daily worker logs で DB connectivity check、season maintenance、leaderboard snapshot maintenance が出ること
- daily worker 実行時に admin operations 通知が outbox 経由で送られること

Bot に必要な Discord 権限は [docs/discord_permissions.md](docs/discord_permissions.md) を参照してください。

### 自動化されない手順
今回のリポジトリ変更だけでは、以下は自動化されません。

- Railway 上での service 作成
- Custom Config File の紐付け
- 環境変数の設定
- 初回 deploy 順序の判断

つまり、このリポジトリは「deploy 時に使う service 設定の再現性」を高めますが、Railway project 自体の初期作成までは行いません。

## モデル更新
```bash
./migrate.sh "describe schema change"
```

生成された migration は `alembic/versions/` で確認してください。
許可なく、生成されたmigrationファイルを手更新しないでください。

## テスト
```bash
./test.sh
```

`pytest` に渡したいオプションもそのまま指定できます。

```bash
./test.sh -k registration -q
```

## Lint
```bash
./lint.sh
```

## DB 停止
```bash
docker compose down
```

ローカルデータも削除する場合:

```bash
docker compose down -v
```
