# Security Review — Phase A Remediation

Task contract: `gemini-lane-security-review` (`TASK_CONTRACTS.json`). Filename is bound
by contract (`path_template: GEMINI_SECURITY_REVIEW.md`) — no Gemini model is involved;
the name is legacy from an earlier multi-lane naming scheme.

**Approval provenance.** Verified against on-disk repository state and source
(re-checked 2026-07-25 after an earlier draft of this section stated some of it wrongly):
- The contract sets `requires_approval: true`. `orchestrator_loop.py:550` gates dispatch on
  `approval_logged(task_id, task_contract_sha256=..., task_spec_sha256=...)` — the full
  digest-bound triple — and `orchestrator_loop.py:626` then calls `consume_approval()` with
  the same digests, which atomically removes the record on success.
- `MASTER_LOG.md` records both states of that gate for this task
  (queue id `LEGACY-d6c45b6a6226`): at `2026-07-23 12:46 UTC`, "awaiting digest-bound
  approval"; at `2026-07-25 05:06 UTC`, "typed contract gemini-lane-security-review v1.0
  validated — dispatching", followed by a queued launch. Passing both gates means a
  digest-bound approval record for this exact contract/spec existed at 05:06 UTC and was
  consumed then. Who recorded it is **not** determinable from repository state; it was not
  recorded by the author of this report.
- `approved_tasks.json` still contains a separate, older record for
  `gemini-lane-security-review` (`approved_at: 2026-07-06T04:24:52Z`) carrying no
  `task_contract_sha256` / `task_spec_sha256` fields. That record alone cannot satisfy
  `consume_approval()`; it is not the record that was consumed.
- The `WORK_QUEUE.json` row shows `status: in_progress` with `contract_validated_at:
  2026-07-25T05:06:19Z` and **no** `lease_*` fields. That shape identifies
  `orchestrator_loop.py` (`_mark_task_in_progress` / `_mark_task_contract_validated`) as the
  writer, not `harness/agent_coordinator.py`, whose claim path would additionally populate
  `lease_id` / `lease_owner` / `lease_contract_sha256` / `lease_task_spec_sha256`. An earlier
  draft of this section asserted those lease fields were populated; they are not.
- The launch queued at 05:06 UTC reached `pickup ready` only; the session
  `jarvis-general-claude-legacyd6c45b6a6226` is absent from `ACTIVE_SESSIONS.json` and never
  attached. No autonomous session edited the repository concurrently with this work.

This document covers Phase A only: the AppleScript/shell-injection findings from the
prior read-only scan, verified against current HEAD and fixed. Everything under
"Not yet addressed" is explicitly deferred to Phase B and was **not** touched.

## Findings

### CRITICAL — `messages.py` `send_imessage`, AppleScript → shell injection (recipient)

- **File:line**: `messages.py:658-729` (function `send_imessage`), interacting with
  `router.py:2581` (`_looks_like_contact_name`).
- **Why exploitable**: The function decided whether a `recipient` string was a "direct
  address" (skip Contacts lookup, interpolate raw) using only
  `bool(re.search(r"[\d@\+]", recipient))` — i.e. "contains a digit, `@`, or `+`".
  Combined with `router.py`'s `_looks_like_contact_name()`, which contained an
  unconditional `if "@" in cleaned: return True`, any string merely containing `@`
  was treated as a plausible recipient and passed through untouched into:
  ```
  set targetBuddy to buddy "{address}" of targetService
  ```
  An attacker-influenced string such as `x@" & (do shell script "<REDACTED_CMD>") & "`
  (e.g. via a voice/STT transcript, a pasted message, or any untrusted text reaching
  `send_imessage`) closes the AppleScript string literal, uses AppleScript's `&`
  concatenation plus `do shell script` to run an arbitrary shell command as the logged
  in user, then reopens a string literal to keep the script syntactically valid.
  Evidence of the shape (redacted, not a real payload — illustrative of the class of
  string that broke out of the quoted literal): `x@" & (do shell script "...") & "`.
- **Fix applied**:
  - `messages.py`: added a strict allowlist — a recipient is only treated as a direct
    address if it matches `^\+?[0-9()\-.\s]{7,}$` (phone) or
    `^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$` (email). Anything else — including
    any string that merely *contains* `@`/digits without matching those shapes — now
    falls through to `lookup_contact()` (name-based Contacts resolution), exactly as a
    genuine contact name would. If Contacts can't resolve it, the function returns a
    "couldn't find a contact" message instead of ever reaching the AppleScript
    interpolation.
  - Whatever address is ultimately used (validated direct address or a value returned
    by `lookup_contact()`) is now also run through the shared `_escape_applescript()`
    helper before interpolation, as defense in depth.
  - `router.py` `_looks_like_contact_name`: replaced the unconditional `if "@" in
    cleaned: return True` with a conservative email-shape regex
    (`_CONTACT_EMAIL_SHAPE_RE = re.compile(r"^[\w.\-+%]+@[\w.\-]+\.[A-Za-z]{2,}$")`) —
    deliberately excludes quotes, spaces, parens, and `&`, so injection-shaped strings
    are rejected at this earlier gate too, independent of the messages.py fix.

### CRITICAL — `messages.py` `send_imessage`, reversed AppleScript escape order (message body)

