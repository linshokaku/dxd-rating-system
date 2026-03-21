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
  bot/
    commands/
    services/
    runtime/
    db/
    models/
    config.py
    main.py
tests/
alembic/
README.md
AGENTS.md
```

## 必須環境変数
- `DISCORD_BOT_TOKEN`
- `DATABASE_URL`
- `SUPER_ADMIN_USER_IDS` (任意。カンマ区切りの Discord user ID。例: `123456789012345678,234567890123456789`)
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
uv run python -m bot.main
```

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
