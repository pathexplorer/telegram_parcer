> **⚠️ TRD_reverse — AI reverse-engineering placeholder**
>
> This file is the **future result of an AI analysis** that performs reverse-engineering of the implemented project,
> pretending the project was written from a spec. It formalizes the system *as it exists* (retrospective spec).
> Do not edit manually as source of truth — the real spec is [TRD.md](TRD.md).
> Expected generation: `AI -> codebase -> TRD_reverse.md`. Currently contains the last generated version.

---

# Technical Requirements — Telegram Parser

> **Setup & operations:** see [../README.md](../README.md) for prerequisites,
> first-time setup, deployment, local mode and troubleshooting. This document is
> the formal specification only.

> This document is a **retrospective technical requirement** — it formalizes the
> system that is currently implemented so it can be understood, maintained, and
> evolved with a single source of truth. Every requirement below is grounded in
> the existing code (v1.0.0) and runtime behaviour; it does not invent features
> that do not exist.

| Field | Value |
|-------|-------|
| Product | Telegram Keyword Monitoring & Alerting Service |
| Code name | `telegram-parcer` |
| Version | 1.0.0 |
| Language / runtime | Python 3.12, async (`asyncio`) |
| Deployment target | Google Cloud Functions (Gen 2, HTTP-triggered) |
| Primary framework | Telethon 1.x (MTProto), Flask (GCF wrapper), aiohttp |
| Status | Implemented (retrospective spec) |

---

## 1. Purpose and Goals

The service monitors a configurable set of public Telegram channels/groups,
scans newly posted messages for a configurable set of keywords in near-real time,
and notifies a dedicated Telegram group when a keyword match is found. It is
invoked **on a schedule**, not continuously, and is built to survive a poll
cycle being interrupted at any point.

**Primary goal:** answer the question *"Did any of my target channels post
something containing my keywords since the last time I checked?"* and, when the
answer is yes, alert the operator immediately with a link to the original message.

**Supporting goals:**

- **G1 — Dynamic, code-free configuration.** Channel list, keywords, and polling
  state live in Firestore so they can change without redeploying.
- **G2 — At-least-once delivery.** No matching message is silently skipped; alert
  duplication is acceptable and preferred to message loss.
- **G3 — Crash resilience.** Interruptions (signal, timeout, flood-wait) must not
  corrupt state or lose more than one in-flight chat's progress.
- **G4 — Independent observability.** The service must be monitorable even when
  Telegram itself is unreachable (heartbeat + health endpoint + structured logs).
- **G5 — Secret isolation.** All credentials live in Secret Manager, never in code
  or repo.

---

## 2. Scope

### 2.1 In scope

- Keyword monitoring of public Telegram channels/groups (user-account access via
  Telethon).
- Matching that is case-insensitive and Unicode-normalized (NFKC + casefold).
- Telegram Bot API alert delivery to a notification chat.
- Full-message archiving to Firestore (`matched_messages`).
- Per-chat polling cursors persisted to Firestore (`cursor_base`).
- Health monitoring: Firestore heartbeat, `?check=1` / `?health=1` endpoints,
  structured CRITICAL logging, Cloud Monitoring alert policy.
- Operator tooling: config management CLI, e2e smoke test, emergency cursor reset,
  chat diagnostic, session generator, deploy/run scripts.

### 2.2 Out of scope (current build)

- Guaranteed exactly-once alert delivery (only at-least-once is provided).
- Full-fidelity archiving of messages larger than ~900 KB (size-bounded only).
- Streaming / real-time push; operation is poll-based.
- Multi-instance / autoscaling (explicitly prohibited, see §11.4).
- Non-Telegram sinks (Discord/Slack/Webhook) — the design allows them, but they
  are not implemented.

---

## 3. Glossary

| Term | Meaning |
|------|---------|
| Chat / channel / group | Any Telegram entity being monitored (has a `title`; NOT a private User). |
| Chat ref | Reference to a chat — either `@username` or a numeric ID. |
| Cursor | `last_processed_id` — the highest message ID already acked for a chat. |
| Match | A message whose normalized text contains at least one normalized keyword. |
| Alert | A Telegram message sent to `NOTIFICATION_CHAT` about a match or an operational event. |
| Archive | The Firestore document `matched_messages/{chat_id}_{message_id}` holding the full matched message. |
| Heartbeat | Firestore doc `telegram/heartbeat` recording last poll success/failure. |
| Poll cycle | One full invocation of `main(request)` / `poll_telegram(...)`. |

---

## 4. System Context & Architecture

```
Cloud Scheduler ──OIDC HTTP──▶ Cloud Function (Gen 2) main(request)
                                        │
              ┌─────────────┬────────────┼─────────────┬─────────────┐
              ▼             ▼            ▼             ▼             ▼
        Secret Manager  Firestore    Telegram MTProto  Bot API      Cloud Logging
        (telegram-secrets) (config,   (Telethon client, (sendMessage) (CRITICAL logs
                           cursor,     GetHistoryRequest)             → alert policy)
                           archive,
                           heartbeat)
```

