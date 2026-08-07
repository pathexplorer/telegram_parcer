# Telegram Parser
![telegram_parcer](cover.webp)

## Overview

This project is a high-performance Telegram monitoring tool designed to listen to specified public channels, search for defined keywords in real-time, and send alerts when matches are found. It utilizes the [Telethon](https://docs.telethon.dev/en/stable/) library for interacting with the Telegram API.

Key features:

- **Keyword Monitoring**: Scans messages for specific keywords.

- **Firestore Message Archive**: Saves every keyword-matched message to Firestore (`matched_messages` collection), with automatic size truncation at ~900 KB to stay within Firestore document limits. Telegram alerts show a 300-character excerpt with a deep link; Firestore holds the size-bounded message (not guaranteed full fidelity beyond ~900 KB) for search and audit.

- **State Management**: Tracks the last checked message ID for each channel in **Google Cloud Firestore**, providing **at-least-once** processing with idempotent archive keys. Cursor-per-chat checkpointing minimizes both missed messages and duplicate alerts on interruption. Cursor is saved after **each chat** (not just at the end) to minimize data loss on interruption.

- **Dynamic Configuration**: Channel lists and keywords are managed in Firestore, allowing updates without redeploying the code.

- **Secure**: Credentials and secrets are managed via **Google Secret Manager**.

- **Graceful Shutdown**: Handles `SIGINT` (Ctrl+C) and `SIGTERM` (Cloud Functions timeout) — saves cursor state before exiting. Supports an optional time limit (`MAX_POLL_SECONDS`) to avoid Telegram flood blocks on long runs.

- **Health Monitoring**: Writes a heartbeat to Firestore (`telegram/heartbeat`) after every successful poll — an independent health signal that works even when Telegram Bot API is down. Exposes a `?health=1` endpoint for uptime checks and Cloud Monitoring integration.

- **Cloud Logging Alerts**: Structured CRITICAL-level logging on all failure paths (startup, runtime, heartbeat) enables Cloud Logging-based alert policies — you get notified by email if the function fails, even if Telegram itself is unreachable.

## User Story

### The Problem

Many public Telegram channels publish time-sensitive information — announcements, offers,
alerts, market movements — but manually monitoring dozens of channels is impractical.
You need a way to **watch many channels at once** and get **notified instantly** when
a message matches your criteria, without scrolling endlessly or relying on Telegram's
built-in notification system (which notifies on every message, not just relevant ones).

### What This Project Solves

This tool answers a single focused question: *"Did any of my target channels post
something containing my keywords since the last time I checked?"*

- **Define once, run forever**: Set your keywords and channel list in Firestore, and
  the poller continuously scans for matches — no manual intervention needed.
- **Immediate, actionable alerts**: When a keyword hits, you get a Telegram notification
  with a 300-character excerpt and a deep link straight to the original message.
- **Full audit trail**: Every matched message is archived to Firestore, so you can
  search and review historical matches at any time.

### Scalability & Reusability

The project is designed as a **self-contained microservice** — a single Cloud Function
that does one job well. This makes it naturally composable in larger pipelines:

| Pattern | How it fits |
|---------|-------------|
| **Pub/Sub fan-out** | Replace the Telegram alert with a Pub/Sub publish call, and downstream services (data pipelines, ML models, dashboards) can consume matched messages in real time. |
| **Multi-keyword verticals** | Deploy separate instances with different keyword sets for different teams or use cases (e.g., one for security alerts, another for market intelligence) — each instance writes to its own Firestore collection or Pub/Sub topic. |
| **Multi-platform extension** | The `message_store.py` serialization and the Firestore archive layer are platform-agnostic. A Discord or Slack listener could reuse the same matching, archiving, and alerting pipeline by swapping only the listener module. |
| **Sink-agnostic output** | The `send.py` module is the only Bot API dependency. Swap it for a webhook, a BigQuery stream, or a custom HTTP endpoint — the core poll → match → archive loop stays unchanged. |

**Scaling limits to be aware of:**

- **Single-instance by design**: Telethon sessions cannot be shared across concurrent
  instances (see [Single-Instance Constraint](#️-important-single-instance-constraint)).
  For high-throughput channels with hundreds of messages per second, a single Cloud Function
  may become a bottleneck. In that scenario, partition channels across multiple session
  strings (multiple Telegram user accounts) and deploy one function per session.
- **Firestore document size**: Individual matched messages are capped at ~900 KB. If you
  need guaranteed full-fidelity archives for very large messages, consider offloading
  storage to Cloud Storage or BigQuery via the sink-agnostic output pattern above.
- **Polling, not streaming**: The function is invoked on a schedule (e.g., every 10 minutes),
  not continuously. For near-real-time requirements, increase the scheduler frequency and
  lower `MAX_POLL_SECONDS` — but stay mindful of Telegram's rate limits.

## Architecture

- **Language**: Python 3.12
- **Core Library**: `Telethon` (Async Telegram client)
- **Testing**: `pytest` (81 tests across 5 test modules) with coverage tracking
- **Infrastructure**:
  - **Google Cloud Firestore**: Stores configuration (`keywords`, `chats`) and state (`cursor_base`).
  - **Google Secret Manager**: Securely stores API credentials.
  - **Google Cloud Functions (Gen 1)**: Deployment environment.

The project is deployed as a **Google Cloud Function (Gen 1)**, HTTP-triggered, with a 500 s timeout.
It is invoked by **Cloud Scheduler** using an OIDC-authenticated request to the function's
`--trigger-http` endpoint.  The function must **not** be deployed with `--allow-unauthenticated`;
Cloud Scheduler authenticates via the service account bound to the function.

### Data Flow

1.  **Configuration Load**: On startup, the app loads sensitive secrets (`API_ID`, `API_HASH`, `session_string`) from Secret Manager and operational config (target chats, keywords) from Firestore.
2.  **Polling**: It iterates through the target chats.
3.  **Optimization**: It maintains a local mapping of `username -> ID`. If a username changes, it automatically resolves the new ID and updates the database.
4.  **Processing**: It fetches messages newer than the last checked ID.
5.  **Matching**: Checks message content against keywords.
6.  **Archiving**: If a keyword match is found, the full message (size-bounded at ~900 KB for Firestore document limits) is saved to the `matched_messages` Firestore collection before the alert is sent. The save is independent — a Firestore write failure does **not** block the Telegram alert.
7.  **Alerting**: Sends a 300-character excerpt alert to the `NOTIFICATION_CHAT` with a deep link to the original message.
8.  **State Update**: Updates Firestore with the new "last checked ID" **after each chat** (incremental persistence). On shutdown (signal, timeout, or flood-wait), the cursor is saved immediately so the next run resumes from the last safely-acked position.
9.  **Heartbeat**: Writes a success/failure timestamp to `telegram/heartbeat` — an independent health signal for external monitoring.

### Architecture Diagram

```mermaid
flowchart TD
    Scheduler["Cloud Scheduler<br/>(OIDC-authenticated HTTP)"]
    GCF["Cloud Function (Gen 1)<br/>main(request)"]
    SM["Secret Manager<br/>telegram-secrets"]
    FS["Firestore"]
    TGAPI["Telegram Bot API<br/>(aiohttp)"]
    TGUser["Telegram Servers<br/>(Telethon / MTProto)"]
    AlertChat["NOTIFICATION_CHAT<br/>(Telegram group)"]

    Scheduler -->|"HTTP trigger"| GCF
    GCF -->|"1. Load secrets"| SM
    GCF -->|"2. Load config + cursor"| FS
    GCF -->|"3. Poll messages (min_id=cursor)"| TGUser
    GCF -->|"4a. Archive matched msg"| FS
    GCF -->|"4b. Send alert excerpt"| TGAPI
    TGAPI -->|"sendMessage"| AlertChat
    GCF -->|"5. Save cursor per-chat"| FS

    subgraph Failure modes
        direction LR
        F1["Archive write fails → alert still sent"]
        F2["Alert send fails → cursor NOT advanced (retry next run)"]
        F3["Crash after alert, before cursor → duplicate alert possible (at-least-once)"]
    end
```

**Delivery guarantees**: The system provides **at-least-once** alerting. If the process crashes after a successful Bot API delivery but before the Firestore cursor is saved, the same message will be re-fetched and the alert duplicated on the next invocation. Conversely, if an alert fails, the cursor stops at the last successfully-acked message — no message is silently skipped. The archive key (`{chat_id}_{message_id}`) is naturally idempotent; duplicate writes to `matched_messages` are harmless.

### ⚠️ Important: Single-Instance Constraint

**Do not enable autoscaling on the Cloud Function.** Telethon sessions are tied to a single
connection — if a second Cloud Function instance starts while the first is still running
(or if two instances run concurrently), Telegram will invalidate the session string when
it sees the same session connecting from a different IP/endpoint. This causes
`AUTH_KEY_UNREGISTERED` errors and requires regenerating the session.

- Set **maximum instances to 1** in your Cloud Function configuration.
- The Scheduler interval should be **longer than the function's maximum execution time**
  to prevent overlap (e.g., 10-minute schedule with a 9-minute `MAX_POLL_SECONDS` limit).
- The poller itself uses `MAX_POLL_SECONDS` to self-limit and exit before the Cloud
  Functions timeout, further reducing the risk of concurrent invocations.
- **Never run the local version while the Cloud Scheduler is active.** Running both
  simultaneously will cause Telethon to block the session string (same as two cloud
  instances colliding). Always pause the scheduler before local testing, and resume it
  afterwards (see [Local Mode](#local-mode) for the commands).

---

## Monitoring & Alerting

The function provides three independent ways to check its health — none of which
depend on the Telegram Bot API (so you're alerted even if Telegram itself is down).

### 1. Firestore Heartbeat (Automatic)

After every successful poll, the function writes a timestamp to Firestore at
`telegram/heartbeat`.  If the poll fails, a failure entry is written instead.

| Field | Description |
|-------|-------------|
| `last_success_ts` | Unix timestamp of last successful poll |
| `last_success_date` | ISO 8601 date of last successful poll |
| `last_failure_ts` | Unix timestamp of last failure (only present on errors) |
| `last_failure_detail` | Error description (only present on errors) |
| `code_version` | Deployed code version tag |
| `function_name` | Cloud Function name |

Check it in the Firestore Console → `telegram/heartbeat` document.

### 2. Health Endpoint

The function exposes query parameters for health checks:

```bash
# Quick config validation (secrets + Firestore only, no polling)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://REGION-PROJECT.cloudfunctions.net/telegramPoller?check=1"

# Full health check — also verifies heartbeat is recent
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://REGION-PROJECT.cloudfunctions.net/telegramPoller?health=1"
```

| Endpoint | Returns | What it checks |
|----------|---------|----------------|
| `?check=1` | 200 / 500 | Secrets + Firestore config load correctly |
| `?health=1` | 200 / 500 | Above + last success heartbeat is within `HEARTBEAT_MAX_AGE_SECONDS` (default 7200 s) |

Use `?health=1` with Cloud Monitoring Uptime Checks or any external monitoring service
(e.g. healthchecks.io, Better Uptime, Pingdom).

### 3. Cloud Logging Alert (Email/SMS)

All failure paths log structured CRITICAL messages with identifiable prefixes:

| Prefix | Meaning |
|--------|---------|
| `STARTUP_FAILURE` | Secret Manager, import, or Firestore config failure |
| `RUNTIME_FAILURE` | Unhandled exception in the polling loop |
| `HEALTH_CHECK_FAILED` | Heartbeat age exceeds threshold |

**Set up an alert** that emails you when these appear:

```bash
# 1. Create an email notification channel
gcloud beta monitoring channels create \
  --display-name="TelegramPoller Critical Alerts" \
  --type=email \
  --channel-labels=email_address=YOUR_EMAIL@gmail.com

# 2. Create the alert policy (using the bundled alert_policy.json)
gcloud alpha monitoring policies create \
  --policy-from-file=alert_policy.json \
  --notification-channels=CHANNEL_ID_FROM_STEP_1
```

> The `alert_policy.json` file is included in the repository and pre-configured
> for the `telegramPoller` function.  See [Monitoring Setup](alert_policy.json).

## Setup & Installation

### Prerequisites
- Python 3.12.
- A Google Cloud Project with Billing enabled.
- **Firestore** (Native mode recommended).
- **Secret Manager** API enabled.
- A Telegram account (the bot will use your user session to read channels).

---

### Clone and Install

```bash
git clone <repository-url>
cd telegram_parcer

# Create virtual environment and install dependencies (including dev deps for tests)
uv sync

# Activate the environment
source .venv/bin/activate
```

> **Dependency management**: This project uses [uv](https://docs.astral.sh/uv/) with
> `pyproject.toml`. The private package `gcp-actions` is sourced from a local path
> (`../gcp_actions`). For Cloud Build deployments, `requirements.txt` is kept in
> sync as a fallback.
>
> If you need to install without `uv`, use:
> ```bash
> unset GOOGLE_APPLICATION_CREDENTIALS
> TOKEN=$(gcloud auth print-access-token)
> uv pip install -r requirements.txt \
>   --index-url "https://oauth2accesstoken:$TOKEN@us-central1-python.pkg.dev/$PROJECT_ID/bike-data-magic/simple/" \
>   --extra-index-url https://pypi.org/simple
> ```

---

### First-Time Bot Setup (Step by Step)

Follow these steps in order to set up the project from scratch.

#### A. Google Cloud Project Setup

1. Create a new project in the [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the **Firestore** API → create a **Firestore database** in **Native mode**.
3. Enable the **Secret Manager** API.
4. Create a **dedicated Service Account** (not your personal email):
   - Go to **IAM & Admin → Service Accounts** → **Create Service Account**
   - Give it a meaningful name, e.g. `telegram-parser-sa`
   - Assign these roles:
     - **Firestore User**
     - **Secret Manager Secret Accessor**
   - Click **Done**
   > Do **not** use your personal Google account (owner email) or the default App Engine SA (`{project_id}@appspot.gserviceaccount.com`) — create a dedicated SA with minimal permissions.
5. **Authenticate locally using Application Default Credentials** (recommended):
   ```bash
   gcloud auth application-default login
   ```
   This stores short-lived credentials at `~/.config/gcloud/application_default_credentials.json`.
   The Google client libraries discover them automatically — no JSON key file needed.

   > ⚠️ **Avoid downloading service-account JSON keys for local development.**
   > Long-lived keys are a security risk.  Use ADC or workload identity federation instead.
   > If you absolutely must use a key (e.g. an air-gapped environment), download it from
   > **IAM → Service Accounts → Keys → Add Key → JSON**, store it securely, and
   > rotate it regularly.  Set `GOOGLE_APPLICATION_CREDENTIALS` to the key path.

#### B. Firestore — Create Configuration Documents

In the **Firestore Data** view, create the following documents inside a `telegram` collection:

| Document ID | Field | Type | Value (example) |
|-------------|-------|------|-----------------|
| `keywords` | `word` | String | `urgent, alert, critical` |
| `chats` | `chats` | String | `@channel1, @channel2` |

Do **not** create `cursor_base` or `matched_messages` — the application creates both automatically on first run.

#### C. Get Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and log in with your Telegram account.
2. Go to **API Development**.
3. Create a new application if you don't have one — you'll receive an **API ID** and **API Hash**.
   > These are **not** the bot token — they belong to your **user account**.

#### D. Create a Telegram Bot (for Alert Notifications)

1. Open Telegram and search for [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts.
3. Once created, BotFather will give you a **bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`).
4. Create a **group chat**, add your bot to it as an **administrator**, and send a message in the group.
5. Get the **chat ID** of this group:
   - Visit `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
   - Look for the `chat.id` value — this is your `NOTIFICATION_CHAT`.

#### E. Generate a Telethon Session String

The project uses Telethon's `StringSession` — a portable string that stores your Telegram authorization. **This is tied to your user Telegram account, not the bot.**

> ⚠️ **CRITICAL: Use your phone number, NOT a bot token!**
> When the script prompts `Please enter your phone (or bot token):`, you MUST enter your **phone number** (e.g., `+1234567890`).
> A bot token will produce a session that **cannot** call `GetHistoryRequest` or `GetDialogsRequest` — the app will fail with `"The API access for bot users is restricted"`.
> Bots are only used for **sending alert notifications** (step D); everything else (reading channel history, iterating dialogs) requires a **user** session.

Run the included session generator:

```bash
source .venv/bin/activate

# The script reads API_ID and API_HASH from GCP Secret Manager,
# so your GCP credentials must be set up:
unset GOOGLE_APPLICATION_CREDENTIALS   # Use your personal gcloud auth
export PROJECT_ID=your-gcp-project-id
export NOTIFICATION_CHAT=-1001234567890   # The group where alerts go

python telegram/local/get_session.py
```

> **Which credentials?** Use your **personal gcloud account** (the one that has Secret Manager read access on the project). When `GOOGLE_APPLICATION_CREDENTIALS` is unset, Google libraries automatically fall back to your credentials at `~/.config/gcloud/application_default_credentials.json` (from `gcloud auth application-default login`).

The script will:
1. Prompt you to log in (enter your **phone number** and the **verification code** sent to Telegram).
2. If you have two-factor authentication enabled, enter your **password**.
3. Cache all your dialogs.
4. Print a very long **session string** — **copy it immediately**.

> **Important**: This session string represents your Telegram user session. If you ever revoke it via Telegram Settings → Active Sessions, you'll need to regenerate it (see [Troubleshooting](#troubleshooting--faq)).

#### F. Store Secrets in Google Secret Manager

In the **Google Cloud Console → Secret Manager**, create a new secret (e.g. named `telegram-secrets`) with the following JSON value:

```json
{
  "API_ID": 123456,
  "API_HASH": "your_api_hash_here",
  "BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
  "session_string": "the_very_long_session_string_from_step_E"
}
```

> **Why a single monolithic secret?** Google Cloud Secret Manager's free tier includes
> only **6 active secret versions** per account. Since other projects in this GCP project
> already use 5 of those slots, all Telegram credentials are bundled into one JSON secret
> to stay within the free tier limit. If you have spare secret quota, feel free to split
> each credential into its own secret — just update `project_env/config.py` accordingly.

Make sure the Service Account from step A has the **Secret Manager Secret Accessor** role on this secret.

#### G. Prepare Local Environment

Copy the example file and fill in your values:

```bash
cp keys.env.example keys.env
# Edit keys.env with your project ID, secret name, and notification chat ID
```

The `keys.env` file should contain:

```env
PROJECT_ID=your-gcp-project-id
TELEGRAM_SECRETS=telegram-secrets
NOTIFICATION_CHAT=-1001234567890
# GOOGLE_APPLICATION_CREDENTIALS is NOT needed — ADC is used by default.
# Only set it if you are using a service-account JSON key (not recommended).
# GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
```

> The `TELEGRAM_SECRETS` value must match the name of the secret you created in step F.

#### H. Run the Project Locally

```bash
source .venv/bin/activate
export $(grep -v '^#' keys.env | xargs)  # Load env vars from keys.env
python main.py
```

You should see log output showing that the bot is scanning channels for keywords.

---

## Testing

The project includes a comprehensive test suite (**81 tests**) built with `pytest`.
All GCP dependencies (Secret Manager, Firestore, Telethon, Cloud Logging) are mocked
so tests run **offline** — no credentials or network access required.

### Running Tests

```bash
# Activate the environment
source .venv/bin/activate

# Run the full test suite
pytest

# Run only unit tests (skip integration / slow tests)
pytest -m "unit"

# Run with coverage report
pytest --cov --cov-report=term-missing

# Run a specific test file
pytest tests/test_listener.py -v

# Run a specific test class or method
pytest tests/test_starter_conf.py::TestCursorValidation -v
```

### Test Structure

| File | Tests | What's Covered |
|------|-------|---------------|
| `test_gcf_deploy.py` | 10 | Full GCF invocation lifecycle, secrets injection, error paths |
| `test_listener.py` | 18 | `_should_stop` signal/timeout, `_safe_title` entity extraction, `_save_cursor_sync` persistence, `poll_telegram` early-return & shutdown paths |
| `test_message_store.py` | 20 | `_strip_nulls`, `_extract_tl_value` type conversion, `_tlobject_to_dict` serialization, `_serialize_message` truncation |
| `test_send.py` | 8 | Bot API HTTP delivery, keyword alert formatting (username/title/ID fallbacks), health alert emoji selection |
| `test_starter_conf.py` | 20 | Firestore config loading (keywords/chats/cursors), cursor validation (malformed, negative, large), legacy alert migration |

### Test Markers

| Marker | Purpose |
|--------|--------|
| `unit` | Fast, isolated tests (no I/O) — safe for pre-commit hooks |
| `integration` | Tests that exercise multiple modules or mocked network deps |
| `slow` | Tests with significant runtime — excluded from quick runs |

### Shared Fixtures (`conftest.py`)

The conftest provides reusable fixtures that mock all external dependencies:
- **`mock_all_gcp_deps`** — Patches Secret Manager, Firestore, Telethon `TelegramClient`, and Cloud Logging in one call.
- **`gcp_env`** (autouse) — Injects minimal GCF-style environment variables into every test.
- **Module cache clearing** (autouse) — Ensures each test gets a fresh import of `telegram_parcer` / `telegram` modules, preventing cross-test contamination.

## Running the Project

### Local Mode
The `main.py` detects if it's running locally and executes a test run.

> ⚠️ **Before running locally, pause the Cloud Scheduler!**
> If the cloud function and your local instance both use the same session string
> at the same time, Telethon will invalidate the session. See
> [Single-Instance Constraint](#️-important-single-instance-constraint).

**Recommended — use the convenience script** (`run_local.sh`):
```bash
# The script automatically pauses the scheduler, runs the poller,
# and resumes the scheduler afterwards (even on Ctrl+C or errors).
./run_local.sh

# Extra args are forwarded to main.py:
MAX_POLL_SECONDS=60 ./run_local.sh
```

**Manual approach** (if you prefer to control each step):
```bash
# 1. Pause the Cloud Scheduler job
gcloud scheduler jobs pause telegram-poll-job --location=$REGION

# 2. Run locally
source .venv/bin/activate
export $(grep -v '^#' keys.env | xargs)  # Load env vars
unset GOOGLE_APPLICATION_CREDENTIALS     # Use personal gcloud creds instead of SA key
python main.py

# 3. Resume the Cloud Scheduler job when done
gcloud scheduler jobs resume telegram-poll-job --location=$REGION
```

### Cloud Deployment

The project deploys to **Cloud Functions (Gen 1)** via a YAML-based **Cloud Build** pipeline (`start.yaml`).
It is invoked by **Cloud Scheduler** with OIDC authentication — the function must **not** be publicly accessible.

#### Quick Environment Setup

Source these before running any deployment commands:

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION=us-central1
export SERVICE_ACCOUNT="tele-looker-wizard@${PROJECT_ID}.iam.gserviceaccount.com"
export BUCKET=telemegagram
export AR_REPO=bike-data-magic
```

#### A. Enable Required APIs

```bash
gcloud services enable cloudbuild.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudfunctions.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
gcloud services enable cloudresourcemanager.googleapis.com  # needed for YAML build
```

#### B. Create the Dedicated Service Account

```bash
gcloud iam service-accounts create tele-looker-wizard \
  --display-name="Wonderful action with telemessages"
```

Grant project-level roles:

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role="roles/logging.logWriter"
```

Grant access to the **Secret Manager secret** (resource-level):

```bash
gcloud secrets add-iam-policy-binding telegram-secrets \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/secretmanager.secretAccessor"
```

#### C. Grant Cloud Build Permissions

Cloud Build needs permission to deploy Cloud Functions and act as the service account:

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/cloudfunctions.developer"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/run.admin"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/serviceusage.serviceUsageConsumer"
```

Grant Cloud Build's SA the ability to impersonate your runtime SA:

```bash
gcloud iam service-accounts add-iam-policy-binding $SERVICE_ACCOUNT \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
```

#### D. Deploy

**Recommended — use the convenience script** (`deploy.sh`):

```bash
# Full deploy: tests → build → smoke test → live verification
./deploy.sh

# Skip stages as needed:
./deploy.sh --skip-tests      # deploy without running tests
./deploy.sh --skip-smoke      # skip post-deploy config check
./deploy.sh --skip-verify     # skip heartbeat verification wait
```

The script automatically:
- Detects your **GCP project ID** from `gcloud config`
- Finds the **service account** (`tele-looker-wizard@...`) documented in the README
- Passes all required substitutions to Cloud Build
- After deploy, **waits for the scheduler to fire** and polls logs until the heartbeat confirms the function is alive

You'll see output like:

```
✅ Deploy complete.

🔍 Live verification — waiting for heartbeat confirmation...
   ✅ LIVE VERIFICATION PASSED — function is working!
   💓 Heartbeat written: success at 2026-08-07T07:15:58
```

**Manual deploy** (if you prefer to control each step):

```bash
gcloud builds submit --config start.yaml \
  --substitutions=_GCP_PROJECT_ID=$PROJECT_ID,_SERVICE_ACCOUNT=$SERVICE_ACCOUNT,_ARTIFACT_REPO=$AR_REPO \
  --gcs-source-staging-dir=gs://${PROJECT_ID}_self_cloudbuild/source
```

#### E. Set Up Cloud Scheduler

Create a scheduler job that invokes the function every 10 minutes (during active hours 05:00–21:00 UTC):

```bash
gcloud scheduler jobs create http telegram-poll-job \
  --schedule "*/10 5-21 * * *" \
  --uri "https://${REGION}-${PROJECT_ID}.cloudfunctions.net/telegramPoller" \
  --http-method GET \
  --location $REGION
```

#### F. Secure the Function with OIDC (Required!)

The function **must not** allow unauthenticated access. Configure it so only Cloud Scheduler
(via its service account) can invoke it:

1. **In Cloud Scheduler** — edit the job, change **Auth** from `None` to **OIDC**,
   and set the service account to `$SERVICE_ACCOUNT`.

2. **On the Cloud Function** — go to **Permissions** → **Add Principal**:
   - Principal: `$SERVICE_ACCOUNT`
   - Role: **Cloud Run Invoker** (`roles/run.invoker`)

3. Remove the **allUsers** binding if one exists (it was likely added during initial deploy).

Verify the IAM policy:

```bash
gcloud run services get-iam-policy telegrampoller \
  --region=$REGION \
  --project=$PROJECT_ID
```

#### G. Debugging 403 Errors

If Cloud Scheduler gets a `403 Forbidden`, the OIDC auth is misconfigured. Check:

```bash
# Check current IAM bindings on the Cloud Run service
gcloud run services get-iam-policy telegrampoller \
  --region=$REGION \
  --project=$PROJECT_ID

# Manually grant Cloud Run Invoker to the scheduler's SA if missing
gcloud run services add-iam-policy-binding telegrampoller \
  --member="serviceAccount:$SERVICE_ACCOUNT" \
  --role="roles/run.invoker" \
  --region=$REGION \
  --project=$PROJECT_ID
```

Also verify the scheduler job uses **OIDC auth** (not `None`) and that the service account
email in the scheduler job matches the one granted `roles/run.invoker`.

### Graceful Shutdown & Time-Limited Polling

The poller supports **safe interruption** — whether you press Ctrl+C locally or Cloud Functions sends `SIGTERM`, the cursor is saved to Firestore before the process exits. The next run resumes from the last safely-acked position, minimizing both duplicate alerts and lost progress.

#### How it works

- **Ctrl+C (SIGINT)** or **Cloud Functions timeout (SIGTERM)** → sets an internal shutdown flag.
- The polling loop checks this flag **between chats** and **after each message** inside a chat.
- On shutdown, the cursor for the current chat (and all previous chats) is saved immediately.
- Cursor is already saved **per-chat** during normal operation, so only the current chat's in-flight messages may be re-scanned on restart.

#### Time-limited runs (`MAX_POLL_SECONDS`)

To avoid Telegram's flood-block system during very long polling runs, set a maximum runtime:

```bash
# Run for at most 120 seconds, then save cursor and exit
MAX_POLL_SECONDS=120 python main.py
```

```bash
# Unlimited — runs until all messages are processed (or interrupted)
python main.py
```

When the time limit is reached, the current message loop finishes its iteration, the cursor is saved, and the process exits cleanly. On Cloud Functions, set the env var in your deployment:

```yaml
--set-env-vars=...,MAX_POLL_SECONDS=120
```

> **Tip**: If Telegram returns a `FloodWaitError` (wait > 60 s), the poller **saves the cursor immediately and exits** rather than waiting and risking a Cloud Function timeout.

## Directory Structure
```
telegram_parcer/
├── main.py                  # Entry point — initializes config and runs the poller
├── run_local.sh             # Safe local runner — pauses/resumes Cloud Scheduler automatically
├── deploy.sh                # Deploy script — tests → build → smoke test → live verify
├── start.yaml               # Cloud Build pipeline definition
├── alert_policy.json        # Cloud Monitoring alert policy for CRITICAL errors
├── pyproject.toml           # Project metadata, dependencies, pytest & coverage config
├── requirements.txt         # Pinned deps for Cloud Build (fallback)
├── keys.env                 # Local environment variables (git-ignored)
├── telegram/                # Core logic
│   ├── listener.py          # Main polling loop, message processing, cursor management
│   ├── message_store.py     # Serializes & persists full matched messages to Firestore
│   ├── starter_conf.py      # Loads keywords, chats & cursor state from Firestore
│   ├── send.py              # Bot API alert delivery (keyword alerts & health alerts)
│   └── local/
│       └── get_session.py   # Interactive Telethon StringSession generator
├── project_env/             # Environment & config loaders
│   └── config.py            # Reads secrets from os.environ
├── emergency/               # Operational tools
│   └── reset_cursors.py     # Emergency cursor-reset utility
├── test/                    # Diagnostic / manual test scripts
│   └── diagnose_chat.py     # Chat accessibility diagnostic tool
└── tests/                   # Automated test suite (pytest)
    ├── conftest.py          # Shared fixtures — mocks for GCP, Firestore, Telethon
    ├── test_gcf_deploy.py   # GCF deployment simulation (end-to-end)
    ├── test_listener.py     # Polling loop, cursor management, shutdown logic
    ├── test_message_store.py # Message serialization, truncation, TL object handling
    ├── test_send.py         # Bot API notifications, alert formatting
    └── test_starter_conf.py # Firestore config loading, cursor validation/migration
```

---

## Troubleshooting / FAQ

### Recovering a Deleted Telegram Session

**Problem**: The bot stops working with errors like `AUTH_KEY_UNREGISTERED` or `Could not load configuration`. You may have accidentally revoked the bot's session from Telegram Settings → Privacy and Security → Active Sessions.

**Why this happens**: This project uses Telethon's `StringSession` — a portable string that stores your Telegram user's authorization key. This string is stored in **Google Secret Manager** (as the `session_string` field inside your `telegram-secrets` secret). If you go to **Telegram Settings → Privacy and Security → Active Sessions** and revoke the session named something like "Telethon" or "Telegram Parser", you invalidate that stored session string.

**Resolution** — regenerate the session string:

1. **Re-run the session generator**:
   ```bash
   source .venv/bin/activate

   # The script reads API_ID and API_HASH from GCP Secret Manager.
   # Use your personal gcloud credentials (unset the service account key):
   unset GOOGLE_APPLICATION_CREDENTIALS
   export PROJECT_ID=your-gcp-project-id
   export NOTIFICATION_CHAT=-1001234567890

   python telegram/local/get_session.py
   ```
   > **Which credentials?** Use your **personal gcloud account** (the one that has Secret Manager read access on the project). When `GOOGLE_APPLICATION_CREDENTIALS` is unset, Google libraries automatically fall back to your credentials at `~/.config/gcloud/application_default_credentials.json` (from `gcloud auth application-default login`).
2. **Log in** with your Telegram phone number and the verification code sent to your Telegram app.
3. **Copy** the new session string printed at the end.

4. **Update the secret** in Google Secret Manager:

   **Option A — GCP Console UI:**
   - Go to **Secret Manager** → select your secret (e.g. `telegram-secrets`)
   - Click **Edit** → paste the new JSON with the updated `session_string`
   - Click **Save**

   **Option B — gcloud CLI:**
   ```bash
   # Get the current secret JSON
   gcloud secrets versions access latest --secret="telegram-secrets" \
     --project=your-gcp-project-id > current_secret.json

   # Edit the file — replace the session_string value
   nano current_secret.json

   # Add a new version with the updated value
   gcloud secrets versions add "telegram-secrets" \
     --data-file=current_secret.json \
     --project=your-gcp-project-id
   ```

5. **Restart the application** — it will pick up the new session string on the next run.

> **Tip**: To prevent accidental revocation in the future, you can rename the session in your Telegram app's active sessions list to something recognizable like "Telegram Parser Bot", so you know not to remove it.

### "The API access for bot users is restricted" Error

**Problem**: The app fails with:
```
The API access for bot users is restricted. The method you tried to invoke 
cannot be executed as a bot (caused by GetHistoryRequest)
```

**Why this happens**: Your `session_string` was generated using a **bot token** instead of a **phone number**. Bots cannot call `GetHistoryRequest` or `GetDialogsRequest` — these require a **user** session. This app needs a user session to read channel history; the bot is only used for sending alert notifications.

**Resolution**: Re-run `get_session.py` and enter your **phone number** (e.g., `+1234567890`) when prompted, NOT a bot token. Then update the `session_string` in Secret Manager with the new value (see [Recovering a Deleted Telegram Session](#recovering-a-deleted-telegram-session) above for the full procedure).

---

### Why am I getting the error `AUTH_KEY_UNREGISTERED`?

This error means Telethon tried to use a session key that no longer exists on Telegram's servers. The most common cause is revoking the session from Telegram Settings → Active Sessions. Follow the [session recovery steps](#recovering-a-deleted-telegram-session) above.

---

### How do I update keywords or add new channels without redeploying?

Edit the Firestore documents:
- **Keywords**: Go to Firestore → `telegram/keywords` document → update the `word` field.
- **Channels**: Go to Firestore → `telegram/chats` document → update the `chats` field.

Changes take effect on the next poll cycle — no redeployment needed.

---

### What is the difference between the Bot Token and the Session String?

| Credential | Purpose | Owner |
|-----------|---------|-------|
| `BOT_TOKEN` | Used to send alert notifications via the Bot API (`sendMessage`). | Your Telegram bot (created via @BotFather). |
| `session_string` | Used by Telethon to authenticate as your **user account** to read channels. | Your personal Telegram account. |

The bot token cannot read messages from channels — that's why a user-level Telethon session is required.

---

### How do I know if the function is currently working?

Four independent ways, none of which depend on Telegram being reachable:

```bash
# 1. Health endpoint (fast — checks config + heartbeat age)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://us-central1-PROJECT.cloudfunctions.net/telegramPoller?health=1"

# 2. Read heartbeat directly from Firestore
gcloud firestore documents describe telegram/heartbeat --project=$PROJECT_ID

# 3. Check logs for heartbeat confirmation
gcloud functions logs read telegramPoller --region=us-central1 --limit=5 \
  | grep -E "Heartbeat|FAILURE"

# 4. Run deploy.sh — waits for live heartbeat after deploy
./deploy.sh
```

See [Monitoring & Alerting](#monitoring--alerting) for full details on setting up automated alerts.

---

### What environment variables does the function use?

| Variable | Default | Purpose |
|----------|---------|---------|
| `GCP_PROJECT_ID` | *(required)* | GCP project ID |
| `TELEGRAM_SECRETS` | `telegram-secrets` | Secret Manager secret name |
| `MAX_POLL_SECONDS` | `450` | Max poll duration before self-exit |
| `HEARTBEAT_MAX_AGE_SECONDS` | `7200` | Max heartbeat age for `?health=1` |
| `LOGGING_LEVEL` | `INFO` | Python log level |
| `CODE_VERSION` | *(auto)* | Deployed code version tag |
