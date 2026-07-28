# Telegram Parser
![telegram_parcer](cover.webp)

## Overview

This project is a high-performance Telegram monitoring tool designed to listen to specified public channels, search for defined keywords in real-time, and send alerts when matches are found. It utilizes the [Telethon](https://docs.telethon.dev/en/stable/) library for interacting with the Telegram API.

Key features:

- **Keyword Monitoring**: Scans messages for specific keywords.

- **State Management**: Tracks the last checked message ID for each channel in **Google Cloud Firestore**, ensuring no messages are missed or processed twice (idempotency).

- **Dynamic Configuration**: Channel lists and keywords are managed in Firestore, allowing updates without redeploying the code.

- **Secure**: Credentials and secrets are managed via **Google Secret Manager**.

## Architecture

- **Language**: Python 3.11+
- **Core Library**: `Telethon` (Async Telegram client)
- **Infrastructure**:
  - **Google Cloud Firestore**: Stores configuration (`keywords`, `chats`) and state (`cursor_base`).
  - **Google Secret Manager**: Securely stores API credentials.
  - **Google Cloud Run / Functions**: Intended deployment environment.

### Data Flow

1.  **Configuration Load**: On startup, the app loads sensitive secrets (`API_ID`, `API_HASH`, `session_string`) from Secret Manager and operational config (target chats, keywords) from Firestore.
2.  **Polling**: It iterates through the target chats.
3.  **Optimization**: It maintains a local mapping of `username -> ID`. If a username changes, it automatically resolves the new ID and updates the database.
4.  **Processing**: It fetches messages newer than the last checked ID.
5.  **Matching**: Checks message content against keywords.
6.  **Alerting**: Sends an alert to the `NOTIFICATION_CHAT` if a match is found.
7.  **State Update**: Updates Firestore with the new "last checked ID".

## Setup & Installation

### Prerequisites
- Python 3.11 or higher.
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
5. Generate and download a JSON key:
   - In the Service Account list → click the email of your new SA → **Keys** → **Add Key** → **Create New Key** → **JSON**
   - Rename the downloaded file to something clear, e.g. `telegram-parser-key.json`, and place it in a safe location (e.g. `/home/your-user/keys/`).
   - The `GOOGLE_APPLICATION_CREDENTIALS` env var will point to this file.

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
source .venv1/bin/activate

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

Create a `keys.env` file in the project root:

```env
PROJECT_ID=your-gcp-project-id
GOOGLE_APPLICATION_CREDENTIALS=/path/to/your-service-account-key.json
TELEGRAM_SECRETS=telegram-secrets
NOTIFICATION_CHAT=-1001234567890
```

> The `TELEGRAM_SECRETS` value must match the name of the secret you created in step F.

#### H. Run the Project Locally

```bash
source .venv1/bin/activate
export $(grep -v '^#' keys.env | xargs)  # Load env vars from keys.env
unset GOOGLE_APPLICATION_CREDENTIALS     # Use personal gcloud creds instead of SA key
python main.py
```

You should see log output showing that the bot is scanning channels for keywords.

---

### 1. Clone and Install
```bash
git clone <repository-url>
cd telegram_parcer

# Create virtual environment
uv venv .venv1
source .venv1/bin/activate

# Install dependencies
# NOTE: GOOGLE_APPLICATION_CREDENTIALS must be UNSET because VS Code
# may set it to a service account that lacks Artifact Registry access.
# Use your personal gcloud credentials instead:
unset GOOGLE_APPLICATION_CREDENTIALS
TOKEN=$(gcloud auth print-access-token)
uv pip install -r requirements.txt \
  --index-url "https://oauth2accesstoken:$TOKEN@us-central1-python.pkg.dev/$PROJECT_ID/bike-data-magic/simple/" \
  --extra-index-url https://pypi.org/simple
```
*Note: This project depends on a custom library `gcp_actions` hosted in a private Artifact Registry. Your gcloud account (`gcloud auth list`) must have the `artifactregistry.reader` (or `writer`) role on the `$PROJECT_ID` project.*

### 2. Configuration (Firestore)
Create the following structure in your Firestore database:

| Collection | Document | Field | Type | Description |
|------------|----------|-------|------|-------------|
| `telegram` | `keywords` | `word` | String (CSV) | Comma-separated list of keywords to search for. |
| `telegram` | `chats` | `chats` | String (CSV) | Comma-separated list of channel usernames (e.g., `@channel1, @channel2`). |
| `telegram` | `cursor_base` | *dynamic* | Map | Stores state. Don't create manually; the app will generate it. |

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
```
*replace `telegram-secrets` with the actual name of your secret in GCP.*

## Running the Project

### Local Mode
The `main.py` detects if it's running locally and executes a test run.
```bash
source .venv1/bin/activate
export $(grep -v '^#' keys.env | xargs)  # Load env vars
unset GOOGLE_APPLICATION_CREDENTIALS     # Use personal gcloud creds instead of SA key
python main.py
```

### Cloud Deployment
The project is ready for Google Cloud.
- **Entry Point**: `main`
- **Runtime**: Python 3.11
- Ensure the Service Account used has permissions for **Firestore User** and **Secret Manager Secret Accessor**.

### Emergency Cursor Reset
See **[Emergency Tools](#emergency-tools)** below for the `reset_cursors` utility.

## Directory Structure
- `main.py`: Entry point. Initializes config and runs the poller.
- `telegram/`: Core logic.
  - `listener.py`: Main loop, polling logic, and message processing.
  - `starter_conf.py`: Loads initial configuration from Firestore.
  - `send.py`: Handles sending alerts.
- `project_env/`: Configuration loaders.
- `emergency/`: Emergency tools (cursor reset, diagnostics).
  - `reset_cursors.py`: Fast-forwards all chat cursors to "now".
- `requirements.txt`: Python dependencies.

---

## Emergency Tools

### `reset_cursors` — "Parse from Current Moment"

When you need to skip all backlog and start monitoring **from now**, use the emergency cursor-reset tool. It scans every tracked chat, records the latest message ID in each, shows a diff against the current stored cursor, and — if confirmed — updates Firestore so future polling sees nothing to catch up on.

**Use cases:**
- You added many new channels and don't want to process thousands of old messages.
- Cursors got corrupted (e.g., a wrong placeholder value was propagated).
- You want a clean "start fresh from today" without deleting any data.

**Usage (local):**
```bash
# Dry-run — scan & print results only, NEVER write to Firestore
uv run python -m emergency.reset_cursors --dry-run

# Interactive — asks y/n before updating cursors
uv run python -m emergency.reset_cursors

# Non-interactive — auto-confirm (useful for scripts/CI)
uv run python -m emergency.reset_cursors --yes

# Shortcut
uv run python -m emergency --dry-run
```

**Sample output (dry-run):**
```
================================================================================
  🔍  EMERGENCY CURSOR SCAN RESULTS
  Scanned at: 2026-07-28 21:04:54 UTC
================================================================================
Chat                           ID              Old cursor     Latest  Status
--------------------------------------------------------------------------------
MyChannel                       1234567890            340        568  📩 +228 new
AnotherGroup                    9876543210              0        120  ⛳ NEW  (0 → 120)
AlreadyFresh                    1111111111            445        445  ✅ up-to-date
StaleChat                       2222222222          102974       9127  ⚠️  STALE (cursor ahead by 93847)
--------------------------------------------------------------------------------
  New chats (no cursor): 1
  Behind (will advance):  1
  Already up-to-date:     1
  Errors/skipped:         1
================================================================================
```

**Status legend:**
| Icon | Meaning |
|------|---------|
| 📩 +N new | Chat has new messages since last cursor — will advance. |
| ✅ up-to-date | Cursor already matches latest message. |
| ⛳ NEW | Chat has no cursor yet — first-time tracking. |
| ⚠️ STALE | Stored cursor is **ahead** of the latest message (likely a corrupted/bad seed value). Will be reset to actual latest. |
| ⏳ skipped | Rate-limited by Telegram — retry later. |

---

## Troubleshooting / FAQ

### Recovering a Deleted Telegram Session

**Problem**: The bot stops working with errors like `AUTH_KEY_UNREGISTERED` or `Could not load configuration`. You may have accidentally revoked the bot's session from Telegram Settings → Privacy and Security → Active Sessions.

**Why this happens**: This project uses Telethon's `StringSession` — a portable string that stores your Telegram user's authorization key. This string is stored in **Google Secret Manager** (as the `session_string` field inside your `telegram-secrets` secret). If you go to **Telegram Settings → Privacy and Security → Active Sessions** and revoke the session named something like "Telethon" or "Telegram Parser", you invalidate that stored session string.

**Resolution** — regenerate the session string:

1. **Re-run the session generator**:
   ```bash
   source .venv1/bin/activate

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