### 4.1 Component/module map

| Module | Responsibility |
|--------|----------------|
| `main.py` | GCF entry point; secret injection, config load, heartbeat, health/check endpoints, polls budget, invokes poller. |
| `telegram/listener.py` | Polling loop: chat resolution, message fetch, keyword match, archive + alert, per-chat cursor persistence, shutdown handling. |
| `telegram/starter_conf.py` | Loads keywords/chats/cursors from Firestore; validates + migrates cursor schemas; normalizes keywords. |
| `telegram/message_store.py` | Serializes full Telethon messages to Firestore `matched_messages` with size truncation. |
| `telegram/send.py` | Bot API delivery with retries, Markdown escaping, alert formatting. |
| `project_env/config.py` | Reads config/credentials from environment (imported by modules). |
| `telegram/local/get_session.py` | Interactive Telethon `StringSession` generator. |
| `scripts/manage_config.py` (+ `.sh`) | CLI to manage chats/keywords/cursors in Firestore. |
| `scripts/e2e_test.py` (+ `.sh`) | End-to-end live pipeline smoke test. |
| `emergency/reset_cursors.py` | Emergency cursor reset utility. |
| `tests/diagnose_chat.py` | Chat accessibility diagnostic tool. |
| `deploy.sh`, `start.yaml`, `run_local.sh` | Deployment pipeline and safe local runner. |
| `alert_policy.json` | Cloud Monitoring alert policy for CRITICAL logs. |

---

## 5. Functional Requirements

Requirements use IDs (`FR-x`). Each is satisfied by the current implementation.

### 5.1 Configuration (FR-CONF)

- **FR-CONF-1** Keywords and chat list MUST be read from Firestore document
  `telegram/keywords` (fields `word`, `chats`) on every configuration load.
- **FR-CONF-2** Configuration MUST be cacheable in-process for a configurable TTL
  (default 60 s) to reduce Firestore reads on warm instances.
- **FR-CONF-3** The `chats` **array** field is authoritative; the legacy
  `channels` string field is ignored.
- **FR-CONF-4** An empty keyword list or empty chat list MUST be treated as a
  fatal startup condition (raise `RuntimeError`), since matching/polling cannot
  proceed.
- **FR-CONF-5** Keywords MUST be normalized with NFKC normalization + `casefold()`
  and deduplicated before matching.

### 5.2 Polling (FR-POLL)

- **FR-POLL-1** For each monitored chat, the service MUST fetch only messages
  newer than that chat's cursor (`min_id=last_processed_id`).
- **FR-POLL-2** A new chat (not yet in `cursor_base`) MUST be registered with its
  cursor seeded to the **latest** message ID, so historical messages are NOT
  alerted on by default.
- **FR-POLL-3** Chats MUST be polled in deterministic order: priority chats first
  (`PRIORITY_CHAT_REFS`), then remaining chats by numeric ID ascending.
- **FR-POLL-4** Chat references MUST resolve by `@username` first, falling back to
  a numeric-ID dialog-cache lookup when the username fails (group went private /
  handle reassigned).
- **FR-POLL-5** If a numeric-ID lookup fails, the service MUST send a one-shot
  "Lost access to chat" health alert (deduplicated per chat via `alerted_keys`).
- **FR-POLL-6** If a username is lost but the numeric ID still resolves, the
  service MUST switch the stored ref to the numeric ID and send a one-shot
  "username lost" warning.
- **FR-POLL-7** A chat ref that resolves to a private **User** (not a
  channel/group) MUST be rejected/skipped with a warning (no `title` attribute).

### 5.3 Matching (FR-MATCH)

- **FR-MATCH-1** A message matches if its text contains at least one configured
  keyword after both sides are NFKC-normalized + casefolded.
- **FR-MATCH-2** Non-text messages (no `message.text`) MUST NOT match and MUST NOT
  block cursor advancement.
- **FR-MATCH-3** Matching is substring-based (keyword present anywhere in text),
  not word-boundary or regex-based.

### 5.4 Archiving (FR-ARCH)

- **FR-ARCH-1** Every matched message MUST be saved to Firestore
  `matched_messages/{chat_id}_{message_id}` BEFORE the alert is attempted.
- **FR-ARCH-2** The archive key MUST be idempotent (re-saving the same message is
  harmless).
- **FR-ARCH-3** A Firestore archive failure MUST NOT block the Telegram alert
  (archive is best-effort relative to alerting).
- **FR-ARCH-4** Archive documents MUST serialize only a defined keep-set of TL
  fields plus pipeline metadata (`_meta`: chat_id, chat_identifier,
  matched_keywords), stripping `None`/empty values.
- **FR-ARCH-5** Archive documents MUST be size-bounded to ~900 KB; oversized
  messages are truncated (text halved iteratively) and flagged
  `_meta._truncated = true`.
