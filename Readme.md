# Telegram Parser
![telegram_parcer](cover.webp)

## Overview

This project is a high-performance Telegram monitoring tool designed to listen to specified public channels, search for defined keywords in real-time, and send alerts when matches are found. It utilizes the [Telethon](https://docs.telethon.dev/en/stable/) library for interacting with the Telegram API.

Key features:

- **Keyword Monitoring**: Scans messages for specific keywords.

- **Firestore Message Archive**: Saves the **full, uncropped** text of every keyword-matched message to Firestore (`matched_messages` collection). Telegram alerts still show a 300-character excerpt with a deep link; Firestore holds the complete message for search and audit.

- **State Management**: Tracks the last checked message ID for each channel in **Google Cloud Firestore**, ensuring no messages are missed or processed twice (idempotency). Cursor is saved after **each chat** (not just at the end) to minimize data loss on interruption.

- **Dynamic Configuration**: Channel lists and keywords are managed in Firestore, allowing updates without redeploying the code.

- **Secure**: Credentials and secrets are managed via **Google Secret Manager**.

- **Graceful Shutdown**: Handles `SIGINT` (Ctrl+C) and `SIGTERM` (Cloud Run timeout) — saves cursor state before exiting. Supports an optional time limit (`MAX_POLL_SECONDS`) to avoid Telegram flood blocks on long runs.

## Architecture

- **Language**: Python 3.12
- **Core Library**: `Telethon` (Async Telegram client)
- **Testing**: `pytest` (81 tests across 5 test modules) with coverage tracking
- **Infrastructure**:
  - **Google Cloud Firestore**: Stores configuration (`keywords`, `chats`) and state (`cursor_base`).
  - **Google Secret Manager**: Securely stores API credentials.
  - **Google Cloud Run / Functions**: Intended deployment environment.

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
6.  **Archiving**: If a keyword match is found, the **full**, uncropped message is saved to the `matched_messages` Firestore collection before the alert is sent. The save is independent — a Firestore write failure does **not** block the Telegram alert.
7.  **Alerting**: Sends a 300-character excerpt alert to the `NOTIFICATION_CHAT` with a deep link to the original message.
8.  **State Update**: Updates Firestore with the new "last checked ID" **after each chat** (incremental persistence). On shutdown (signal, timeout, or flood-wait), the cursor is saved immediately so the next run resumes from the last safely-acked position.

## Setup & Installation

### Prerequisites
- Python 3.12.
- A Google Cloud Project with Billing enabled.
- **Firestore** (Native mode recommended).
- **Secret Manager** API enabled.
- A Telegram account (the bot will use your user session to read channels).

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

Do **not** create `cursor_base` — the application will create it automatically on first run.

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

### 1. Clone and Install
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

### 2. Configuration (Firestore)
Create the following structure in your Firestore database:

| Collection | Document | Field | Type | Description |
|------------|----------|-------|------|-------------|
| `telegram` | `keywords` | `word` | String (CSV) | Comma-separated list of keywords to search for. |
| `telegram` | `chats` | `chats` | String (CSV) | Comma-separated list of channel usernames (e.g., `@channel1, @channel2`). |
| `telegram` | `cursor_base` | *dynamic* | Map | Stores state. Don't create manually; the app will generate it. |
| `matched_messages` | `{chat_id}_{message_id}` | *dynamic* | Map | Stores the **full**, uncropped content of every keyword-matched message. Created automatically — no manual setup needed. |

### 3. Secrets (Secret Manager)
Create a secret in Google Secret Manager (e.g., named `telegram-secrets`). The value should be a JSON string:
```json
{
  "API_ID": "YOUR_API_ID",
  "API_HASH": "YOUR_API_HASH",
  "BOT_TOKEN": "YOUR_BOT_TOKEN",
  "session_string": "YOUR_TELETHON_SESSION_STRING"
}
```
> **How to get a `session_string`**: Follow **[Step E](#e-generate-a-telethon-session-string)** in the First-Time Bot Setup section above. The included script `telegram/local/get_session.py` handles the entire process.

### 4. Local Environment Variables
Create a `keys.env` file in the project root for local development:
```env
PROJECT_ID=your-gcp-project-id
TELEGRAM_SECRETS=telegram-secrets
NOTIFICATION_CHAT=-1001234567890
# Optional: max seconds per poll run (avoids Telegram flood blocks)
MAX_POLL_SECONDS=120
```
*replace `telegram-secrets` with the actual name of your secret in GCP.*

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
```bash
source .venv/bin/activate
export $(grep -v '^#' keys.env | xargs)  # Load env vars
unset GOOGLE_APPLICATION_CREDENTIALS     # Use personal gcloud creds instead of SA key
python main.py
```

### Cloud Deployment
The project is ready for Google Cloud.
- **Entry Point**: `main`
- **Runtime**: Python 3.12
- Ensure the Service Account used has permissions for **Firestore User** and **Secret Manager Secret Accessor**.

### Graceful Shutdown & Time-Limited Polling

The poller supports **safe interruption** — whether you press Ctrl+C locally or Cloud Run sends `SIGTERM`, the cursor is saved to Firestore before the process exits. The next run resumes from the last saved position with **no duplicate alerts** and **no lost progress**.

#### How it works

- **Ctrl+C (SIGINT)** or **Cloud Run timeout (SIGTERM)** → sets an internal shutdown flag.
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

When the time limit is reached, the current message loop finishes its iteration, the cursor is saved, and the process exits cleanly. On Cloud Functions/Cloud Run, set the env var in your deployment:

```yaml
--set-env-vars=...,MAX_POLL_SECONDS=120
```

> **Tip**: If Telegram returns a `FloodWaitError` (wait > 60 s), the poller **saves the cursor immediately and exits** rather than waiting and risking a Cloud Function timeout.

## Directory Structure
```
telegram_parcer/
├── main.py                  # Entry point — initializes config and runs the poller
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
