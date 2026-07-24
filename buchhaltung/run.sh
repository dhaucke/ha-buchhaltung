#!/usr/bin/env bash
set -euo pipefail

export HD_DATA_DIR="${HD_DATA_DIR:-/data}"
export HD_HOST="${HD_HOST:-0.0.0.0}"
export HD_PORT="${HD_PORT:-8099}"

mkdir -p "${HD_DATA_DIR}/documents" "${HD_DATA_DIR}/archive"
exec python3 /app/web.py