- **FR-ARCH-6** TL objects, datetimes, bytes must be converted to Firestore-safe
  plain values (dicts, ISO strings, base64 wrappers).

### 5.5 Alerting (FR-ALERT)

- **FR-ALERT-1** A keyword match MUST trigger a Telegram alert to
  `NOTIFICATION_CHAT` containing: matched keywords, chat identifier, a
  300-character excerpt of the message, and a deep link to the original message.
- **FR-ALERT-2** Alert text MUST be Markdown-escaped so untrusted message content
  cannot break formatting or inject commands.
- **FR-ALERT-3** Bot API delivery MUST retry transient failures (429, 5xx) up to 3
  attempts with exponential backoff and respect `Retry-After` on 429 (capped at
  60 s so an aggressive value cannot consume the poll budget).
- **FR-ALERT-4** If Markdown parsing fails, the message MUST be re-sent as plain
  text as a fallback.
- **FR-ALERT-5** A permanent non-transient Bot API error MUST abort delivery for
  that message immediately (no retry).
- **FR-ALERT-6** Operational (non-keyword) alerts MUST be supported for health
  events: chat resolution failure, lost access, username lost, cursor-guard and
  cross-contamination warnings (severity affects emoji prefix).

### 5.6 State / Cursor management (FR-STATE)

- **FR-STATE-1** Cursors MUST be persisted per-chat to Firestore
  `telegram/cursor_base` as typed dicts:
  `{"ref", "last_processed_id", "alerted_keys", "schema_version"}`.
- **FR-STATE-2** The cursor MUST advance only past successfully-acked messages;
  on an alert-send failure the cursor stays at the last acked message so the
  message is retried next cycle.
- **FR-STATE-3** The cursor MUST be saved after **each** chat (incremental
  persistence), not only at the end, to minimize loss on interruption.
- **FR-STATE-4** On shutdown (signal/timeout/flood-wait) the cursor MUST be saved
  before exit.
- **FR-STATE-5** Cursor guards MUST prevent invalid advancement: reject a computed
  cursor > newest message (keep old, notify) and a computed cursor < current
  (possible cross-contamination; keep old).
- **FR-STATE-6** Cross-contamination detection MUST abort the save if multiple
  distinct chats would receive the same new cursor value.
- **FR-STATE-7** Before writing, `cursor_base` MUST be backed up (with pruning of
  old backups) to allow recovery.
- **FR-STATE-8** Legacy cursor formats MUST be auto-migrated on load
  (positional lists → typed dicts; nested-array `alerted` → CSV string;
  schema_version stamped).

### 5.7 Health & Monitoring (FR-MON)

- **FR-MON-1** After every successful poll cycle, a heartbeat MUST be written to
  `telegram/heartbeat` with `last_success_ts`, `last_success_date`, `code_version`,
  `function_name`.
- **FR-MON-2** On failure, a failure heartbeat MUST be written with
  `last_failure_ts`, `last_failure_date`, `last_failure_detail`.
- **FR-MON-3** Heartbeat writes MUST be best-effort (never crash the function).
- **FR-MON-4** `?check=1` MUST validate secrets + Firestore config only (200/500).
- **FR-MON-5** `?health=1` MUST validate config AND that the last success heartbeat
  is younger than `HEARTBEAT_MAX_AGE_SECONDS` (default 7200 s); returns 200/500.
- **FR-MON-6** All critical failure paths MUST log structured CRITICAL messages
  with identifiable prefixes: `STARTUP_FAILURE`, `RUNTIME_FAILURE`,
  `HEALTH_CHECK_FAILED` (these drive the Cloud Monitoring alert policy).

### 5.8 Secret management (FR-SEC)

- **FR-SEC-1** Credentials (`API_ID`, `API_HASH`, `BOT_TOKEN`, `session_string`)
  MUST be injected from Google Secret Manager (`TELEGRAM_SECRETS`) into the
  environment on cold start, and cached for the warm instance's lifetime.
- **FR-SEC-2** Secrets MUST be injected before any module that imports them is
  imported.
- **FR-SEC-3** If any required env var is missing after injection, startup MUST
  fail with a CRITICAL log and 500 response.
- **FR-SEC-4** The HTTP endpoint MUST NOT be deployed unauthenticated; the platform
  OIDC check is relied upon (`K_SERVICE` presence ⇒ assume validated).

### 5.9 Operator tooling (FR-OPS)

- **FR-OPS-1** A config-management CLI MUST support add/list/remove of chats and
  keywords, chat verification (resolve to channel/group, not a User), cursor
  reset (`reset-chat`), `--dry-run`, `--yes`, `--no-verify`.
- **FR-OPS-2** An e2e smoke test MUST exercise the full live pipeline: post a
  keyword test message, trigger the poller, verify the alert arrives and the
  archive exists; exit 0 = PASS; test message cleaned up.
