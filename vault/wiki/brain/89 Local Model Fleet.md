---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: repo
confidence: high
created: 2026-04-21
updated: 2026-04-21
version: 1
tags:
  - jarvis
  - local-first
  - ollama
  - training
  - models
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Local Skill Loop]]"
  - "[[80 Jarvis Roadmap]]"
  - "[[84 Frontier Capability Parity]]"
  - "[[86 Capability Eval Harness]]"
  - "[[87 Production Readiness Contract]]"
  - "[[88 Coder Workbench]]"
---

# Local Model Fleet

Purpose: keep Jarvis honest and operational about local models, free training lanes, and self-learning.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[79 Local Skill Loop]], [[80 Jarvis Roadmap]], [[84 Frontier Capability Parity]], [[86 Capability Eval Harness]], [[87 Production Readiness Contract]], [[88 Coder Workbench]]

Jarvis now exposes a model-fleet surface through:

- `/local/model-fleet`
- `jarvis --model-fleet`
- console `/model-fleet`
- router fast paths for local LLM, Ollama model, Google Colab, and free-training questions

## Contract

Jarvis should answer model and training questions from live fleet state, not from hype-thread memory.

The model-fleet layer should answer:

- which Ollama models are installed
- which configured roles are ready
- which next model is worth pulling
- why "download every model" is the wrong policy
- which free or low-cost training lanes are real
- which lanes are local runtime, external training lab, or gated self-learning

## Current Position

Do not download every model.

Install models by role, measure them, then promote only after evals:

- fast coder
- long-context coding agent
- reasoning model
- default local chat model
- vision model
- embedding model

Google Colab and Unsloth notebooks can be useful for LoRA, SFT, and GRPO experiments without renting a GPU server. They are not a 24/7 Jarvis host. Colab free resources are availability-limited and can terminate, so Jarvis should treat Colab as a training lab and keep production runtime on the local Mac daemon.

## Self-Learning Rule

Jarvis can learn safely through:

1. teacher examples
2. local training packs
3. optional external LoRA/GRPO experiments
4. local evals
5. explicit promotion gates

Jarvis should not silently mutate its production model or routing defaults just because a new training artifact exists.

## Product Direction

The next model-fleet upgrade should generate a Colab-ready handoff notebook from the existing Jarvis training pack, then import the trained adapter or converted model back into the local eval and promotion loop.
