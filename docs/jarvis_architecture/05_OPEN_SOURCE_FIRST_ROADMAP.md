# Jarvis Open-Source-First Roadmap

This document turns the current Jarvis repo into a concrete build plan for an open-source-first personal assistant that feels closer to "Jarvis" from Iron Man than a generic chat app.

The goal is not a single magic model. The goal is a local-first assistant runtime with strong tools, memory, multimodal perception, and a disciplined orchestration layer that can keep improving without being boxed in by one provider.

## Product Goal

Jarvis should become:

- a persistent local assistant runtime, not just a desktop chat window
- a personal operating layer across voice, screen, browser, files, devices, and memory
- open-source-first by default, with cloud models as optional augmentation instead of the foundation
- truthful about what it knows, what it inferred, and what it actually observed
- fast enough for daily use and structured enough to keep getting better over time

## Core Architecture Principles

### 1. Local First, Cloud Optional

Open models and local services should own the default path for chat, coding, speech, retrieval, and routing. Cloud providers should remain available for explicit escalation, not as the baseline requirement.

Implication for this repo:

- keep [model_router.py](/Users/truthseeker/jarvis-ai/model_router.py) policy-driven
- treat [brain_ollama.py](/Users/truthseeker/jarvis-ai/brain_ollama.py) as the default backbone
- push [voice.py](/Users/truthseeker/jarvis-ai/voice.py), [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py), and [camera.py](/Users/truthseeker/jarvis-ai/camera.py) toward local implementations

### 2. Runtime First, UI Second

The assistant should exist independently of the window. The UI is a client of the runtime, not the owner of the assistant state.

Implication for this repo:

- grow [jarvis_daemon.py](/Users/truthseeker/jarvis-ai/jarvis_daemon.py) from bootstrap thread into a real runtime process
- move more operational ownership out of [ui.py](/Users/truthseeker/jarvis-ai/ui.py)
- make [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py) the authoritative shared state surface

### 3. Skills Over Prompt Bloat

Jarvis should stay narrow at idle and become specialized only when needed. That means skill activation, graph grounding, and MCP domain activation on demand instead of giant always-loaded prompts.

Implication for this repo:

- keep [skills.py](/Users/truthseeker/jarvis-ai/skills.py) as the local skill registry
- keep [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py) for repo grounding
- extend the same pattern to MCP tool groups rather than loading everything up front

### 4. Grounding Beats Vibes

Jarvis should answer from observed data, repo structure, durable memory, and explicit tool results. Every important answer path should prefer grounded evidence over fluent guessing.

Implication for this repo:

- strengthen [memory.py](/Users/truthseeker/jarvis-ai/memory.py), [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py), [vault.py](/Users/truthseeker/jarvis-ai/vault.py), and [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py) into one layered retrieval system
- keep runtime metadata in [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py) visible to both UI and API
- continue pushing answer paths away from fabricated system-status claims

### 5. Multimodal Is Mandatory

The "Jarvis" experience requires more than text. Voice, screen, camera, browser, meeting audio, and device state are first-class inputs.

Implication for this repo:

- keep [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py), [camera.py](/Users/truthseeker/jarvis-ai/camera.py), [browser.py](/Users/truthseeker/jarvis-ai/browser.py), and [hardware.py](/Users/truthseeker/jarvis-ai/hardware.py) as major subsystems, not side features

### 6. Transparent Capability Boundaries

The product should feel powerful, but it must also be explicit about what it actually heard, saw, inferred, or could not verify. This matters for trust and for debugging.

Implication for this repo:

- preserve explicit status surfaces in [api.py](/Users/truthseeker/jarvis-ai/api.py)
- expand runtime diagnostics instead of hiding uncertainty
- do not optimize for concealment; optimize for legitimate, controllable assistive behavior

### 7. Evals Are a Product Feature

Jarvis should not get "smarter" by intuition alone. Improvements need replayable evals and promotion gates.

Implication for this repo:

