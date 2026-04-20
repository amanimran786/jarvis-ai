# Defensive Security ROE Templates

Use these templates to keep security work useful, bounded, and defensive.

## Authorization And Scope Gate

Require:
- written authorization or clear owned-system context
- target assets and boundaries
- allowed actions and prohibited actions
- data handling and credential rules
- stop condition and escalation owner

Output:
- whether the task is in scope
- what scope detail is missing
- safe defensive analysis if active testing is not authorized

## Threat Model

Require:
- assets
- actors
- entry points
- trust boundaries
- abuse paths
- existing controls
- missing controls
- verification plan

Output:
- highest-impact abuse path first
- confirmed risks versus assumptions
- smallest control that changes the outcome

## Code Security Review

Check:
- authn/authz boundary
- input validation and parsing
- secret handling
- path traversal and file write gates
- SSRF and outbound network controls
- injection and deserialization risks
- audit logging and rate limits

Output:
- findings first, ranked by exploitability and impact
- exact file/function references when available
- tests or probes that prove the risk

## Security Incident Triage

Require:
- signal and source
- blast radius
- affected assets/users
- timeline
- containment option
- evidence preservation
- communications owner

Output:
- severity from evidence
- contain, eradicate, recover, and learn steps
- first reversible action

## AI Misuse And Prompt Injection

Check:
- trusted versus untrusted inputs
- tool permission boundary
- memory read/write boundary
- prompt-injection path
- data exfiltration path
- classifier or policy gap
- eval case that reproduces the failure

Output:
- override path
- model-boundary, policy-boundary, or tooling-boundary failure
- control plus eval

## Browser And Source Ingestion Gate

Require:
- user consent
- terms/robots constraints
- credential boundary
- rate limit
- data minimization
- storage and deletion rule
- human approval for risky actions

Output:
- read-only inspection first
- no bypassing access controls or anti-abuse systems
- provenance for anything stored in memory or vault
