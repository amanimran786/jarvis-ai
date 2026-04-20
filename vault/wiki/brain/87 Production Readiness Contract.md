---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-20
updated: 2026-04-20
version: 1
tags:
  - jarvis
  - production
  - local-first
  - reliability
related:
  - "[[80 Jarvis Roadmap]]"
  - "[[84 Frontier Capability Parity]]"
  - "[[86 Capability Eval Harness]]"
  - "[[85 Defensive Security ROE]]"
---

# Production Readiness Contract

Purpose: keep Jarvis honest about whether it is production-ready, locally free, and capable for a given request class.

Linked notes: [[80 Jarvis Roadmap]], [[84 Frontier Capability Parity]], [[86 Capability Eval Harness]], [[85 Defensive Security ROE]]

Jarvis now exposes readiness through `/production-readiness` and the console command `/production-readiness`.

The answer to "is Jarvis 100% production-ready and free regardless of request?" is intentionally **no**.

That is not a failure of the local-first build. It is the correct operating contract:

- no local assistant can satisfy every possible request for free
- some tasks require live internet, third-party accounts, unavailable tools, or explicit permissions
- unsafe or unauthorized requests must be blocked or converted into defensive guidance
- local output quality is bounded by installed models, context, hardware, and verification depth
- zero API cost applies to the default local/open-source core path, not to optional services a user explicitly chooses

## Readiness Layers

### Local Daily Core

The local daily core is ready only when these surfaces are live:

- local model routing
- local coding-agent lane
- voice input and output
- local vision
- vault and semantic memory
- managed smart agents
- terminal console
- packaged macOS app
- safety and permission contract
- capability eval harness
- zero-cost default route

### Production Go-Live Gates

Jarvis should not call itself fully production-ready until these gates are handled:

- installed console and packaged app stay synced
- packaged voice/UI smoke verifies mic capture, STT text, and audible TTS
- recurring live golden suite runs against console, memory, voice, vision, coding, and safety
- brain and runtime backup/restore has a tested drill
- crash recovery and runtime observability are explicit
- macOS permission onboarding makes failures actionable

## Rule

Capability parity is not the same as production readiness.

When asked about readiness, Jarvis must lead with the truthful boundary, then name the current local-core state and next seam.
