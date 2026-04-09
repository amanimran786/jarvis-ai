# Graph Report - /Users/truthseeker/jarvis-ai  (2026-04-08)

## Corpus Check
- 154 files · ~120,462 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1659 nodes · 2959 edges · 40 communities detected
- Extraction: 63% EXTRACTED · 37% INFERRED · 0% AMBIGUOUS · INFERRED: 1109 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## God Nodes (most connected - your core abstractions)
1. `JarvisWindow` - 48 edges
2. `route_stream()` - 36 edges
3. `OrbShellWindow` - 35 edges
4. `RouterTests` - 33 edges
5. `answer_for_query()` - 29 edges
6. `MemoryModuleTests` - 28 edges
7. `ConversationContextTests` - 25 edges
8. `MeetingOverlay` - 24 edges
9. `_fallback_role_output()` - 21 edges
10. `_listen_loop()` - 17 edges

## Surprising Connections (you probably didn't know these)
- `VoiceWorker` --inherits--> `QThread`  [EXTRACTED]
  /Users/truthseeker/jarvis-ai/ui.py →   _Bridges community 6 → community 1_
- `Device` --inherits--> `ABC`  [EXTRACTED]
  /Users/truthseeker/jarvis-ai/hardware.py →   _Bridges community 24 → community 4_

## Communities

### Community 0 - "Test Jarvis Regression Suite"
Cohesion: 0.01
Nodes (25): ApiSurfaceTests, BenchmarkCoverageTests, BrowserMeetingCaptionTests, CallPrivacyTests, CostPolicyTests, GraphContextTests, InterviewProfileTests, LiveAssistRenderingTests (+17 more)

### Community 1 - "Ui / Overlay"
Cohesion: 0.03
Nodes (49): PulseDot, QFrame, QLineEdit, QObject, QWidget, _activate_macos_app(), _apply_macos_identity(), ArcReactor (+41 more)

### Community 2 - "Model Router / Usage Tracker"
Cohesion: 0.03
Nodes (87): ask(), ask_stream(), ask_claude(), ask_claude_stream(), Remove markdown artifacts because Jarvis responses are spoken aloud., _strip_markdown(), ask_gemini(), ask_gemini_stream() (+79 more)

### Community 3 - "Test Unit Coverage"
Cohesion: 0.02
Nodes (18): BehaviorHooksProtectedPathTests, tests/test_unit_coverage.py  Comprehensive unit tests for pure-Python Jarvis mod, Tests for write-then-retrieve cycle using isolated temp directories., SemanticMemoryFormattingTests, SemanticMemoryRetrievalTests, SemanticMemoryStatusTests, SemanticMemoryWriteTests, TerminalBlockedPatternTests (+10 more)

### Community 4 - "Hardware / Bridge"
Cohesion: 0.04
Nodes (53): bridge_enabled(), bridge_snapshot(), bridge_urls(), primary_bridge_url(), refresh_bridge_status(), nearby_summary(), refresh_nearby_devices(), auto_connect_serial() (+45 more)

### Community 5 - "Api"
Cohesion: 0.05
Nodes (43): bridge_status(), build_vault(), chat(), ChatRequest, FactRequest, FeedbackRequest, _find_free_port(), ForgetRequest (+35 more)

### Community 6 - "Overlay / Meeting Controller"
Cohesion: 0.05
Nodes (34): current_meeting_label(), refresh_status(), _all_process_names(), _browser_active_meeting_label(), _browser_any_meeting_label(), _compute_meeting_app(), detect_meeting_app(), get_overlay() (+26 more)

### Community 7 - "Meeting Listener"
Cohesion: 0.07
Nodes (58): _activate_source(), auto_configure_blackhole(), _build_source_candidates(), _device_name(), _fallback_suggestion_text(), _generate_suggestion(), get_blackhole_device(), get_preferred_microphone_device() (+50 more)

### Community 8 - "Skills / Orchestrator"
Cohesion: 0.06
Nodes (52): _execute_step(), _plan_steps(), Jarvis Operative Agent — autonomous multi-step task execution.  The operative ta, Replace $step_N_result placeholders with actual outputs., Execute a single step. Returns (ok, result_text)., Execute a multi-step task autonomously.      Args:         task:        Natural, Run task in background. Calls on_complete(result) when done., Use Sonnet to break the task into executable steps. (+44 more)

### Community 9 - "Router / Prompt Modifiers"
Cohesion: 0.07
Nodes (49): ModifierResult, parse(), _parse_parameterized(), _parse_role_task_format(), _parse_simple_prefix(), Scoped prompt modifiers for Jarvis.  These are request-local controls like:   EL, _strip_command_prefix(), _clear_pending_recipient() (+41 more)

