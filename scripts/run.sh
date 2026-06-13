#!/usr/bin/env bash
# Quick start cho IoT Simulator.
# 1. python -m venv .venv && source .venv/bin/activate
# 2. pip install -r requirements.txt
# 3. cp env.example.txt .env  → sửa IOT_API_KEY
# 4. bash scripts/run.sh

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "[setup] create venv…"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --quiet -r requirements.txt

if [ -f ".env" ]; then
    # shellcheck disable=SC1091
    set -a; source .env; set +a
fi

exec python -m src.main "$@"
