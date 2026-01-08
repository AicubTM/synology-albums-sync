#!/bin/sh
# run_mounts_on_boot.sh - minimal boot-only wrapper for DSM Task Scheduler
# This script accepts no arguments and always runs `python main.py --mount`.

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR" || exit 1

# Load environment from .env if present
if [ -f .env ]; then
  set -a
  . .env
  set +a
fi

# Allow overriding the Python interpreter path via environment
PYTHON="${PYTHON:-/usr/local/bin/python3}"
LOGDIR="${LOGDIR:-/var/log/synology-albums-sync}"
mkdir -p "$LOGDIR"

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
"$PYTHON" main.py --mount >> "$LOGDIR/boot-sync-${TIMESTAMP}.log" 2>&1
