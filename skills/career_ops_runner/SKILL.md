Name: Career Ops Runner

Purpose:
Run career-ops Node.js scripts via the terminal_operator connector. Use this when the user wants to analyze rejection patterns, check pipeline health, merge tracker additions, or trigger other career-ops maintenance tasks.

Use When:
- the user asks to scan for new job offers
- the user wants to analyze rejection patterns or improve targeting
- the user wants to check pipeline integrity or health
- the user asks to merge tracker additions
- the user says "run career-ops", "check my pipeline", "analyze my rejections", or similar

Connector: terminal_operator
Career-ops project root: /Users/truthseeker/PycharmProjects/career-ops

Available Commands:

| Task | Command |
|------|---------|
| Analyze rejection patterns | cd /Users/truthseeker/PycharmProjects/career-ops && node analyze-patterns.mjs |
| Pipeline health check | cd /Users/truthseeker/PycharmProjects/career-ops && node verify-pipeline.mjs |
| Merge tracker additions | cd /Users/truthseeker/PycharmProjects/career-ops && node merge-tracker.mjs |
| Normalize statuses | cd /Users/truthseeker/PycharmProjects/career-ops && node normalize-statuses.mjs |
| Deduplicate tracker | cd /Users/truthseeker/PycharmProjects/career-ops && node dedup-tracker.mjs |

Rules:

SAFETY:
- NEVER run scans, submit applications, or trigger any write operation without explicit user instruction for that specific action.
- NEVER submit or send applications on the user's behalf. Always stop before any Submit/Send/Apply action. The user makes the final call.
- NEVER run destructive operations (dedup, normalize, merge) without confirming with the user first.
- If the user asks to "scan jobs", clarify whether they want to run the scan command or just check existing results — do not fire a scan automatically.

OUTPUT HANDLING:
- analyze-patterns.mjs outputs JSON. Parse it and summarize findings in plain language: which archetypes are being rejected, which signals correlate with low scores, and what targeting adjustments are recommended.
- verify-pipeline.mjs outputs health diagnostics. Surface any warnings or errors clearly.
- After running merge-tracker.mjs, confirm how many new entries were merged and flag any conflicts.

CONTEXT:
- Read /Users/truthseeker/PycharmProjects/career-ops/data/applications.md before interpreting pattern results to give grounded context (e.g., how many applications are in the tracker, which stages they're at).
- Cross-reference rejection patterns with Aman's scoring weights in /Users/truthseeker/PycharmProjects/career-ops/modes/_profile.md to explain whether rejections reflect targeting misalignment or something else.
