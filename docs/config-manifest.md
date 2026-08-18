# Config Manifest — Telegram Parser

Single source of truth for all environment variables, secrets, and runtime
configuration. INSTRUCTION.md keeps structural prose only; every config fact lives here.

## 1. Layers — where configuration lives

| # | Layer | Storage | Loaded at | Typical keys |
|---|-------|---------|-----------|--------------|
| 1 | Bootstrap input | `keys.env` (gitignored) | local scripts (`run_local.sh`, `diagnose_chat.py`, `e2e_test.py`) | `PROJECT_ID`, `TELEGRAM_SECRETS`, `NOTIFICATION_CHAT`, `GOOGLE_APPLICATION_CREDENTIALS` |
| 2 | Secrets | Secret Manager, one JSON secret (name from `TELEGRAM_SECRETS`) | app cold start via `InjectConfig` (`main.py:_inject_secrets`) | `API_ID`, `API_HASH`, `BOT_TOKEN`, `session_string`, `NOTIFICATION_CHAT` |
| 3 | Function env vars | Cloud Function `--set-env-vars` (`start.yaml`) | function boot | `GCP_PROJECT_ID`, `CODE_VERSION`, `TELEGRAM_SECRETS`, `LOGGING_LEVEL`, `MAX_POLL_SECONDS`, `PRIORITY_CHAT_REFS`, `HEARTBEAT_MAX_AGE_SECONDS` |
| 4 | Non-secret runtime config | Firestore | every start (`forming_configuration`) | `telegram/keywords`, `telegram/cursor_base` |
| 5 | Local override (dev only) | `local_config.json` (project root, never commit) | gcp-actions `init_config` | `GCP_PROJECT_ID` |

Precedence in gcp-actions `init_config`: `local_config.json` > Secret Manager > Firestore.
`keys.env` is never read by the Cloud Function — only by local scripts.

## 2. Variable registry

Hard = required, Opt = optional (default), Ref = legacy/read-only alias.

| Variable | Layer | Level | Default | Consumer | Why |
|----------|-------|-------|---------|----------|-----|
| `PROJECT_ID` | 1 | Hard | — | all local scripts, `deploy.sh` | GCP project ID (alias of `GCP_PROJECT_ID`, see quirks) |
| `GCP_PROJECT_ID` | 1, 3 | Hard (env 3) | — | `deploy.sh`, `scripts/*`, `gcp_actions` | canonical name in function env; local alias `PROJECT_ID` |
| `TELEGRAM_SECRETS` | 1, 3 | Hard | `telegram-secrets` | `main.py`, `get_session.py` | Secret Manager secret name holding the Telegram JSON |
| `NOTIFICATION_CHAT` | 1, 2 | Hard | — | `telegram/send.py`, `project_env/config.py` | Chat ID for keyword alerts; integer, negative for groups; lives in env or secret |
| `GOOGLE_APPLICATION_CREDENTIALS` | 1 | Opt | empty (ADC) | gcloud libs | Path to SA JSON key; leave empty and use ADC |
| `API_ID` | 2 | Hard | — | Telethon client | Telegram API ID (from my.telegram.org) |
| `API_HASH` | 2 | Hard | — | Telethon client | Telegram API hash |
| `session_string` | 2 | Hard | — | Telethon `StringSession` | User session (phone login, NOT bot token); lowercase on purpose |
| `BOT_TOKEN` | 2 | Hard* | — | `telegram/send.py` | Bot API token for alerts; *not checked by `_REQUIRED_ENV_VARS` (see quirks) |
| `MAX_POLL_SECONDS` | 1, 3 | Opt | `450` (env 3), unset (local) | `main.py`, `run_local.sh` | Self-exit limit; keep ≥50 s below the 540 s Gen2 limit |
| `LOGGING_LEVEL` | 1, 3 | Opt | `INFO` | logging config | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `CODE_VERSION` | 3 | Opt | `latest` (build tag) | heartbeat payload | Deployed version tag `$_TAG_NAME` |
| `PRIORITY_CHAT_REFS` | 3 | Opt | empty | `telegram/listener.py` | Comma-separated chat refs polled first; e.g. `@greenfield9000` for e2e |
| `HEARTBEAT_MAX_AGE_SECONDS` | 3 | Opt | `7200` | `main.py` `?health=1` | Max heartbeat age; set > 2× scheduler interval |
| `REGION` | 1 | Opt | `us-central1` | `run_local.sh`, scheduler | Deploy region |
| `SCHEDULER_JOB_NAME` | 1 | Opt | `telegram-poll-job` | `run_local.sh` | Scheduler job paused/resumed during local runs |
| `TEST_CHAT_REF` | 1 | Ref | — | `tests/diagnose_chat.py` | Chat ref override for diagnostics |
| `GCS_BUCKET_NAME` | — | Ref | — | `project_env/config.py` only | Read but never consumed — legacy |
| `GCS_CLOUD_PROJECT` | — | Ref | — | `project_env/config.py` only | Read but never consumed — legacy |
| `K_SERVICE` | 3 | Ref | auto | `main.py` | GCF-injected; used to detect cloud vs local |
| `TELEGRAM_API_TOKEN` / `TELEGRAM_HASH` | — | Ref | literal `"telegram_api_id"` / `"telegram_hash"` | `project_env/config.py:20-21` | Hardcoded constants, no consumer — legacy |

## 3. Naming / convention reference

Fixed names, not derived per-environment:

