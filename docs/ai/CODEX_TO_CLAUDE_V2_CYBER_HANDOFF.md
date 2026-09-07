# Codex to Claude: V2 Cyber Capability Program

Timestamp: 2026-09-06 (America/Los_Angeles)

Owner decision: every red-, blue-, and purple-team capability in
`docs/V2_CYBER_CAPABILITY_GATES.md` must earn at least 8/10 before V2 is called
an expert cyber coworker. The scores must come from reproducible local fixtures,
not model self-ratings or feature presence.

Codex's current lane:

- `jarvis_v2/security_tools.py`
- pluggable tool schemas in `jarvis_v2/agent.py` and `jarvis_v2/team.py`
- `tests/test_v2_security_tools.py`
- the cyber capability gates and migration/build evidence
- `jarvis_v2/cyber_eval.py`, `scripts/score_v2_cyber_eval.py`, and their tests

Implemented in this checkpoint:

- explicit expiring owner grants
- per-action authorization and grant digest in results
- workspace/symlink containment
- SHA-256 file evidence
- deterministic Python AST security checks
- per-agent security schema assignment; synthesis remains tool-free
- hash-chained evaluation records, critical-failure score caps, and three-run
  pinned-model promotion gates

Do not credit this slice with network recon, exploit validation, malware
analysis, log investigation, remediation, or continuous validation. Do not
change the standard Qwen production model or the default read-only profile.

Suggested non-overlapping Claude lane after this checkpoint lands: draft local,
synthetic evaluation fixtures and expected outcomes for threat modeling,
incident triage, Sigma/YARA rule design, and purple-team coverage mapping. Do
not add an unrestricted process executor or modify the files in Codex's current
lane without a new coordinator assignment.