- keep [local_model_eval.py](/Users/truthseeker/jarvis-ai/local_model_eval.py) and [local_beta.py](/Users/truthseeker/jarvis-ai/local_beta.py)
- make them more local, cheaper, and more representative of actual Jarvis tasks

## Open-Source Model Stack By Function

The right model stack is not one model for everything. Jarvis should use a small open stack with clear ownership by function.

| Function | Current Repo State | Recommended Open-Source Default | Why |
|---|---|---|---|
| General chat | `gemma4:e4b` via [config.py](/Users/truthseeker/jarvis-ai/config.py) | `Qwen2.5-7B-Instruct` or keep `gemma4:e4b` as fast fallback | Better quality per token for broad assistant chat while still runnable locally |
| Coding | `qwen2.5-coder:7b` | Keep `Qwen2.5-Coder-7B-Instruct` | Strong specialized code model with clear role separation |
| Reasoning | `gemma4:e4b` | Same as general local instruct model first | Avoid a fragmented stack unless reasoning evals prove a need |
| Routing | Heuristic policy in [model_router.py](/Users/truthseeker/jarvis-ai/model_router.py) | Keep rules first, add light local classifier only if needed | Routing should stay cheap, deterministic, and observable |
| STT | Cloud Whisper path in [voice.py](/Users/truthseeker/jarvis-ai/voice.py) and [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py) | `faster-whisper` | Strong local speech recognition with good quality and broad adoption |
| TTS | ElevenLabs/OpenAI in [voice.py](/Users/truthseeker/jarvis-ai/voice.py) | `Kokoro-82M` | Good local voice path that fits the open-source-first goal |
| OCR | Partial / provider-driven | `PaddleOCR` | Strong local OCR foundation for screen and document understanding |
| Vision understanding | GPT-4o path in [camera.py](/Users/truthseeker/jarvis-ai/camera.py) | `Qwen2.5-VL-7B-Instruct` | Local multimodal reasoning for screen/camera snapshots |
| Embeddings | TF-IDF in [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py) | `bge-small-en-v1.5` or `bge-base-en-v1.5` | Better retrieval quality without requiring a vector database-heavy architecture |
| Reranking | None | `bge-reranker-base` | Improves grounded answer quality for vault and memory retrieval |
| Judge/eval model | Cloud-heavy in [local_model_eval.py](/Users/truthseeker/jarvis-ai/local_model_eval.py) | local rubric + pairwise scoring first | Keeps eval loops cheaper and aligned with the open-source goal |

### External References

