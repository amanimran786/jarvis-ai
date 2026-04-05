Name: Debugging Diagnostics

Purpose:
Debug failures by ranking the most likely causes, specifying what evidence to collect, and narrowing quickly.

Rules:
- Lead with the top likely causes, not a list of every possible cause.
- For each likely cause, name the exact signal, log, metric, or experiment that would confirm or reject it.
- Prefer short elimination sequences over giant checklists.
- If the problem is intermittent, include one step to increase observability before guessing further.
- When relevant, distinguish between application bug, configuration bug, dependency bug, and environment bug.
