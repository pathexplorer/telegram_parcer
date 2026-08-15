#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# e2e_test.sh — user-friendly wrapper around scripts/e2e_test.py.
#
# Sends a keyword test message ("This my specialtestphrase E2E<marker>") to
# the test group, forces the deployed poller to run, and waits for the alert
# to arrive in the notification chat.  See scripts/e2e_test.py --help.
#
# Usage:
#   ./scripts/e2e_test.sh                     # defaults (@greenfield9000)
#   ./scripts/e2e_test.sh --chat @greenfield9000 --trigger scheduler
#   ./scripts/e2e_test.sh --dry-run           # send only, no trigger
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -x "${PROJECT_ROOT}/.venv/bin/python3" ]]; then
    PY="${PROJECT_ROOT}/.venv/bin/python3"
else
    PY="python3"
fi

ENV_FILE="${PROJECT_ROOT}/keys.env"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$ENV_FILE"
    set +a
else
    echo "ℹ️  keys.env not found — using environment variables already set." >&2
fi

exec "$PY" -m scripts.e2e_test "$@"
