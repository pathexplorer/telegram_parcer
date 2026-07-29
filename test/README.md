# Test Playground — Chat Accessibility Diagnostics

## Purpose

When a Telegram chat moves from **public → private**, the main listener may lose the
ability to parse messages.  The `diagnose_chat.py` tool isolates each resolution
step (username → numeric ID → dialog cache → message fetch) and reports exactly
what succeeds and what fails.

## Quick Start

```bash
# By username (including @ prefix)
uv run python test/diagnose_chat.py @mobilization_law

# By numeric ID
uv run python test/diagnose_chat.py 1511100059

# Via environment variable
TEST_CHAT_REF=1511100059 uv run python test/diagnose_chat.py

# Or, after `uv sync`, use the console entry point:
uv run diagnose 1511100059
```

## What It Checks

| Step | Description |
|------|-------------|
| A. Session | Is the Telethon session still valid? |
| B. Dialog Cache | How many dialogs does the account have? |
| C. Username Resolution | Can `@handle` be resolved via `get_entity`? |
| D. Numeric-ID Resolution | Is the chat in the dialog cache? Is the `access_hash` valid? |
| E. Message Fetch | Can we read the last 5 messages? |

## Interpreting Results

- **All green** → chat is reachable; listener failures are transient (flood wait, network).
- **C fails, D succeeds** → chat went private but is still accessible by numeric ID
  (listener's fallback path should work).
- **D fails (not in cache)** → user account is no longer a member, or the chat ID is wrong.
- **D fails (get_input_entity error)** → session may lack a valid `access_hash`;
  re-run `telegram/local/get_session.py` to refresh the session.
- **E fails after D succeeds** → rare; may indicate chat deletion or Telegram API changes.