### Community 10 - "Browser"
Cohesion: 0.09
Nodes (52): _app_exists(), _browser_command(), _choose_browser(), _clean_browser_js_error(), click_text(), copy_current_page_url(), _escape_applescript(), _execute_tab_js() (+44 more)

### Community 11 - "Vault / Skill Factory"
Cohesion: 0.08
Nodes (44): create_skill_from_vault(), _load_index(), promote_failures(), Promote stable vault knowledge or repeated eval patterns into local skills., _restore_skill_state(), _save_index(), _slugify(), _snapshot_skill_state() (+36 more)

### Community 12 - "Interview Profile"
Cohesion: 0.1
Nodes (43): _active_pack_id(), answer_for_query(), _available_pack_ids(), behavioral_story_text(), canonical_profile_text(), _clean_markdown_excerpt(), data_story_text(), enforcement_decision_text() (+35 more)

### Community 13 - "Learner / Tools"
Cohesion: 0.06
Nodes (30): _capture_frame(), Capture a single frame from the webcam and return path to saved image., Capture a webcam frame and ask GPT-4o Vision to describe it.     prompt: what to, Take a screenshot and describe it using Vision API., screenshot_and_describe(), see(), extract_and_learn(), get_learning_context() (+22 more)

### Community 14 - "Test Unit Coverage"
Cohesion: 0.08
Nodes (3): MemoryModuleTests, NotesModuleTests, Tests for memory.py — isolated using a per-test temporary file.

### Community 15 - "Self Improve"
Cohesion: 0.08
Nodes (36): analyze_weakness(), apply_improvement(), _backup(), _diff(), _extract_syntax_error_line(), generate_improvement(), _heuristic_comment_fix(), list_backups() (+28 more)

### Community 16 - "Test Unit Coverage"
Cohesion: 0.08
Nodes (4): BehaviorHooksFileWriteTests, BehaviorHooksSelfImproveTests, BehaviorHooksShellGatingTests, BehaviorHooksSummaryTests

### Community 17 - "Test Jarvis Live Integrations / Google Services"
Cohesion: 0.07
Nodes (22): _calendar(), create_event(), _drive(), _extract_drive_file_id(), _get_creds(), get_drive_file_text(), get_todays_events(), get_unread_emails() (+14 more)

### Community 18 - "Terminal / Behavior Hooks"
Cohesion: 0.09
Nodes (30): _is_protected_path(), _normalize_path(), _now_iso(), post_file_write(), post_shell_command(), pre_file_write(), pre_self_improve(), pre_shell_command() (+22 more)

### Community 19 - "Evals / Local Beta"
Cohesion: 0.14
Nodes (28): build_improvement_brief(), _choose_target_file(), classify_failure(), _find_interaction(), _is_resolved_by_later_success(), load(), _load_unlocked(), log_failure() (+20 more)

### Community 20 - "Local Training"
Cohesion: 0.16
Nodes (28): _build_axolotl_yaml(), _build_distill_prompt(), _build_expert_distill_prompt(), build_finetune_handoff(), _build_handoff_readme(), build_modelfile(), build_training_pack(), _build_unsloth_script() (+20 more)

### Community 21 - "Specialized Agents"
Cohesion: 0.12
Nodes (27): AgentSpec, _auth_security_fallback(), choose_roles(), _entropy_fallback(), _explicit_roles(), _fallback_role_output(), _fastapi_502_executor_fallback(), _fastapi_502_planner_fallback() (+19 more)

### Community 22 - "Semantic Memory / Jarvis Beta"
Cohesion: 0.1
Nodes (27): _build_context(), jarvis_beta.py — headless beta test harness for Jarvis.  Bypasses macOS-only imp, Assemble context from:       1. Core facts from memory.json       2. Semantic KB, Simple display-only routing label., _route_label(), run(), _build_index(), context_for_query() (+19 more)

### Community 23 - "Source Ingest / Notes"
Cohesion: 0.18
Nodes (26): add_note(), get_notes(), _load(), _save(), search_notes(), _candidate_repo_docs(), _extract_pdf_bytes(), _extract_pdf_file() (+18 more)

