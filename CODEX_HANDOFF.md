# Jarvis Beta Test Handoff — 2026-04-25

## What was done this session

### Bugs fixed
1. **`Say:` prefix leaked into message body** — `_sanitize_message_body` now strips leading `Say: ` before storing draft.
2. **`send an iMessage to +NUMBER` not parsed** — compose regex extended to include `imessage`/`i message` keyword.
3. **Wrong contact sent without showing number** — draft confirmation now shows resolved phone number when unambiguous (`_eager_resolve_contact` + `_message_confirmation_prompt` update).
4. **Time query going to LLM** — `_dispatch_single_intent` now runs on single queries too (not just multi-intent), instant `datetime.now()` response.
5. **Web search opening Chrome instead of returning results** — fast-path added before the browser block in `route_stream`; `"search the web for"` / `"search google for"` removed from browser triggers.
6. **`qwen3.6:35b-a3b` model tag mismatch** — fixed in `config.py` to `qwen3.6:35b` (actual Ollama tag).
7. **`mem0ai` not installed** — installed in venv; confirmed `mem0_layer.status()` was returning false due to missing package.

### New features added
- **`messages_thread.py`** — lightweight conversation thread tracker. Records sent messages and incoming relays. Persists to `~/Library/Application Support/Jarvis/message_threads.json`.
- **"X replied: [message]" flow** — Jarvis records the incoming message, generates a contextual draft reply using conversation history, and stages it for confirm-send.
- **"reply to X" flow** — shows conversation history with that contact and asks what to say.
- Both flows wire into the existing confirm-send confirmation gate.

## What still needs testing/fixing

### Actively in-progress (tests running when handed off)
- Web search returns results vs opening browser — test result pending (model was loading)
- Incoming message relay ("Farhan replied: ...") — test result pending (requests queued behind model)

### Known issues not yet addressed
- **Audio hardware error** (`PaMacCore AUHAL "what" error`) — repeated in logs, microphone is erroring during wake-word polling. This causes STT polling errors but doesn't crash the app. Needs investigation in `voice.py`.
- **5 repeated `tool_execution` failures** in cost_policy tracking — these are from the pre-fix crashes. Will clear after fresh conversations succeed.
- **mem0 cross-session memory** — installed but not yet verified working end-to-end with Qdrant embeddings.
- **iMessage read history** — requires Full Disk Access for `chat.db`. Current workaround: Jarvis-maintained thread log. User needs to grant FDA to Terminal in System Settings > Privacy & Security > Full Disk Access for full history read.

## Current Jarvis state
- Running: `./venv/bin/python main.py --no-ui` on port 8765
- Token: check `~/Library/Application Support/Jarvis/.jarvis_runtime.json`
- Mode: open-source (all local, $0 cost)
- Active models: `qwen3.6:35b` (general), `deepseek-r1:14b` (reasoning), `gemma4:e4b` (fast/simple)

## How to continue testing

```bash
cd ~/jarvis-ai
TOKEN=$(python3 -c "import json; print(json.load(open('/Users/truthseeker/Library/Application Support/Jarvis/.jarvis_runtime.json'))['token'])")

# Helper
chat() { curl -s http://127.0.0.1:8765/chat -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d "{\"message\": $(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$1")}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('['+d.get('model','?')+']: '+d.get('response',''))"; }

# Test each feature
chat "What time is it?"
chat "Search the web for latest AI news"
chat "What is the capital of France?"
chat "Farhan replied: yo bro that intro message was wild"
chat "cancel message"   # if draft is pending
chat "reply to Farhan"
```

## Next enhancements to build
1. **Email read + reply** — `google_services.get_unread_emails()` works, needs compose flow similar to iMessage
2. **Proactive notifications** — background watcher that pings on calendar events, emails, etc.
3. **Voice wake-word audio hardware fix** — silence the PaMacCore AUHAL errors
4. **iMessage FDA grant** — add instruction/prompt for user to grant Full Disk Access so Jarvis can read `chat.db` natively
5. **Weather with location** — `tools.get_weather()` may need a location parameter

## Codex app addendum — 2026-04-25 01:10 PT

Additional fixes applied after the initial handoff:
- `messages_thread.py` now aliases contact-name and resolved-address keys so sent messages and relayed incoming replies land in the same Jarvis-maintained thread.
- `reply to <contact>` now sets the pending recipient when history exists, so the next user utterance becomes a draft instead of falling into general chat.
- Incoming relay detection is narrower: explicit `replied/texted/messaged/wrote/responded: ...` or `said to reply ...` only. Plain prose like `Aman said write a SQL query` stays normal chat.
- Generated reply drafts now pass through unsafe-message screening and eager contact resolution before confirm-send.
- Added isolated temp-file tests for message thread storage; no test writes to the real Application Support message thread file.