- **File:line**: `messages.py:692` (pre-fix), function `send_imessage`.
- **Why exploitable**:
  ```python
  safe_msg = message.replace('"', '\\"').replace("\\", "\\\\")
  ```
  Quotes were escaped *before* backslashes. For a body containing `"`, the first
  `.replace('"', '\\"')` turns `"` into `\"` (one backslash + quote). The second
  `.replace("\\", "\\\\")` then re-doubles that just-inserted backslash, producing
  `\\"` — an escaped backslash followed by an **unescaped** quote. That unescaped
  quote terminates the AppleScript string literal early, and any trailing text becomes
  free-standing AppleScript source (a second, weaker injection primitive alongside the
  recipient bug — same class of bug the rest of the repo already avoids, see "Clean
  areas" below).
- **Fix applied**: swapped to the correct backslash-first order, and delegated to the
  already-existing, already-correct helper instead of a bespoke inline expression:
  ```python
  safe_address = _escape_applescript(address)
  safe_msg = _escape_applescript(message)
  ```
  `_escape_applescript` is imported from `browser.py` (`from browser import
  _escape_applescript`) rather than re-implemented a third time in this file — see
  "Fix scope" for why `api.py` was left with its own inline copy.

### MEDIUM — `api.py:3520` (`/remote/type`), same reversed escape order

- **File:line**: `api.py:3520` (pre-fix line number; function `remote_type`).
- **Why exploitable**: identical bug pattern to the messages.py body escaping above,
  feeding a `keystroke "<text>"` AppleScript command instead of `send "<text>"`. Lower
  severity because the endpoint is gated behind `_token_authorized(request)` (bearer
  token required), so exploitation requires a valid token first — but a valid-token
  caller (or a token leaked/replayed) could still break out of the `keystroke` literal
  and run arbitrary AppleScript/shell via a crafted `text` value.
- **Fix applied**: swapped to backslash-first order:
  ```python
  safe_text = req.text.replace('\\', '\\\\').replace('"', '\\"')[:500]
  ```
  Left as an inline expression (not switched to `browser._escape_applescript`) to keep
  the change minimal — `api.py` does not currently import `browser`, and the fix
  requested for this file was explicitly scoped to the escape-order swap only.

### LOW — `router.py:227-236` `_schedule_osascript_alarm`, raw newline in title

- **File:line**: `router.py:227-236` (pre-fix), function `_schedule_osascript_alarm`.
- **Why exploitable**: escape order here was already correct
  (`title.replace("\\", "\\\\").replace('"', '\\"')`). The remaining bug: the generated
  script is a two-line AppleScript string built with an embedded `\n`:
  ```
  delay {delay_secs}\ndisplay notification "{safe_title}" with title "Jarvis Reminder" ...
  ```
  If `title` itself contains a raw `\n`/`\r` (e.g. a multi-line reminder title from a
  parsed voice command), the escaper does not touch newlines, so the title's newline
  splits the `delay N` line early and corrupts the script — a syntax error (denial of
  the reminder), not code execution, since the injected content lands on its own
  script line rather than inside a live AppleScript string context that then continues
  to be interpreted as commands.
- **Fix applied**:
  ```python
  flat_title = title.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
  safe_title = browser._escape_applescript(flat_title)
  ```
  Also switched the inline escape expression to the shared `browser._escape_applescript`
  helper (this file already does `import browser` at module scope), matching the
  "reuse the existing helper" guidance instead of keeping a fourth inline duplicate.

## Clean areas

Confirmed as already correct (backslash-first escape order) during this review, not
modified:
- `terminal.py:32` (`_escape_applescript`, used by `run_command`)
- `browser.py:118` (`_escape_applescript`, used for URL/JS interpolation)
- `router.py:233` (was already correct — only the newline-splitting bug above was live)
- `harness/notify.py:24`
- `harness/completion_verifier.py:287`
- `ade/notify.py:31`

## Fix scope

Files edited in this change (matches the approved HIGH-severity fix scope for
`gemini-lane-security-review`):

- `messages.py` — strict recipient allowlist (`_is_valid_direct_address`,
  `_PHONE_ADDRESS_RE`, `_EMAIL_ADDRESS_RE`), import of `browser._escape_applescript`,
  fixed escape order for recipient + body in `send_imessage`.
- `router.py` — tightened `_looks_like_contact_name` email gate
  (`_CONTACT_EMAIL_SHAPE_RE`); `_schedule_osascript_alarm` now strips `\r`/`\n` from
  the title and reuses `browser._escape_applescript`.
- `api.py` — fixed reversed escape order in `remote_type` (`/remote/type`).
- `tests/test_messages_contacts.py` — regression tests for the recipient/body fixes.
- `tests/test_router_applescript_injection_regression.py` (new) — regression tests for
  `_looks_like_contact_name` and `_schedule_osascript_alarm`.
- `tests/test_remote_controls.py` — regression test for `/remote/type` escape order.

No other files were modified. No secrets, tokens, or credential values appear in this
report or in the test fixtures (test bearer token `jarvis-test-remote-token` is a
pre-existing, repo-wide test-only sentinel, not a real credential — it is unrelated to
this change and predates it).

## Not yet addressed (deferred to Phase B)

Per the approved scope for this pass, the following are explicitly **not** fixed here
and require separate, dedicated review:

- Deterministic manager security gating.
- Generated-test confinement.
- Capability enforcement for direct specialist function calls.
- Outbound private-data controls.
- Untrusted repository-context separation.
- `terminal.py:193-211` (`run_python`) — spot-checked while writing this report:
  the function writes `code` to a temp file and runs it via `subprocess.run(["python3",
  path], ...)` with **no** call to `safety_permissions.authorize_tool_call` (or any
  other gate) in the function body. This looks like a real gap, but fixing it is out of
  the approved Phase A scope (AppleScript/shell escaping only) — flagged here for
  Phase B triage rather than fixed now.
- `behavior_hooks.py:212-225` — uses a denylist rather than an allowlist for path
  policy. Flagged as a product-design question for Phase B discussion, not treated as
  a bug in this report.