| Resource | Name | Notes |
|----------|------|-------|
| Secret Manager secret | `telegram-secrets` (via `TELEGRAM_SECRETS`) | JSON payload: each key becomes an env var |
| Service account | `tele-looker-wizard@{PROJECT_ID}.iam.gserviceaccount.com` | `deploy.sh` auto-detects it |
| Cloud Function | `telegramPoller` (`_FUNCTION_NAME`) | Gen2, HTTP, `--max-instances=1` |
| Cloud Scheduler job | `telegram-poll-job` | `*/10 5-21 * * *`, OIDC-auth'd, region `${REGION}` |
| Artifact Registry repo | `_ARTIFACT_REPO` (builder substitution) | host `us-central1-python.pkg.dev/{project}/{repo}` |
| Cloud Build staging bucket | `gs://${PROJECT_ID}_self_cloudbuild/source` | `deploy.sh` |
| Function URL | `https://${REGION}-${PROJECT_ID}.cloudfunctions.net/telegramPoller` | |

Firestore documents:

| Document | Purpose |
|----------|---------|
| `telegram/keywords` | `chats` array (target chat refs), `word` array (keywords) — managed via `scripts/manage_config.py` |
| `telegram/cursor_base` | per-chat poll cursors (auto-created) |
| `telegram/heartbeat` | `{timestamp_success, timestamp_fail, code_version…}` written each poll |
| `matched_messages/{chat_id}_{message_id}` | idempotent message archive (~900 KB truncation) |

## 4. Environment setup checklist (new env)

```bash
# 1. Local bootstrap
cp keys.env.example keys.env          # fill PROJECT_ID, TELEGRAM_SECRETS, NOTIFICATION_CHAT
gcloud auth application-default login # ADC; leave GOOGLE_APPLICATION_CREDENTIALS empty

# 2. External accounts (one-time)
#    api_id + api_hash:  https://my.telegram.org → API development tools
#    bot_token:          @BotFather  (bot used ONLY for sending alerts)
#    user session:       python3 telegram/local/get_session.py  (phone login, not bot)

# 3. Secret Manager — flat JSON, every key becomes an env var:
gcloud secrets create "$TELEGRAM_SECRETS" --project="$PROJECT_ID"
#   payload = {"API_ID":…, "API_HASH":…, "BOT_TOKEN":…,
#              "session_string":…, "NOTIFICATION_CHAT":…}

# 4. Provision (see INSTRUCTION.md → Setup & Installation for full commands):
gcloud iam service-accounts create tele-looker-wizard   # + roles: CF invoker/deployer, SA user
gcloud scheduler jobs create http telegram-poll-job \   # step E in INSTRUCTION.md
  --schedule "*/10 5-21 * * *" --uri "https://${REGION}-${PROJECT_ID}.cloudfunctions.net/telegramPoller"

# 5. Seed runtime config (Firestore)
python3 -m scripts.manage_config add-chat "@mychannel"
python3 -m scripts.manage_config add-keywords "urgent, emergency"

# 6. Deploy
./deploy.sh   # reads start.yaml; waits for live heartbeat
```

## 5. Anti-drift verification

```bash
# Secret payload vs registry (all 5 keys present, values non-empty):
gcloud secrets versions access latest --secret="telegram-secrets" --project="$PROJECT_ID" \
  > /tmp/secret-check.json && python3 -c '
import json,sys; d=json.load(open("/tmp/secret-check.json"))
need={"API_ID","API_HASH","BOT_TOKEN","session_string","NOTIFICATION_CHAT"}
print("MISSING:", need-set(d)) if need-set(d) else print("OK: all 5 payload keys present")'

# Function env vars vs start.yaml:
gcloud functions describe telegramPoller --region=${REGION:-us-central1} \
  --project="$PROJECT_ID" --format="value(serviceConfig.environmentVariables)" \
  | python3 -m json.tool

# Firestore config sanity (chats + word arrays non-empty, no legacy "channels" string):
python3 -m scripts.manage_config list

# Heartbeat freshness:
curl -fsS "https://${REGION:-us-central1}-${PROJECT_ID}.cloudfunctions.net/telegramPoller?health=1"
```

## 6. Known quirks

- **Lowercase `session_string`** — only env var in the project with lowercase name; the
  payload key must match exactly.
- **`BOT_TOKEN` missing from `_REQUIRED_ENV_VARS`** (`main.py:52`) — it is imported at
  startup by `telegram/send.py`, so a missing token fails at import time, not with a
  clean "missing env var" message.
- **`PROJECT_ID` vs `GCP_PROJECT_ID`** — local scripts accept both (`PROJECT_ID=${GCP_PROJECT_ID:-}`);
  the function env uses only `GCP_PROJECT_ID`.
- **Secret JSON payload keys become env vars** — flat structure, no nesting
  (`gcp_actions` `init_config` sets `os.environ[key] = str(value)` for every key).
- **`NOTIFICATION_CHAT` must be an int** — `project_env/config.py` raises
  `EnvironmentError` if unset and `ValueError` if non-integer; negative for groups.
- **Secret version limit** — max 6 active versions per secret; rotate by adding a new
  version and disabling old ones (`gcloud secrets versions disable`).
- **Session must be a phone user session** — a bot-token session raises
  "The API access for bot users is restricted"; re-run `get_session.py` with a phone.
- **Legacy constants** — `TELEGRAM_API_TOKEN`/`TELEGRAM_HASH` (`project_env/config.py`)
  and `GCS_BUCKET_NAME`/`GCS_CLOUD_PROJECT` are read but never consumed; the literal
  strings `"telegram_api_id"`/`"telegram_hash"` hint at an old secret payload layout.
- **Firestore `chats` array only** — the legacy `channels` string field is ignored;
  `manage_config.py` is the safe editor.