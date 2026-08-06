#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_local.sh — Safely run the Telegram parser locally.
#
# Pauses the Cloud Scheduler job before running and resumes it afterwards,
# even if you Ctrl+C or the script fails.  This prevents Telethon from
# blocking the session string when the cloud function and local instance
# collide.
#
# Usage:
#   ./run_local.sh [extra args for main.py]
#
# Examples:
#   ./run_local.sh
#   ./run_local.sh --debug
#   MAX_POLL_SECONDS=60 ./run_local.sh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- load environment ----------------------------------------------------
ENV_FILE="${SCRIPT_DIR}/keys.env"
if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: keys.env not found at $ENV_FILE" >&2
    echo "  cp keys.env.example keys.env  # then edit it with your values" >&2
    exit 1
fi
set -a
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

# ---- resolve required vars -----------------------------------------------
# Support both PROJECT_ID and GCP_PROJECT_ID (legacy naming).
PROJECT="${PROJECT_ID:-${GCP_PROJECT_ID:-}}"
REGION="${REGION:-us-central1}"
JOB_NAME="${SCHEDULER_JOB_NAME:-telegram-poll-job}"

if [[ -z "$PROJECT" ]]; then
    echo "ERROR: Neither PROJECT_ID nor GCP_PROJECT_ID is set in keys.env" >&2
    exit 1
fi

# ---- helpers -------------------------------------------------------------
pause_scheduler() {
    echo "⏸  Pausing Cloud Scheduler job: $JOB_NAME (region: $REGION)..."
    gcloud scheduler jobs pause "$JOB_NAME" \
        --location="$REGION" \
        --project="$PROJECT" \
        --quiet
    echo "✓  Scheduler paused."
}

resume_scheduler() {
    echo ""
    echo "▶  Resuming Cloud Scheduler job: $JOB_NAME (region: $REGION)..."
    gcloud scheduler jobs resume "$JOB_NAME" \
        --location="$REGION" \
        --project="$PROJECT" \
        --quiet
    echo "✓  Scheduler resumed."
}

cleanup() {
    local exit_code=$?
    resume_scheduler
    exit $exit_code
}

# ---- main ----------------------------------------------------------------
echo "======================================="
echo " Telegram Parser — Local Runner"
echo " Project:  $PROJECT"
echo " Region:   $REGION"
echo " Scheduler job: $JOB_NAME"
echo "======================================="
echo ""

# Trap EXIT so we resume the scheduler no matter how this script ends.
trap cleanup EXIT

pause_scheduler

echo ""
echo "🚀 Starting local poller..."
echo "   (press Ctrl+C to stop early — scheduler will still be resumed)"
echo ""

# Run the actual application, forwarding any extra arguments.
exec python main.py "$@"
