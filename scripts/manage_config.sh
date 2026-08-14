#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# manage_config.sh — user-friendly wrapper around scripts/manage_config.py.
#
# Loads local environment (keys.env), picks the right Python interpreter
# (prefers .venv), and runs the Firestore config manager for the Telegram
# parser: add/remove/list chats and keywords, with optional Telegram
# verification of new chats.
#
# Usage:
#   ./scripts/manage_config.sh                 # interactive menu
#   ./scripts/manage_config.sh list
#   ./scripts/manage_config.sh add-chat "@greenfield9000"
#   ./scripts/manage_config.sh add-keywords "urgent, emergency"
#   ./scripts/manage_config.sh remove-chat "@old_channel"
#   ./scripts/manage_config.sh remove-keywords "obsolete"
#   ./scripts/manage_config.sh add-chat "@x" --no-verify --dry-run
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ---- pick Python interpreter ---------------------------------------------
if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY="${PROJECT_ROOT}/.venv/bin/python3"
else
    PY="python3"
fi

# ---- load environment ----------------------------------------------------
ENV_FILE="${PROJECT_ROOT}/keys.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
else
    echo "ℹ️  keys.env not found — using environment variables already set." >&2
fi

# ---- run the config manager ----------------------------------------------
exec "$PY" -m scripts.manage_config "$@"
