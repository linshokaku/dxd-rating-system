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
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/dxd_rating
```

## ローカル起動手順
```bash
cp .env.example .env
docker compose up -d db
uv sync
uv run alembic upgrade head
DEVELOPMENT_MODE=true uv run python -m dxd_rating.apps.bot.main
```

開発モードで Bot を起動する場合:

```bash
DEVELOPMENT_MODE=true uv run python -m dxd_rating.apps.bot.main
```

開発モードでは `/admin_setup_ui_channels` が作成する UI チャンネルをすべて private channel として作成します。

## Cron Job
定期実行処理は `src/dxd_rating/apps/worker/` 配下に置き、Railway の Cron Job からコマンド実行する想定です。
現時点の日次エントリーポイントは次のとおりです。

```bash
uv run python -m dxd_rating.apps.worker.daily
```

このジョブは Bot 本体とは別プロセスで動作し、現在は DB 接続確認と今後の定期処理を追加するための雛形を提供します。
Railway ではこのコマンドを Cron Job に設定し、スケジュール自体は Railway 側で管理してください。
このジョブは現在、DB 接続確認、シーズン保守、ランキング snapshot の日次生成と古い snapshot の削除を行います。

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
