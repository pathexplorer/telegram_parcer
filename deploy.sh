#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Telegram Parser — deploy script with pre-flight test gate.
#
# Usage:
#   ./deploy.sh                     # run tests, then deploy
#   ./deploy.sh --skip-tests        # deploy without running tests
#
# Requires:
#   - pytest installed in .venv/
#   - gcloud authenticated
#   - GCS staging bucket: gs://handy-cache-476919-t4_self_cloudbuild/source
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SKIP_TESTS=false
if [[ "${1:-}" == "--skip-tests" ]]; then
    SKIP_TESTS=true
    shift
fi

# ── Test gate ────────────────────────────────────────────────────────
if [[ "$SKIP_TESTS" == false ]]; then
    echo "═══════════════════════════════════════════════════════════════"
    echo "  🔍 Running test suite before deploy..."
    echo "═══════════════════════════════════════════════════════════════"
    if .venv/bin/python -m pytest tests/ -v; then
        echo ""
        echo "  ✅ All tests passed."
    else
        echo ""
        echo "  ❌ Tests failed — deploy ABORTED."
        echo "  Use --skip-tests to deploy anyway (not recommended)."
        exit 1
    fi
    echo ""
fi

# ── Deploy ───────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  🚀 Submitting Cloud Build..."
echo "═══════════════════════════════════════════════════════════════"

gcloud builds submit \
    --config start.yaml \
    --gcs-source-staging-dir=gs://handy-cache-476919-t4_self_cloudbuild/source
