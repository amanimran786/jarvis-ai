# Eval Pattern: self_improve

Source file: `raw/eval-self-improve-patterns.md`

## Summary
Eval Pattern: self_improve Related brain notes: [[78 AI Runtime Agent Engineering Principles]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]], [[91 Vault Changelog]], [[93 Vault Maintenance]] 423574acb974 Issue: Self-improve should only act when there is enough recent failure evidence. Expected: It should require recent eval evidence before proposing changes.

## Key Terms
improve, should, self, recent, evidence, eval, jarvis, vault, brain, runtime, decision, log

## Citation Map
- Eval Pattern: self_improve at line 1: Related brain notes: [[78 AI Runtime Agent Engineering Principles]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]], [[91 Vault Changelog]], [[93 Vault Maintenance]]
- 423574acb974 at line 5: Issue: Self-improve should only act when there is enough recent failure evidence.
- Why This Belongs In The Brain at line 11: - supports the local-first guardrail that self-improve should be evidence-gated, not impulse-driven - reinforces the runtime principle that maintenance and mutation should stay explicit, bounded, and reviewable - should be referenced when logging future self-improve policy changes in [[70 Jarvis Decision Log]] or [[91 Vault Changelog]]