- **FR-OPS-3** An emergency cursor reset utility MUST allow resetting cursors.
- **FR-OPS-4** A local runner MUST pause the Cloud Scheduler before running and
  resume it afterwards (including on error/Ctrl+C) to avoid single-instance
  session collisions.
- **FR-OPS-5** A deploy pipeline MUST run tests → build → smoke test → live
  heartbeat verification.

---

## 6. Non-Functional Requirements

### 6.1 Delivery guarantees

- **NFR-DEL-1 — At-least-once alerting.** A crash between successful Bot API
  delivery and cursor save MAY produce a duplicate alert; it MUST NOT lose an
  alert that was successfully delivered.
- **NFR-DEL-2 — No silent skip.** An alert failure MUST stall the cursor at that
  message so it is retried; messages are not skipped.
- **NFR-DEL-3 — Archive idempotency.** Duplicate archive writes are harmless
  (natural key).

### 6.2 Resilience & graceful shutdown

- **NFR-RES-1** The poller MUST handle `SIGINT`/`SIGTERM` by saving cursor state
  before exiting.
- **NFR-RES-2** Polling MUST self-limit via `MAX_POLL_SECONDS` (default 450 s) so
  the function exits before the GCF 540 s timeout, reducing scheduler overlap.
- **NFR-RES-3** A `FloodWaitError` with wait > 60 s MUST cause the cursor to be
  saved and the poller to exit rather than block for the full wait.

### 6.3 Performance

- **NFR-PERF-1** One shared `aiohttp.ClientSession` (10 s timeout) is reused for
  all Bot API calls within a poll cycle.
- **NFR-PERF-2** A dialog cache is built once per cycle to resolve numeric IDs
  without extra network round-trips.
- **NFR-PERF-3** Messages are processed newest-first (`reversed(messages)`) within
  a batch, advancing the cursor monotonically.

### 6.4 Security

- **NFR-SEC-1** No secrets in the repository (`keys.env` git-ignored; only
  `keys.env.example` committed).
- **NFR-SEC-2** Least-privilege dedicated service account (Firestore User +
  Secret Manager Secret Accessor); ADC preferred for local dev.
- **NFR-SEC-3** Single monolithic secret keeps within Secret Manager free-tier
  version limits.

### 6.5 Testability

- **NFR-TST-1** All external dependencies (Secret Manager, Firestore, Telethon,
  Cloud Logging) MUST be mockable so the suite runs fully offline.
- **NFR-TST-2** Test markers MUST distinguish `unit` (fast, no I/O), `integration`,
  and `slow` tests.
- **NFR-TST-3** Coverage MUST be tracked with branch coverage across
  `telegram`, `project_env`, `emergency`.

---

## 7. Data Model (Firestore)

### 7.1 `telegram/keywords`

| Field | Type | Notes |
|-------|------|-------|
| `word` | String (CSV) | Comma-separated keywords. |
| `chats` | Array | Authoritative target chat refs (`@name` or numeric). |
| `channels` | String | Legacy, ignored. |

### 7.2 `telegram/cursor_base` — map of `chat_id → cursor`

