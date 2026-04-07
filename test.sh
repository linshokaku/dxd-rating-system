#!/usr/bin/env bash

set -euo pipefail

cleanup() {
  docker compose down
}

trap cleanup EXIT

docker compose down -v
docker compose up -d db

until [ "$(docker inspect -f '{{.State.Health.Status}}' dxd-rating-db 2>/dev/null)" = "healthy" ]; do
  sleep 1
done

uv sync --extra dev
uv run alembic upgrade head
uv run pytest "$@"
