# dxd-rating-system

## 技術スタック
- Python 3.12+
- discord.py
- SQLAlchemy
- Alembic
- PostgreSQL (psycopg)
- pydantic-settings
- uv

## ディレクトリ構成
```text
src/
  bot/
    commands/
    services/
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

## ローカル DB 起動
ローカル開発用の PostgreSQL は `docker compose` で起動できます。

```bash
docker compose up -d db
```

起動確認:

```bash
docker compose ps
```

停止:

```bash
docker compose down
```

データも削除して作り直す場合:

```bash
docker compose down -v
```

`docker-compose.yml` の DB 設定は `.env.example` の `DATABASE_URL` と対応しています。

```env
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/dxd_rating
```

## ローカル起動手順
1. 環境変数ファイルを作成
```bash
cp .env.example .env
```

2. ローカル DB 起動
```bash
docker compose up -d db
```

3. 依存インストール
```bash
uv sync
```

4. Bot 起動
```bash
uv run python -m bot.main
```

## コード品質チェック
```bash
uv run ruff format .
uv run ruff check .
uv run mypy
```