Verified after addendum:
```bash
python3 -m pytest tests/test_messages_thread.py -q
python3 -m pytest tests/test_message_intent_parsing.py -q
python3 -m pytest tests/test_jarvis_regression_suite.py -q -k 'test_message_multi_turn_collects_recipient_then_body or test_time_query_bypasses_pending_message_draft or test_search_query_bypasses_pending_message_draft or test_general_question_bypasses_pending_message_draft or test_reply_to_thread_sets_pending_recipient_for_next_body or test_plain_said_prompt_does_not_become_incoming_message_relay or test_ambiguous_contact_option_selection_reconfirms_same_body_and_sends_to_resolved_contact'
python3 -m pytest tests/test_jarvis_regression_suite.py -q -k 'email_compose or email_confirm or pending_email or time_query_bypasses_pending_email'
python3 -m pytest tests/test_mem0_layer.py tests/test_voice_tts_regression.py -q
```

## Claude Code parallel track — 2026-04-25

**Taking ownership of (do not touch):**
1. `briefing.py` — morning/on-demand status briefing (weather + calendar + unread email + memory summary)
2. `router.py` lines for `email_compose` flow — send email via Google, parallel to iMessage flow
3. `tools.py` — `web_search` summarization pass using gemma4:e4b (no LLM timeout risk)
4. `google_services.py` — `send_email()` wiring + `get_unread_emails()` improvements

**Leave for Codex:**
- `voice.py`, `messages.py`, `messages_thread.py`, `mem0_layer.py`, `local_runtime/`
- Any STT/TTS/audio fixes
- iMessage FDA flow

## Codex parallel track — 2026-04-25 18:05 PT

Applied and verified:
- Weather now accepts requested locations: `tools.get_weather(location="")`; router fast-path and orchestrator weather pass parsed `location/city/place`.
- Timer fast-path now accepts `set a 5 minute timer`, not only `set a timer for 5 minutes`.
- Pending email drafts now accept bare `cancel`, `stop`, `nevermind`, and `no`; these clear the draft and never call `send_email`.
- Email compose now accepts conservative spoken forms: `write an email to ... saying ...`, `draft an email for ... saying ...`, and `send ... an email saying ...`.
- Google OAuth files moved out of repo/app bundle path: `google_services.py` uses `~/Library/Application Support/Jarvis/{credentials.json,token.json}` and migrates legacy repo-root files if needed.
- `Jarvis.spec` excludes `.env`, `credentials.json`, and `token.json` from bundled datas.
- Normal voice wake-word mic selection skips meeting/virtual devices such as `Microsoft Teams Audio`, `ZoomAudio`, `BlackHole`, `Loopback`, and aggregate/multi-output devices. Smart Listen keeps its separate meeting-audio policy.
- Voice regression tests now point `.jarvis_voice.log` at a temp path so tests do not pollute production Application Support logs.

Verification:
```bash
./venv/bin/python -m unittest tests.test_mem0_layer tests.test_voice_tts_regression \
  tests.test_jarvis_regression_suite.RouterTests.test_local_beta_fast_path \
  tests.test_jarvis_regression_suite.RouterTests.test_engineering_beta_fast_path \
  tests.test_jarvis_regression_suite.RouterTests.test_google_auth_files_are_outside_repo_and_excluded_from_bundle \
  tests.test_jarvis_regression_suite.RouterTests.test_email_compose_accepts_common_spoken_forms \
  tests.test_jarvis_regression_suite.RouterTests.test_bare_cancel_clears_pending_email_draft_without_sending \
  tests.test_jarvis_regression_suite.RouterTests.test_set_numeric_timer_phrase_uses_fast_path \
  tests.test_jarvis_regression_suite.RouterTests.test_weather_query_passes_requested_location \
  tests.test_jarvis_regression_suite.RouterTests.test_weather_tool_uses_orchestrator_location_param \
  tests.test_jarvis_regression_suite.RouterTests.test_reply_to_thread_command_wins_over_pending_relay_recipient \
  tests.test_jarvis_regression_suite.RouterTests.test_short_incoming_relay_stays_on_fast_path \
  tests.test_jarvis_regression_suite.RouterTests.test_search_query_bypasses_pending_email_draft \
  tests.test_message_intent_parsing tests.test_messages_contacts tests.test_jarvis_health -v
git diff --check
PYINSTALLER_CONFIG_DIR=/tmp/pyinstaller-jarvis-codex-auth scripts/install_jarvis_app.sh --applications-only
```

Packaged app verification on side port 8774 passed:
- `/status` online, open-source, local available.
- `write an email to beta@example.com saying Ship it` stages a Gmail draft.
- `cancel` cancels the draft.
- `set a 5 minute timer` returns `Timer set for 5 minutes.`
- `what is the weather in San Jose today?` returns San Jose weather.
- `find /Users/truthseeker/Applications/Jarvis.app -name token.json -o -name credentials.json -o -name .env` returned no bundled secrets.
- Latest side run selected `MacBook Pro Microphone` without AUHAL noise. Earlier side run reproduced `PaMacCore (AUHAL)` errors before recovering, so CoreAudio/PyAudio instability is improved by filtering virtual devices but not proven fully eliminated.

Current live runtime:
- Claude/source runtime is still on `127.0.0.1:8765` as `/Users/truthseeker/jarvis-ai/venv/bin/python main.py --no-ui`.
- Codex side-port packaged verifier on `8774` was stopped after verification.
