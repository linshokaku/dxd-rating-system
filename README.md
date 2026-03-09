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
- `LOG_LEVEL` (任意。未指定時は `INFO`)

ローカルでは `.env.example` をコピーして `.env` を作成してください。

```bash
cp .env.example .env
```

## ローカル起動手順
1. 依存インストール
```bash
uv sync
```
2. Bot 起動
```bash
uv run python -m bot.main
```

## コード品質チェック
```bash
uv run ruff format .
uv run ruff check .
uv run mypy
```
