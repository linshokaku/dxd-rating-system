#!/usr/bin/env bash

set -euo pipefail

uv sync --extra dev
uv run ruff check --fix .
uv run ruff format .
uv run ruff check .
uv run mypy
