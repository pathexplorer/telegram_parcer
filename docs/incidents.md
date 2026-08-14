# Incidents

Operational write-ups for failures encountered in production. Each entry
documents the symptom, the root cause, and the prevention rule for future
Telegram pipelines.

---

## 2026-08-13 — Silent alert failure + recurring CRITICAL parse errors

### Symptom

1. **Recurring CRITICAL in Cloud Logging** (every ~15 min, one per scheduler run):

   ```
   ❌ Permanent error: Bot API returned 400: {"ok":false,"error_code":400,
      "description":"Bad Request: can't parse entities: Can't find end of the
      entity starting at byte offset 582"}
   ```

   Logged from `telegram/send.py` `_post_once` on every poll cycle.

2. **No email was received** for these failures. The Cloud Monitoring alert
   policy never fired even though the function logs were full of CRITICALs.

3. Cloud Scheduler entries showed `status: DEADLINE_EXCEEDED` / HTTP 504,
   displayed as `!! Error` in the Cloud Logging UI.

### Root causes

**A. Untrusted content broke Markdown parsing.**

The keyword-alert message embeds raw, untrusted content from monitored
channels: the message excerpt (`message.text[:300]`), chat identifiers,
keywords, and titles. Monitored channels can contain stray or unclosed
Markdown characters (`*`, `_`, `[`, `]`, `` ` ``, `|`, …). When that raw
text is interpolated into a message sent with `parse_mode="Markdown"`,
Telegram's parser rejects the **entire** message with HTTP 400
("can't parse entities"), so the alert is silently dropped.

**B. Alert filter targeted the wrong resource type.**

The alert policy matched `resource.type="cloud_function"`, but the
function is **Gen 2** — which Cloud Logging records under
`resource.type="cloud_run_revision"` (with `resource.labels.service_name`).
The filter never matched any log entry, so no alert ever fired.

**C. Scheduler 504s were cosmetic.**

Cloud Scheduler HTTP targets have a hard **60 s** timeout. The poller is
designed to run up to `_MAX_POLL_SECONDS=450` s, so the scheduler's own
HTTP connection timed out and logged `DEADLINE_EXCEEDED` + 504 while the
function kept running in the background. Informational, not a real failure.

### Fix

- **Escape untrusted content** at the source (`_md_escape` in
  `telegram/send.py`) before interpolating into Markdown messages.
- **Plain-text fallback**: if the Bot API still rejects the message with
  `can't parse entities`, resend it without `parse_mode` so the alert is
  always delivered instead of dropped. Non-parse permanent errors still
  raise immediately (original behavior preserved).
- **Corrected the alert filter** to:
  `resource.type="cloud_run_revision" AND resource.labels.service_name="telegrampoller" AND severity>=CRITICAL`
  and re-bound the email notification channel.
- Added regression tests for escaped malformed content and the plain-text
  fallback. Full suite: 85 passed.

### Verification

- CRITICAL parse-error logs and scheduler ERROR entries stopped after
  deploy.
- Historical CRITICAL log entries produced a one-time backlog of alert
  emails (expected after the filter correction), which then stopped.

### Prevention rules for future Telegram pipelines

1. **Never trust upstream content.** Escape any text coming from monitored
   channels/users before inserting it into Markdown or HTML messages.
2. **Never drop an alert silently.** Add a plain-text fallback for
   `parse_mode` rejection so a formatting failure can't discard an alert.
3. **Truncate excerpts safely.** Slice on a character/surrogate boundary
   and sanitize **before** truncating so you never cut mid-entity.
4. **Prefer HTML parse mode** where possible — `html.escape` is lossless and
   more forgiving than legacy `Markdown`. If using Markdown, escape all
   metacharacters.
5. **Match alert/log filters to the actual runtime.** Check the function
   generation (`gcloud functions describe ... --format=value(environment)`);
   Gen 2 logs under `cloud_run_revision`. Dry-run the filter with
   `gcloud logging read '<filter>'` and confirm it matches real entries
   before trusting the alert.
6. **Decouple health from notification success.** Use an independent
   heartbeat (e.g. `Heartbeat written: success`) as the authoritative
   liveness signal so a single failed delivery can't stall the pipeline.
7. **Test the failure paths.** Add regression tests for malformed content
   and fallback behavior so a new caller can't reintroduce a silent drop.

### References

- `telegram/send.py` — `_md_escape`, plain-text fallback in
  `send_bot_notification`, `send_alert`
- `alert_policy.json` — Gen 2 log filter
- `tests/test_send.py` — regression tests