```json
{
  "<chat_id>": {
    "ref": "@username_or_numeric_id",
    "last_processed_id": 12345,
    "alerted_keys": "access_lost,username_lost",
    "schema_version": 1
  }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `ref` | String | Current chat reference (auto-updated on rename). |
| `last_processed_id` | Int | Last acked message ID; 0 = scan from start. |
| `alerted_keys` | String | CSV of one-shot operational alerts already sent. |
| `schema_version` | Int | Cursor format version (currently 1). |

### 7.3 `matched_messages/{chat_id}_{message_id}`

Kept TL fields (`id`, `date`, `message` [overridden with `.text`], `entities`,
`media`, `post_author`, `reply_to`, `fwd_from`, `edit_date`, `grouped_id`, `views`,
`forwards`, `reactions`, `peer_id`, `from_id`) plus `_meta`:

| Field | Type | Notes |
|-------|------|-------|
| `chat_id` | String | Owning chat ID. |
| `chat_identifier` | String | username/title/first_name fallback. |
| `matched_keywords` | Array | Keywords that matched. |
| `_truncated` | Bool | Present when text exceeded ~900 KB. |
| `_original_text_length` | Int | Original length when truncated. |

### 7.4 `telegram/heartbeat`

| Field | Type | Notes |
|-------|------|-------|
| `last_success_ts` / `_date` | Int / String | Present on success. |
| `last_failure_ts` / `_date` / `_detail` | Int / String | Present on failure. |
| `code_version` | String | Deployed version tag. |
| `function_name` | String | e.g. `telegrampoller` (K_SERVICE) or `local`. |

---

## 8. Interfaces

### 8.1 HTTP endpoint

Single GCF HTTP entry `main(request)`:

| Query | Behavior | Response |
|-------|----------|----------|
| *(none)* | Run a full poll cycle, then write success heartbeat. | `200 "Polling complete"` |
| `?check=1` | Validate secrets + Firestore config only. | `200 "OK"` / `500` |
| `?health=1` | Validate config + heartbeat age within threshold. | `200 "OK — last success N s ago"` / `500 "UNHEALTHY..."` |

All critical failures return `500` with a descriptive body.

### 8.2 Environment variables

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `API_ID`, `API_HASH`, `session_string`, `NOTIFICATION_CHAT` | — | Yes (injected) | Telegram credentials + alert destination. |
| `GCP_PROJECT_ID` | — | Yes | GCP project. |
| `TELEGRAM_SECRETS` | `telegram-secrets` | No | Secret Manager secret name. |
| `MAX_POLL_SECONDS` | `450` | No | Self-imposed poll budget. |
| `HEARTBEAT_MAX_AGE_SECONDS` | `7200` | No | Max heartbeat age for `?health=1`. |
| `LOGGING_LEVEL` | `INFO` | No | Python log level. |
| `CODE_VERSION` | auto | No | Version tag written to heartbeat. |
| `PRIORITY_CHAT_REFS` | — | No | Comma-separated chats polled first. |
| `BOT_TOKEN` | — | Yes (injected) | Bot API token for alerts. |

### 8.3 Alert message format (keyword)

```
🚨 **KEYWORD ALERT!** 🚨
**Keywords:** <keywords>
**Group:** <chat_identifier>
**Message:** <first 300 chars>...
[Go to message](https://t.me/c/<chat_id>/<msg_id>)
```

---

## 9. Failure Modes & Mitigations

| Failure | Behaviour | Requirement |
|---------|-----------|-------------|
| Archive write fails | Alert still sent; error logged. | FR-ARCH-3 |
| Alert send fails | Cursor not advanced; retry next cycle. | FR-STATE-2 |
| Crash after alert, before cursor save | Duplicate alert possible (at-least-once). | NFR-DEL-1 |
| `FloodWaitError` > 60 s | Save cursor + exit. | NFR-RES-3 |
| Cursor > newest / < current | Guard refuses advance; notify. | FR-STATE-5 |
| Multiple chats share a cursor value | Save aborted; notify. | FR-STATE-6 |
| Chat inaccessible (numeric) | One-shot "lost access" alert; skip chat. | FR-POLL-5 |
| Username lost (private group) | Switch to numeric ID; one-shot warning. | FR-POLL-6 |
| Ref resolves to a User | Skip registration with warning. | FR-POLL-7 |
| Heartbeat write fails | Logged, non-fatal. | FR-MON-3 |

---

## 10. Constraints

- **C-1** Python `==3.12.*` (enforced by `requires-python`).
- **C-2** GCF Gen 2 timeout 540 s; poll budget default 450 s.
- **C-3** Firestore document limit 1 MiB; archive capped at ~900 KB.
- **C-4** Telegram message IDs are positive 31-bit ints (cursor bounds check).
- **C-5** Firestore does not allow nested arrays — hence CSV `alerted_keys`.
- **C-6** Secret Manager free tier limits number of secrets → single monolithic
  secret.

---

## 11. Deployment & Operational Constraints

- **11.1** Deployed via Cloud Build (`start.yaml`) to GCF Gen 2, HTTP-triggered,
  invoked by Cloud Scheduler every 10 min (active hours 05:00–21:00 UTC) with
  OIDC auth.
- **11.2** The function MUST NOT be deployed `--allow-unauthenticated`; the
  scheduler's service account requires `roles/run.invoker`.
- **11.3** The scheduler interval MUST exceed the function's max execution time to
  prevent overlapping invocations.
- **11.4** Autoscaling is PROHIBITED (`max instances = 1`). Telethon sessions
  cannot be shared; concurrent instances invalidate the session string
  (`AUTH_KEY_UNREGISTERED`). Local runs MUST NOT overlap the cloud scheduler.

---

## 12. Assumptions

- **A-1** Monitored channels are publicly readable by the configured user account.
- **A-2** The user account (Telethon `session_string`) differs from the alert bot;
  bots cannot read history (`GetHistoryRequest`).
- **A-3** Keyword matching is plain substring, case-insensitive, Unicode-normalized.
- **A-4** Poll cadence and keyword density keep volume within Telegram rate limits;
  `MAX_POLL_SECONDS` exists specifically to bound this.
- **A-5** The same single user session is used; horizontal scaling is deliberately
  out of scope.

---

## 13. Acceptance Criteria (Summary)

The system is considered to meet its technical requirement when:

1. A scheduled poll scans all configured chats and alerts on new keyword matches.
2. Config (keywords/chats) changes take effect on the next poll without redeploy.
3. Interrupting the process at any point does not corrupt `cursor_base` and loses
   at most one in-flight chat's progress (duplicates allowed, losses not).
4. Matched messages are archived idempotently in `matched_messages`.
5. A failure of the Bot API, Telegram, or the poller itself is visible via
   heartbeat + `?health=1` + CRITICAL logs → operator alert, independent of
   Telegram reachability.
6. The offline test suite (unit/integration/slow) passes with mocked GCP deps.
7. The e2e smoke test passes against the live deployed function.

---

## 14. Requirements ↔ Test Traceability Matrix (RTM)

Row = functional requirement; column coverage = the automated tests that guard
it. `—` = no direct automated test currently covers that requirement (manual /
operational verification only). Tests are grouped per module; see §15 for the
module-level inventory.

| Req | Covered by | Module |
|-----|-----------|--------|
| FR-CONF-1 | `TestFormingConfigurationHappyPath`, `TestKnownUsernamesConstruction` | test_starter_conf |
| FR-CONF-2 | Config TTL / load-path — `TestConfigLoading` (GCF), `TestGCFDeployHappyPath` | test_gcf_deploy |
| FR-CONF-3 | `test_field_list_array_vs_legacy_string` | test_manage_config |
| FR-CONF-4 | `TestFormingConfigurationErrors`; poller guard `test_empty_target_chats_returns_early`, `test_empty_keywords_returns_early` | test_starter_conf / test_listener |
| FR-CONF-5 | keyword normalization — `TestLegacyMigration`, `TestFormingConfigurationHappyPath` | test_starter_conf |
| FR-POLL-1 | fetch newer than cursor — `test_numeric_ref_steady_state_is_silent`; lifecycle paths | test_listener |
| FR-POLL-2 | `test_verify_and_provision_from_start_sets_cursor_zero`; `add-chat` provisioning | test_manage_config |
| FR-POLL-3 | `TestChatPollingPriority` (get/parse/sort) | test_listener |
| FR-POLL-4 | numeric dialog-cache fallback `_lookup_dialog` — `test_numeric_ref_steady_state_is_silent` | test_listener |
| FR-POLL-5 | `test_add_chats_rejects_chat_that_resolves_to_user` (manager-side), access-lost path via lifecycle logs | test_manage_config |
| FR-POLL-6 | `test_reset_chat_*` ref handling (partial) | test_manage_config |
| FR-POLL-7 | `test_add_chats_rejects_chat_that_resolves_to_user` | test_manage_config |
| FR-MATCH-1 | `TestSendAlert` (match formatting); serialization path | test_send / test_message_store |
| FR-MATCH-2 | `test_numeric_ref_steady_state_is_silent` (non-matching msg processed) | test_listener |
| FR-MATCH-3 | `TestMatching` — substring semantics, case-insensitivity, NFKC fullwidth + composed/decomposed | test_listener |
| FR-ARCH-1 | `test_archive_verified_true` (e2e); `TestSerializeMessage` | test_e2e_test / test_message_store |
| FR-ARCH-2 | idempotent key `{chat_id}_{message_id}` — `TestSerializeMessage` | test_message_store |
| FR-ARCH-3 | archive-independent alerting — pipeline upstream; not unit-isolated | — |
| FR-ARCH-4 | `TestStripNulls`, `TestSerializeMessage` | test_message_store |
| FR-ARCH-5 | `TestSerializeMessage` (truncation), `TestEstimateSize` | test_message_store |
| FR-ARCH-6 | `TestExtractTlValue`, `TestTLObjectToDict` | test_message_store |
| FR-ALERT-1 | `TestSendAlert` | test_send |
| FR-ALERT-2 | `TestSendBotNotification` (escape), `test_marker_has_no_markdown_metacharacters` | test_send / test_e2e_test |
| FR-ALERT-3 | `test_retries_transient_then_succeeds`, `test_retries_exhausted_raises`, `test_retry_after_honored` | test_send |
| FR-ALERT-4 | `TestSendBotNotification` (plain-text fallback) | test_send |
| FR-ALERT-5 | `TestSendBotNotification` (permanent error, no retry) | test_send |
| FR-ALERT-6 | `TestSendHealthAlert` | test_send |
| FR-STATE-1 | `TestCursorValidation`, `TestLegacyMigration` | test_starter_conf |
| FR-STATE-2 | `test_saves_successfully`; `TestAlertFailureCursorStall` (alert raises → cursor stays, batch breaks) | test_listener |
| FR-STATE-3 | `_save_cursor_sync` per-chat saving — `TestSaveCursorSync` | test_listener |
| FR-STATE-4 | `TestShouldStop` + `test_shutdown_during_chat_registration`, `test_timeout_during_chat_registration` | test_listener |
| FR-STATE-5 | `TestCursorGuards` (computed > newest / < current → keep old + notify) | test_listener |
| FR-STATE-6 | `TestCrossContaminationDetection` (duplicate cursor values → abort final save) | test_listener |
| FR-STATE-7 | `TestBackupBeforeSave` (backup + prune called; backup failure non-fatal) | test_listener |
| FR-STATE-8 | `test_reset_chat_*`, `TestLegacyMigration`, `_migrate_cursor_to_typed` | test_manage_config / test_starter_conf |
| FR-MON-1 | `TestGCFDeployHappyPath` (heartbeat success) | test_gcf_deploy |
| FR-MON-2 | `TestGCFDeployErrors` (failure heartbeat) | test_gcf_deploy |
| FR-MON-3 | `TestWriteHeartbeat.test_firestore_failure_is_best_effort`, `test_heartbeat_read_failure_returns_none` | test_gcf_deploy |
| FR-MON-4 | `TestGCFDeployHappyPath` (`?check=1`) | test_gcf_deploy |
| FR-MON-5 | `?health=1` — `TestEnvironmentDetection`, `TestGCFSpecific` | test_gcf_deploy |
| FR-MON-6 | `TestGCFDeployErrors` (CRITICAL prefixes) | test_gcf_deploy |
| FR-SEC-1 | `TestGCFDeployHappyPath` (secrets injection) | test_gcf_deploy |
| FR-SEC-2 | `TestGCFSpecific` (inject-before-import) | test_gcf_deploy |
| FR-SEC-3 | `TestGCFDeployErrors` (missing env → 500) | test_gcf_deploy |
| FR-SEC-4 | `TestEnvironmentDetection` (K_SERVICE/auth) | test_gcf_deploy |
| FR-OPS-1 | `Test*` in test_manage_config (add/remove/reset/dry-run/verify) | test_manage_config |
| FR-OPS-2 | `Test*` helper coverage in test_e2e_test (keyword guard, archive, alert detect, trigger) | test_e2e_test |
| FR-OPS-3 | `TestResetCursorsHelpers` — `emergency/reset_cursors.py` helpers (gap, cursor/ref extraction, dialog lookup, latest-message fetch) | test_tooling |
| FR-OPS-4 | `TestShellScripts` — `bash -n` lint + shebang check for `run_local.sh`, `deploy.sh`, `scripts/*.sh` | test_tooling |
| FR-OPS-5 | `TestShellScripts` (deploy.sh lint) + `TestGCFDeploy*` simulates deploy lifecycle | test_tooling / test_gcf_deploy |

**Traceability note:** requirements without a direct test (`—`) are either
operationally verified or exercised through the live deploy/e2e pipeline rather
than the offline suite — and the offline suite size itself is audited by
`TestSuiteInventoryAudit` (tests/test_suite_audit.py) against
`tests/expected_test_counts.json`.

---

## 15. Test Suite Inventory

Reference inventory of the offline `pytest` suite. All GCP deps are mocked
(`conftest.py` fixtures) so tests run offline.

| Module | Groups / classes | Aspect covered |
|--------|------------------|----------------|
| `test_gcf_deploy.py` | Happy path, config loading, errors, GCF-specific, env detection, `TestWriteHeartbeat` | Deploy invocation lifecycle, secret injection, `?check` / `?health`, failure paths, heartbeat success/failure fields + best-effort. |
| `test_listener.py` | `TestShouldStop`, `TestSafeTitle`, `TestSaveCursorSync`, `TestPollTelegramLifecycle`, `TestChatPollingPriority`, `TestMatching`, `TestCursorGuards`, `TestCrossContaminationDetection`, `TestBackupBeforeSave`, `TestAlertFailureCursorStall` | Signal/timeout, title extraction, cursor persistence, poll lifecycle, priority ordering, keyword matching (substring/NFKC/case), cursor guards, cross-contamination abort, backup/prune, alert-failure cursor stall. |
| `test_message_store.py` | `TestStripNulls`, `TestExtractTlValue`, `TestTLObjectToDict`, `TestEstimateSize`, `TestSerializeMessage` | Serialization, type conversion, truncation, size bounds. |
| `test_send.py` | `TestSendBotNotification` (delivery, transient retry + backoff, 429 Retry-After, Markdown escape/plain-text fallback, permanent-error no-retry), `TestSendAlert`, `TestSendHealthAlert` | Bot API delivery, retries, Markdown escape/fallback, alert formatting. |
| `test_manage_config.py` | (20 flat tests) | Config normalization, chat/keyword add/remove, dedup, User rejection, cursor provisioning/reset, dry-run. |
| `test_e2e_test.py` | (11 flat tests) | E2E helpers: keyword guard, archive check, alert detection, int/string chat IDs, gcloud trigger construction. |
| `test_starter_conf.py` | Happy path, errors, cursor validation, legacy migration, known-usernames | Firestore config load, cursor validation, migration. |
| `test_tooling.py` | `TestResetCursorsHelpers`, `TestShellScripts` | `emergency/reset_cursors.py` helpers (gap formatting, cursor/ref extraction, dialog lookup, latest-message fetch); `bash -n` lint + shebang for `run_local.sh`, `deploy.sh`, `scripts/*.sh`. |
| `test_suite_audit.py` | `TestSuiteInventoryAudit` | Re-collects the suite and compares total + per-module counts against `tests/expected_test_counts.json` (regen: `python3 scripts/audit_test_counts.py --write`). |

**Test markers** (`pytest`): `unit` (fast, no I/O — pre-commit safe), `integration`
(multi-module / mocked network), `slow` (significant runtime — excluded from quick
runs).

---

## 16. Change Log & Drift Tracking

Use this section to track any deviation between this requirements document and the
implemented code. When code changes, update the requirements **and** log the change
here.

| Date | Section / Req | Change | Author |
|------|--------------|--------|--------|
| *(initial)* | v1.0.0 retrospective baseline | Document created from current implemented state. | — |
| 2026-08-16 | §14, §16.2 | Gap-closure batch: added offline tests for cursor guards, cross-contamination, backup/prune, heartbeat best-effort, alert-failure cursor stall, substring matching, reset_cursors helpers + `bash -n` shell lint, and a test-count audit (manifest `tests/expected_test_counts.json` + `tests/test_suite_audit.py`). Suite grew 122 → 166 tests; README counts corrected. | — |
| 2026-08-16 | FR-POLL-6 | **Bug fix**: registration check for an already-known numeric chat indexed the typed cursor dict with `[0]` (legacy-list style) → `KeyError: 0` → spurious "Chat resolution failed" CRITICAL + alert every cycle for numeric-ID chats. Now keyed by `"ref"` (implementation fix only; intended behaviour unchanged). | — |
| 2026-08-16 | FR-ALERT-3, §2, §10, §11 | **Bug fix (retry loop) + Gen 2 correction.** The Bot API retry loop never retried: `_post_once` *returned* a `RuntimeError` for transient 5xx instead of raising, so the loop collapsed to a single Markdown attempt + one plain-text fallback, with no backoff. Rewrote `send.py` to raise typed errors (`_TransientError`, `_MarkdownParseError`) and retry transient failures with exponential backoff + jitter; 429 `Retry-After` honored but capped at 60 s. Added tests `test_retries_transient_then_succeeds`, `test_retries_exhausted_raises`, `test_retry_after_honored` (FR-ALERT-3 now genuinely covered; RTM cell updated). Also corrected the deployment target from Gen 1 to **Gen 2** across README/TRD/`start.yaml` (`--gen2` flag added) so the Monitoring alert filter (`cloud_run_revision`) matches the actual runtime. Suite 166 → 169 tests. | — |

### 16.1 Change policy

- **New behaviour** → add/amend a requirement (§5) and a row in the RTM (§14).
- **Removed behaviour** → strike or retire the requirement; log the removal.
- **Bug fixes** → note in this log; do not rewrite the spec unless the intended
  behaviour (requirement) changed, not just the implementation.
- **Out-of-scope additions** → record under §16.2 so they are deliberate and
  reviewed against §2.2.

### 16.2 Known gaps / technical debt

| Gap | Impact | Linked req | Suggested action |
|-----|--------|-----------|------------------|
| No dedicated automated tests for cursor guards, cross-contamination detection, backup/pruning, heartbeat best-effort. | Guard behaviour only verified manually / via live ops. | FR-STATE-5, 6, 7; FR-MON-3 | ✅ **Closed 2026-08-16** — `TestCursorGuards`, `TestCrossContaminationDetection`, `TestBackupBeforeSave` (test_listener), `TestWriteHeartbeat` (test_gcf_deploy). Also exposed + fixed a real `KeyError` bug in the numeric-ref registration path (see change log). |
| `emergency/reset_cursors.py`, `run_local.sh`, `deploy.sh` have no automated tests. | Tooling regressions undetected by CI. | FR-OPS-3, 4, 5 | ✅ **Closed 2026-08-16** — `tests/test_tooling.py`: behavioral tests for `reset_cursors.py` helpers + `bash -n` lint and shebang check for all `.sh` scripts. |
| Substring matching (FR-MATCH-3) has no explicit negative/positive focused tests. | Matching edge cases (word boundaries) untested. | FR-MATCH-3 | ✅ **Closed 2026-08-16** — matching extracted into pure `_find_matching_keywords()` (listener.py) + `TestMatching` (substring, case, NFKC fullwidth/composed-decomposed, empty text). |
| Poller's alert-failure cursor-stall (FR-STATE-2 at message level) exercised only via full lifecycle, not isolated. | Failure injection untested in isolation. | FR-STATE-2 | ✅ **Closed 2026-08-16** — `TestAlertFailureCursorStall.test_alert_failure_keeps_cursor_and_breaks_batch`: alert raises → cursor stays, batch loop breaks. |
| Document count (81 tests) is documented only in README; no automated audit. | Spec/README/test-count drift. | NFR-TST-* | ✅ **Closed 2026-08-16** — manifest `tests/expected_test_counts.json` + `TestSuiteInventoryAudit` (test_suite_audit.py) re-collects the suite and fails on drift (regen: `python3 scripts/audit_test_counts.py --write`). README inventory corrected to the actual 166 tests. |