### Community 24 - "Agents"
Cohesion: 0.11
Nodes (10): ABC, Agent, EmailAgent, IdleContextAgent, MeetingPrepAgent, Jarvis Proactive Agent System.  Agents run continuously in the background and su, Start all agents in a single background thread.     on_alert(title: str, body: s, ResearchAgent (+2 more)

### Community 25 - "Runtime State / Jarvis Daemon"
Cohesion: 0.11
Nodes (19): Start the local API daemon once and record basic runtime state., _resolve_host_port(), start_daemon(), _wait_for_api_ready(), _install_crash_logging(), _resolve_api_port(), _run(), _run_headless() (+11 more)

### Community 26 - "Memory"
Cohesion: 0.21
Nodes (24): add_fact(), add_project(), _build_long_term_profile(), _build_working_memory(), consolidate_memory(), _conversation_focus(), _dedupe_keep_order(), forget() (+16 more)

### Community 27 - "Test Unit Coverage"
Cohesion: 0.08
Nodes (1): ConversationContextTests

### Community 28 - "Voice / Call Privacy"
Cohesion: 0.12
Nodes (20): is_enabled(), _meeting_label(), should_suppress_audio(), snapshot(), status_text(), _get_eleven(), listen(), Speak a streaming response sentence by sentence.     Plays each sentence as soon (+12 more)

### Community 29 - "Local Model Eval"
Cohesion: 0.23
Nodes (17): benchmark_cases(), _ensure_dirs(), _failure_cases(), _judge_answer(), _judge_prompt(), _load_eval_result(), _load_state(), _normalize_case_id() (+9 more)

### Community 30 - "Test Unit Coverage"
Cohesion: 0.23
Nodes (1): UsageTrackerRecordTests

### Community 31 - "Graph Context"
Cohesion: 0.28
Nodes (13): context_for_query(), _extract_summary_lines(), _link_key(), _load_payload(), _looks_identifier(), _node_text(), _path_label(), _query_is_repo_related() (+5 more)

### Community 32 - "Test Jarvis Regression Suite"
Cohesion: 0.17
Nodes (1): SkillAndAgentTests

### Community 33 - "Hotkeys"
Cohesion: 0.29
Nodes (8): _fire(), _normalize(), _on_press(), _on_release(), Global hotkey system for Jarvis. Works system-wide — even during Zoom/Teams call, Normalize key for consistent matching., Start the global hotkey listener in a background thread., start()

### Community 34 - "Briefing"
Cohesion: 0.38
Nodes (6): build_briefing(), _greeting(), Return True if enough time has passed since the last session., Assemble a briefing intro. The actual calendar/email/weather     content is fetc, _save_session(), should_brief()

### Community 35 - "Jarvis Cli"
Cohesion: 0.6
Nodes (3): get(), main(), post()

### Community 36 - "Build Graphify Repo"
Cohesion: 0.7
Nodes (4): build_graph(), _label_communities(), main(), _tokenize()

### Community 37 - "Train Unsloth"
Cohesion: 1.0
Nodes (0): 

### Community 38 - "Agents"
Cohesion: 1.0
Nodes (1): Return (title, body, speak_aloud) or None if nothing to surface.

### Community 39 - "Jarvis Golden Cases"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **201 isolated node(s):** `Cost-aware routing and local-model improvement policy for Jarvis.  This module t`, `Decide whether to keep the base routing tier or bias cheaper based on     recent`, `Promote stable vault knowledge or repeated eval patterns into local skills.`, `Jarvis Meeting Overlay — floating HUD toolbar for live calls.  Invisible to scre`, `Return the best-known active meeting app label without blocking the UI by defaul` (+196 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Train Unsloth`** (2 nodes): `train_unsloth.py`, `format_messages()`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Agents`** (1 nodes): `Return (title, body, speak_aloud) or None if nothing to surface.`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Jarvis Golden Cases`** (1 nodes): `jarvis_golden_cases.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `JarvisWindow` connect `Ui / Overlay` to `Overlay / Meeting Controller`?**
  _High betweenness centrality (0.037) - this node is a cross-community bridge._
- **Are the 35 inferred relationships involving `route_stream()` (e.g. with `_s()` and `_clear_pending_recipient()`) actually correct?**
  _`route_stream()` has 35 INFERRED edges - model-reasoned connections that need verification._
- **Are the 28 inferred relationships involving `answer_for_query()` (e.g. with `supported_role_families_text()` and `is_target_role_pack_query()`) actually correct?**
  _`answer_for_query()` has 28 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Cost-aware routing and local-model improvement policy for Jarvis.  This module t`, `Decide whether to keep the base routing tier or bias cheaper based on     recent`, `Promote stable vault knowledge or repeated eval patterns into local skills.` to the rest of the system?**
  _201 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Test Jarvis Regression Suite` be split into smaller, more focused modules?**
  _Cohesion score 0.01 - nodes in this community are weakly interconnected._
- **Should `Ui / Overlay` be split into smaller, more focused modules?**
  _Cohesion score 0.03 - nodes in this community are weakly interconnected._
- **Should `Model Router / Usage Tracker` be split into smaller, more focused modules?**
  _Cohesion score 0.03 - nodes in this community are weakly interconnected._