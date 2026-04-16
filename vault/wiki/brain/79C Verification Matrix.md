---
type: verification
area: engineering
owner: jarvis
write_policy: curated
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - verification
  - testing
  - release
related:
  - "[[79 Coding Implementation Playbook]]"
  - "[[79A Code Review Regression Heuristics]]"
  - "[[79B Jarvis Architecture Runtime Seams]]"
  - "[[80 Jarvis Roadmap]]"
---

# Verification Matrix

Purpose: define what “done” means for different Jarvis change types.

Linked notes: [[79 Coding Implementation Playbook]], [[79A Code Review Regression Heuristics]], [[79B Jarvis Architecture Runtime Seams]], [[80 Jarvis Roadmap]]

## Change Type → Expected Proof

- vault/docs structure
  - refresh vault index
  - validate links or canvas JSON when touched
  - do not rebuild the app just for ceremony

- routing or specialist-agent logic
  - targeted regression tests around the exact route
  - prove explicit requests and automatic routing both behave correctly when changed

- native vault/operator hooks
  - focused native-path regression tests
  - safety-boundary tests for protected or admin-like behavior

- technical grounding or answer-shaping
  - targeted model-router tests
  - prove technical prompts change and nontechnical prompts do not

- packaged-app behavior
  - rebuild installed app
  - run installed-bundle smoke
  - if the bug was in launch, voice, or runtime status, prefer the packaged surface over source-tree confidence

## Default Verification Rule

Run the narrowest check that can prove the changed seam works. Widen only when the seam crosses packaging, voice, or live runtime behavior.
