#!/usr/bin/env bash
# Usage: ./run.sh            (loads .env if present)
set -e
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
