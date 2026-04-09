# Career Application States

Adapted for Jarvis from the open-source Career-Ops project by santifer:
https://github.com/santifer/career-ops

Use these as the canonical status labels when Jarvis helps Aman track job applications.

## States

- Evaluated
  Offer or job description evaluated, but not yet applied

- Applied
  Application submitted

- Responded
  Company replied, but the process is not yet in active interviews

- Interview
  Active interview process underway

- Offer
  Offer received

- Rejected
  Rejected by company

- Discarded
  Closed or intentionally dropped by Aman

- Skip
  Not worth applying to; keep for awareness only

## Rules

- status labels should stay normalized
- dates should live separately from the status itself
- Jarvis should not invent a status change without a clear user instruction or explicit evidence
- low-fit roles should usually stay in `Skip` or `Discarded`, not be encouraged by default

