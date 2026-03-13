#!/usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: ./migrate.sh \"describe schema change\"" >&2
  exit 1
fi

cleanup() {
  docker compose down
}

trap cleanup EXIT

docker compose up -d db

until [ "$(docker inspect -f '{{.State.Health.Status}}' dxd-rating-db 2>/dev/null)" = "healthy" ]; do
  sleep 1
done

uv sync --extra dev
uv run alembic upgrade head
uv run alembic revision --autogenerate -m "$1"
uv run alembic upgrade head
