# Eval Pattern: self_improve

Related brain notes: [[78 AI Runtime Agent Engineering Principles]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]], [[91 Vault Changelog]], [[93 Vault Maintenance]]

## 423574acb974
Issue: Self-improve should only act when there is enough recent failure evidence.
Expected: It should require recent eval evidence before proposing changes.
User input: improve yourself
Response: Analyzing my own code and generating improvements. This will take a moment... Analysis complete: Not enough recent eval evidence. Need at least 2 recent logged failures.

## Why This Belongs In The Brain

- supports the local-first guardrail that self-improve should be evidence-gated, not impulse-driven
- reinforces the runtime principle that maintenance and mutation should stay explicit, bounded, and reviewable
- should be referenced when logging future self-improve policy changes in [[70 Jarvis Decision Log]] or [[91 Vault Changelog]]
