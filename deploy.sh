#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Telegram Parser — deploy script with pre-flight test gate and
# post-deploy smoke test.
#
# Usage:
#   ./deploy.sh                     # run tests, deploy, smoke test
#   ./deploy.sh --skip-tests        # deploy without running tests
#   ./deploy.sh --skip-smoke        # skip post-deploy smoke test
#
# Requires:
#   - pytest installed in .venv/
#   - gcloud authenticated
#   - GCS staging bucket: gs://handy-cache-476919-t4_self_cloudbuild/source
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Parse flags ──────────────────────────────────────────────────────
SKIP_TESTS=false
SKIP_SMOKE=false
for arg in "$@"; do
    case "$arg" in
        --skip-tests) SKIP_TESTS=true ;;
        --skip-smoke) SKIP_SMOKE=true ;;
    esac
done

# ── Read build vars from start.yaml ───────────────────────────────────
# Extract substitution values without needing yq/jq.
_FUNCTION_NAME=$(grep '_FUNCTION_NAME:' start.yaml | head -1 | awk '{print $2}')
_REGION=$(grep '_REGION:' start.yaml | head -1 | awk '{print $2}')
_GCP_PROJECT_ID=$(grep '_GCP_PROJECT_ID:' start.yaml | head -1 | awk '{print $2}' | tr -d '"')

FUNCTION_URL="https://${_REGION}-${_GCP_PROJECT_ID}.cloudfunctions.net/${_FUNCTION_NAME}"

echo "Function: $_FUNCTION_NAME"
echo "Region:   $_REGION"
echo "Project:  $_GCP_PROJECT_ID"
echo ""

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

# ── Post-deploy smoke test ────────────────────────────────────────────
if [[ "$SKIP_SMOKE" == false ]]; then
    echo ""
    echo "═══════════════════════════════════════════════════════════════"
    echo "  💨 Post-deploy smoke test — validating config loads..."
    echo "  URL: ${FUNCTION_URL}?check=1"
    echo "═══════════════════════════════════════════════════════════════"

    # Get an identity token for the function's audience URL.
    IDENTITY_TOKEN=$(gcloud auth print-identity-token \
        --audiences="$FUNCTION_URL" 2>/dev/null) || true

    if [[ -z "$IDENTITY_TOKEN" ]]; then
        echo "  ⚠️  Could not obtain identity token (gcloud auth issue?)."
        echo "  Skipping smoke test — invoke manually:"
        echo "    curl -H 'Authorization: Bearer TOKEN' '${FUNCTION_URL}?check=1'"
    else
        # The function may take a few seconds to become ready after deploy.
        # Retry up to 3 times with a short delay.
        for attempt in 1 2 3; do
            HTTP_CODE=$(curl -s -o /tmp/smoke_response.txt -w "%{http_code}" \
                -H "Authorization: Bearer ${IDENTITY_TOKEN}" \
                "${FUNCTION_URL}?check=1" 2>/dev/null || echo "000")

            if [[ "$HTTP_CODE" == "200" ]]; then
                BODY=$(cat /tmp/smoke_response.txt)
                echo "  ✅ Smoke test PASSED (HTTP 200): ${BODY}"
                break
            elif [[ "$attempt" -lt 3 ]]; then
                echo "  ⏳ Attempt ${attempt}: HTTP ${HTTP_CODE} — waiting for function to be ready..."
                sleep 10
            else
                BODY=$(cat /tmp/smoke_response.txt 2>/dev/null || echo "(no body)")
                echo "  ❌ Smoke test FAILED after 3 attempts (last HTTP ${HTTP_CODE})."
                echo "  Response: ${BODY}"
                echo ""
                echo "  📋 Check logs for details:"
                echo "    gcloud functions logs read ${_FUNCTION_NAME} --region=${_REGION} --limit=10"
                exit 1
            fi
        done
    fi
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✅ Deploy complete."
echo "═══════════════════════════════════════════════════════════════"
