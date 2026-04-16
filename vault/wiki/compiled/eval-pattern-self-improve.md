# Eval Pattern: self_improve

Source file: `raw/eval-self-improve-patterns.md`

Connected notes: [[78 AI Runtime Agent Engineering Principles]], [[70 Jarvis Decision Log]], [[80 Jarvis Roadmap]], [[91 Vault Changelog]], [[93 Vault Maintenance]]

## Summary
Eval Pattern: self_improve 423574acb974 Issue: Self-improve should only act when there is enough recent failure evidence. Expected: It should require recent eval evidence before proposing changes.

## Key Terms
recent, eval, improve, evidence, self, should, enough, pattern, 423574acb974, issue, only, act

## Citation Map
- 423574acb974 at line 3: Issue: Self-improve should only act when there is enough recent failure evidence.

## Why This Matters

- reinforces the runtime rule that self-improve needs explicit failure evidence before it mutates behavior
- belongs next to [[78 AI Runtime Agent Engineering Principles]] because it is a concrete eval example of bounded agent behavior
- should inform future policy updates logged in [[70 Jarvis Decision Log]] and maintenance follow-up tracked through [[93 Vault Maintenance]]