- [Qwen2.5-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct)
- [Qwen2.5-Coder-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct)
- [Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)
- [BGE reranker](https://huggingface.co/BAAI/bge-reranker-base)

## Must-Have Subsystems

These are the core subsystems required to reach the target product. Each section shows what already exists and what is still missing.

### 1. Assistant Runtime

Existing:

- [main.py](/Users/truthseeker/jarvis-ai/main.py)
- [jarvis_daemon.py](/Users/truthseeker/jarvis-ai/jarvis_daemon.py)
- [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py)
- [api.py](/Users/truthseeker/jarvis-ai/api.py)

Still needed:

- a true long-lived daemon process
- event bus or state subscription model instead of ad hoc polling and shared globals
- clean separation between runtime lifecycle and UI lifecycle

### 2. Desktop and Remote Clients

Existing:

- [ui.py](/Users/truthseeker/jarvis-ai/ui.py)
- [overlay.py](/Users/truthseeker/jarvis-ai/overlay.py)
- [device_panel.py](/Users/truthseeker/jarvis-ai/device_panel.py)
- [bridge.py](/Users/truthseeker/jarvis-ai/bridge.py)

Still needed:

- UI decomposition so the desktop shell is not a monolith
- same-Wi-Fi phone/browser client
- consistent client behavior between source runs and packaged app runs

### 3. Tool and Action Layer

Existing:

- [router.py](/Users/truthseeker/jarvis-ai/router.py)
- [orchestrator.py](/Users/truthseeker/jarvis-ai/orchestrator.py)
- [skills.py](/Users/truthseeker/jarvis-ai/skills.py)
- [browser.py](/Users/truthseeker/jarvis-ai/browser.py)
- [hardware.py](/Users/truthseeker/jarvis-ai/hardware.py)
- [terminal.py](/Users/truthseeker/jarvis-ai/terminal.py)

Still needed:

- MCP skill-dispatch rather than broad tool exposure
- tighter verification around multi-step action chains
- clearer distinction between read-only, write, and privileged actions

### 4. Memory and Grounding

Existing:

- [memory.py](/Users/truthseeker/jarvis-ai/memory.py)
- [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py)
- [vault.py](/Users/truthseeker/jarvis-ai/vault.py)
- [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py)
- [scripts/build_graphify_repo.py](/Users/truthseeker/jarvis-ai/scripts/build_graphify_repo.py)

Still needed:

- one retrieval pipeline that composes facts, vault entries, graph context, and conversation context coherently
- embedding-backed retrieval and reranking
- a scheduled consolidation path for durable memory growth and cleanup

### 5. Multimodal Perception

Existing:

- [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py)
- [voice.py](/Users/truthseeker/jarvis-ai/voice.py)
- [camera.py](/Users/truthseeker/jarvis-ai/camera.py)
- [browser.py](/Users/truthseeker/jarvis-ai/browser.py)

Still needed:

- local STT
- local OCR
- local screen/camera vision
- more reliable caption and call-audio fusion

### 6. Device and Environment Awareness

Existing:

- [hardware.py](/Users/truthseeker/jarvis-ai/hardware.py)
- [bridge.py](/Users/truthseeker/jarvis-ai/bridge.py)
- [device_panel.py](/Users/truthseeker/jarvis-ai/device_panel.py)

Still needed:

- intentional cross-device session handoff
- explicit remote-control flows over trusted LAN
- persistent device state and action history

### 7. Self-Improvement and Evaluation

Existing:

- [self_improve.py](/Users/truthseeker/jarvis-ai/self_improve.py)
- [local_model_eval.py](/Users/truthseeker/jarvis-ai/local_model_eval.py)
- [local_beta.py](/Users/truthseeker/jarvis-ai/local_beta.py)

Still needed:

- more realistic golden tasks for meetings, memory, device actions, and screen analysis
- lower-cost local eval loops
- hard safety boundaries around self-editing and promotion

## Phased Roadmap

This roadmap is ordered to maximize leverage and reduce breakage in the current repo.

### Phase 1: Runtime Hardening

Objective:

Turn Jarvis into a stable runtime that the UI talks to, instead of a UI that owns the assistant.

Primary work:

- grow [jarvis_daemon.py](/Users/truthseeker/jarvis-ai/jarvis_daemon.py) into a real runtime service
- make [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py) the authoritative state model
- keep [api.py](/Users/truthseeker/jarvis-ai/api.py) as the runtime surface for both desktop and future remote clients
- continue moving operational logic out of [ui.py](/Users/truthseeker/jarvis-ai/ui.py)

Exit criteria:

- Jarvis can keep running when the UI closes or restarts
- packaged app and source run share the same behavior
- all current UI surfaces read from runtime state instead of private window state

### Phase 2: Open-Source Default Brain

Objective:

Make the default assistant path open-source-first across chat, coding, speech, and retrieval.

Primary work:

- formalize the open stack in [config.py](/Users/truthseeker/jarvis-ai/config.py) and [model_router.py](/Users/truthseeker/jarvis-ai/model_router.py)
- replace cloud STT/TTS in [voice.py](/Users/truthseeker/jarvis-ai/voice.py) and [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py)
- upgrade [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py) from TF-IDF-only retrieval to embeddings + reranking
- keep cloud providers as opt-in escalation, not the baseline requirement

Exit criteria:

- chat, coding, STT, TTS, and core retrieval all have viable local defaults
- Jarvis remains useful when cloud keys are absent
- quality regressions are measured, not guessed

### Phase 3: Unified Memory and Grounded Reasoning

Objective:

Make Jarvis answer from durable knowledge and current evidence instead of raw prompt accumulation.

Primary work:

- unify [memory.py](/Users/truthseeker/jarvis-ai/memory.py), [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py), [vault.py](/Users/truthseeker/jarvis-ai/vault.py), and [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py)
- add scheduled memory consolidation and cleanup
- strengthen answer provenance and confidence reporting

Exit criteria:

- memory retrieval is layered and explainable
- repo questions, project questions, and personal-context questions all route through the same grounded retrieval spine
- answer quality improves while prompt size decreases

### Phase 4: Multimodal Copilot

Objective:

Make Jarvis genuinely useful during calls, browser work, screen review, and device control.

Primary work:

- harden [meeting_listener.py](/Users/truthseeker/jarvis-ai/meeting_listener.py) for reliable speech/caption fusion
- add local OCR and local VLM support to [camera.py](/Users/truthseeker/jarvis-ai/camera.py) and screen analysis flows
- improve [browser.py](/Users/truthseeker/jarvis-ai/browser.py) for explicit current-task grounding
- expand [hardware.py](/Users/truthseeker/jarvis-ai/hardware.py) and [bridge.py](/Users/truthseeker/jarvis-ai/bridge.py) into real cross-device assist primitives

Exit criteria:

- Jarvis can hear, see, and act locally with acceptable latency
- meeting assist is reliable enough for daily work
- screen and device workflows feel like first-class product features

### Phase 5: Skillful Tooling and Coordinator Runtime

Objective:

Move from "one request in, one answer out" toward coordinated multi-step work.

Primary work:

- evolve [orchestrator.py](/Users/truthseeker/jarvis-ai/orchestrator.py) into a stronger coordinator runtime
- add MCP skill activation so only relevant tool groups load per task
- support safe multi-step tool execution and verification

Exit criteria:

- Jarvis can plan, execute, verify, and summarize multi-step tasks more reliably
- tool bloat is reduced
- the assistant has clearer behavior across repo work, browser work, memory work, and device work

### Phase 6: Remote Bridge and Ambient Assistant

Objective:

Make Jarvis available across the desktop, phone, and trusted local network.

Primary work:

- build a real same-Wi-Fi remote client on top of [api.py](/Users/truthseeker/jarvis-ai/api.py) and [bridge.py](/Users/truthseeker/jarvis-ai/bridge.py)
- support session handoff and current-task continuity
- turn the assistant into an always-available runtime instead of a window you manually shepherd

Exit criteria:

- Jarvis is controllable from another device on trusted LAN
- current context and tasks can move between surfaces cleanly
- the product feels like an ambient assistant, not a fragile desktop script

## What To Build Next In This Repo

The next highest-leverage milestone is not a new model. It is a cleaner runtime boundary.

### Immediate Next Build

1. Expand [jarvis_daemon.py](/Users/truthseeker/jarvis-ai/jarvis_daemon.py) into a true runtime owner.
2. Keep moving meeting, bridge, and device ownership out of [ui.py](/Users/truthseeker/jarvis-ai/ui.py).
3. Make [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py) the only state surface the UI and compact toolbar trust.
4. Add a dedicated remote client entrypoint after runtime parity is stable.

Why this first:

- it reduces the number of bugs caused by UI-specific state
- it makes packaging and source runs behave the same
- it unlocks remote bridge, Auto-Dream, and a real coordinator later
- it gives every later subsystem a stable place to live

## Success Criteria

Jarvis is on the right track when:

- the app is useful without cloud keys
- the runtime survives UI restarts
- meeting assist, browser assist, and memory grounding all work through one coherent runtime
- answers distinguish observed facts, retrieved evidence, and inference
- new capabilities arrive as modular subsystems instead of prompt sprawl
- the open-source path gets better over time without requiring a complete rewrite

## One-Line Direction

Build Jarvis as a persistent local assistant runtime with modular open-source perception, memory, skills, and device control, then let clients, bridges, and optional cloud escalation sit on top of that core instead of defining it.
