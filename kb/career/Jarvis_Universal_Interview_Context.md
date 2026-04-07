# JARVIS — AMAN IMRAN: UNIVERSAL INTERVIEW INTELLIGENCE SYSTEM
# Version 2.1 — Part of Jarvis Architecture
#
# ARCHITECTURE POSITION:
#   This file = kb/career/universal_base (loaded by InterviewIntel module)
#   Pointer:    Jarvis/kb/career/universal_base.md
#   Loaded by:  03_CAPABILITY_MODULES.md → InterviewIntel → context_requirements
#   Layer:      Career Knowledge Base (supplementary to L0 identity)
#   Privacy:    Public
#
# USAGE:
#   - Always loaded when InterviewIntel module activates
#   - Parts 1–9 are universal (role-agnostic) — load always
#   - Part 10+ are target role packs — load only when JARVIS_ACTIVE_ROLE matches
#   - For new role packs: use kb/career/packs/_template.md and append below
#   - Do NOT edit the universal base to be role-specific
#
# RELATED FILES:
#   Jarvis/00_ARCHITECTURE.md       — Full system design
#   Jarvis/03_CAPABILITY_MODULES.md — InterviewIntel module spec
#   Jarvis/kb/core/identity.md      — L0 identity (always loaded)
#   Jarvis/kb/career/packs/         — All role packs

---

## HOW THIS SYSTEM WORKS

This document has two layers:

**Layer 1 — Universal Base Profile (Parts 1–9)**
Everything about Aman that is true regardless of which role he is interviewing for.
His stories, his frameworks, his skills, his voice, his values. Jarvis uses this layer
for every interview, at every company, in every domain.

**Layer 2 — Target Role Packs (Part 10+)**
Role-specific context appended when Aman is interviewing for a specific position.
Each pack contains: what this role cares about, how to map Aman's experience to it,
company-specific talking points, and questions unique to that role.

**HOW JARVIS SHOULD USE THIS:**
1. Always load the Universal Base Profile first — this is your default operating context
2. Check if a Target Role Pack exists for the role being prepared for
3. If yes: activate that pack and use it to filter and prioritize which universal stories
   and frameworks to lead with — but never replace the universal layer
4. If no Target Role Pack exists: answer using the universal layer alone, mapping
   Aman's most relevant experience to what the role requires

**Adding a new role:** Append a new Target Role Pack at the bottom of this file
following the template provided. The universal base never changes — only packs are added.

---

# =================================================================
# LAYER 1: UNIVERSAL BASE PROFILE
# =================================================================

---

## PART 1: WHO AMAN IS

**Full name:** Aman Imran
**Domain expertise:** Trust & Safety, AI Safety, content policy, quality operations,
cross-functional program management
**Total experience:** 5+ years
**Education:** San Jose State University
**Based in:** San Francisco Bay Area

### Universal one-paragraph bio
"I'm a Trust & Safety and AI Safety professional with over five years of experience
building the operational systems that make enforcement actually work at scale — not just
enforcement decisions, but the calibration programs, quality frameworks, tooling, and
cross-functional infrastructure behind them. I've worked across YouTube, Meta, TikTok,
Google Play, and Anthropic. I started in the highest-stakes enforcement category you can
work in — child safety — and built from there into quality systems, data operations, and
AI safety. I combine enforcement judgment, data fluency, and a systems thinker's instinct.
I don't just find problems — I find the root cause, build the fix, and make sure it
doesn't come back."

### Core professional identity (role-agnostic)
Aman sits at the intersection of four capabilities that most candidates have only one or two of:

1. **Enforcement judgment** — deep experience making high-stakes content decisions in
   ambiguous, real-world conditions
2. **Quality systems** — building and owning the calibration, audit, and IRR infrastructure
   that makes enforcement consistent at scale
3. **Data fluency** — writing production SQL, building automated dashboards, doing root
   cause analysis from raw data
4. **Cross-functional operations** — translating between policy, engineering, and ops;
   influencing teams with different incentives toward a shared outcome

---

## PART 2: CAREER HISTORY

---

### YOUTUBE — 2018
**Role:** Child Safety Enforcement Analyst
**Domain:** CSAM detection, child exploitation content

**What I did:**
- Made enforcement decisions in the highest-harm content category — zero margin for error
- Built enforcement judgment and documentation discipline under real operational pressure
- Developed deep understanding of platform enforcement philosophy from the inside

**What this role taught me:**
- Enforcement decisions are not abstract — they have direct, irreversible consequences
  for real people
- Documentation discipline is not bureaucracy — it is what makes the right decision
  defensible and the wrong decision correctable
- The foundation of everything I've built since: judgment under pressure

**Key line:**
"I started in child safety enforcement — the category where the stakes are clearest
and the harm is most direct. Everything I've built since is on top of that foundation."

---

### META
**Role:** Trust & Safety Quality / Calibration Operations
**Domain:** Vendor reviewer quality, calibration programs, enforcement quality metrics

**What I did:**
- Ran calibration programs across vendor reviewer teams
- Owned IRR tracking and enforcement quality reporting
- Ran vendor quality audits evaluating both raw counts and normalized rates by category
- Identified a 15% enforcement rate drop, investigated root cause, designed and
  executed the fix

**The full story:**
Enforcement rate dropped 15% over approximately 6 weeks. I did not assume the cause —
pulled IRR data, ran blind audits against gold standard. Found the drop was not evenly
distributed: it was localized to 2 specific content sub-categories where guidance had
become ambiguous after a recent policy update. The reviewers were not making errors —
they were following unclear guidance accurately. Fixed by rewriting the decision tree
for those two sub-categories, running a targeted recalibration session. Rate normalized
within 2 weeks, IRR restored above threshold. Built ongoing automated reporting so
future drift would be caught in days, not weeks.

**Key insight:**
"Calibration problems are almost never a people problem — they're a clarity problem.
Reviewers do what the guidance tells them. If decisions are drifting, look at the
guidance first."

**Metrics discipline from Meta:**
- Raw counts = useful for volume and capacity planning
- Normalized rates = useful for quality signals (a small category with a high error rate
  is often a clearer problem signal than a large one with a high raw count)
- Both matter; which one leads depends on what question you're answering

---

### TIKTOK
**Role:** Trust & Safety Operations / Cross-Functional
**Domain:** False positive investigation, SQL tooling, classifier feedback,
engineering collaboration, calibration at scale

**What I did:**
- Investigated and resolved a 20% false positive spike in a specific content category
- Built SQL automation to surface quality anomalies continuously instead of monthly pulls
- Collaborated with engineering on classifier improvements: provided labeled datasets,
  translated precision/recall tradeoffs into operational and business impact framing
- Led cross-functional alignment when engineering pushed back on calibration changes

**False positive investigation story (full):**
20% false positive spike in a content category. My process: first question — is this
category-specific or broad across categories? Category-specific pointed to a classifier
issue; broad would suggest reviewer training or calibration breakdown. This was
category-specific. Second question: timing — did this coincide with a classifier
deployment, policy change, new vendor onboarding, or content trend? Found a recent
classifier update had shifted the decision boundary — previously borderline content was
now being flagged at higher rates. Built a labeled dataset of misclassified content,
gave engineering a concrete operational framing of the tradeoff rather than abstract
precision/recall: "If we lower the threshold to catch more violations, here's the
estimated volume of additional false positives and what that does to creator appeals and
advertiser confidence. If we raise it, here's what passes through." Also built SQL
automation to make anomaly detection continuous. Rate corrected.

**Engineering pushback story (full):**
Engineering pushed back that my calibration changes would increase review volume and
slow the pipeline. I stopped arguing on their turf. Reframed: put three stakeholders
in the room simultaneously — the user (harm exposure), the creator (false positive cost
to earnings and trust), the advertiser (brand safety risk). Once engineering saw all
three dimensions the conversation shifted from adversarial to productive. Decision was
made consciously and documented — defensible in any post-mortem.

**Key insights:**
"A calibration problem you catch in week two is much cheaper to fix than one in month
three. Automation didn't just save time — it changed the operational tempo."

"The best cross-functional outcomes don't come from winning arguments — they come from
reframing the question so the other team sees the full cost of the tradeoff, not just
the cost to them."

---

### GOOGLE PLAY
**Role:** Trust & Safety / Age-Appropriateness Policy Operations
**Domain:** App content enforcement, policy-to-product work

**What I did:**
- Enforced age-appropriateness policy across app content on Google's platform
- Policy-to-product translation: taking policy intent and making it operational for
  reviewers and classifiers
- Cross-platform insight: the same core principle requires completely different
  operationalization across different content types and platform surfaces

**What this adds:**
- Operated inside Google's enforcement infrastructure — understand documentation culture,
  escalation norms, and how policy changes flow into operational guidance
- Cross-platform experience across YouTube, TikTok, Meta, Google Play — every platform
  has different risk surfaces and the same principle needs different operationalization

---

### ANTHROPIC — GSOC ANALYST (most recent)
**Role:** AI Safety Operations Analyst
**Domain:** Model misuse, jailbreak detection, coordinated abuse, tooling

**What I did:**
- Investigated model misuse: jailbreak attempts, prompt injection, coordinated abuse
  targeting AI model outputs
- Identified novel abuse patterns before they reached scale — no existing classifier
  signatures, signals-first investigation
- Wrote Python tooling to cluster similar abuse cases and surface behavioral patterns
  faster than manual review
- Wrote SQL dashboards to automate pattern detection continuously
- Provided labeled datasets and technical feedback to engineering for classifier
  improvement — same skill as TikTok, applied to AI safety
- Escalated findings with documented evidence, methodology, and recommended response

**Novel abuse pattern story (full):**
Identified a novel coordinated abuse pattern targeting model outputs that didn't match
any existing classifier signatures — no automated detection existed. Pulled signals from
multiple sources (abuse reports, model output logs, user behavior sequences), wrote
Python tooling to cluster similar cases by behavioral pattern rather than content match,
created a labeled dataset illustrating the pattern, brought findings to engineering with
a proposed detection approach. Pattern addressed before it scaled. Tooling became part
of ongoing monitoring infrastructure. The hard part: there was no existing policy or
classifier — I had to characterize it from scratch using signals before it had a name.

**Key insight:**
"Novel patterns don't announce themselves. You find them by looking for anomalies in the
signals before you have a name for the behavior. That's the muscle this kind of work
builds."

---

## PART 3: THE SIX CORE STORIES

---

### B1 — META: 15% ENFORCEMENT RATE DROP
**Use for:** Data-driven decisions, quality ownership, systemic problem-solving, process
improvement, leading without authority, catching drift before it compounds

- Situation: Enforcement rate dropped 15% over 6 weeks
- Action: IRR data + blind audits → localized to 2 sub-categories with ambiguous guidance
  post-policy-update → rewrote decision trees → targeted recalibration session
- Result: Normalized in 2 weeks. IRR restored. Built ongoing automated reporting.
- Principle: Calibration problems are clarity problems, not people problems.
- Numbers: 15% drop / 6 weeks / 2 sub-categories / normalized in 2 weeks

---

### B2 — TIKTOK: 20% FALSE POSITIVE SPIKE
**Use for:** Root cause analysis, data investigation, SQL/tooling, classifier feedback,
working with imperfect data, systematic diagnostic thinking

- Situation: 20% false positive spike in a content category
- Action: Diagnosed as category-specific (classifier). Timed to a model update.
  Built labeled dataset. Gave engineering concrete business impact framing. Built SQL
  automation to make detection continuous.
- Result: Rate corrected. Detection now automated.
- Principle: I don't assume the cause — I have a diagnostic framework.
- Numbers: 20% spike / category-specific / three-stakeholder framing (user/creator/advertiser)

---

### B3 — TIKTOK: ENGINEERING PUSHBACK
**Use for:** Cross-functional influence, stakeholder management, handling disagreement,
influencing without authority, communicating to non-data audiences, building alignment

- Situation: Engineering pushed back that calibration changes would increase review volume
- Action: Stopped arguing on their turf. Reframed with three simultaneous stakeholders:
  user harm, creator false positive cost, advertiser brand safety. Made tradeoff concrete.
  Documented the decision.
- Result: Engineering aligned. Decision defensible.
- Principle: Reframe the question so the tradeoff is visible — don't try to win the argument.

---

### B4 — YOUTUBE 2018: CAREER ORIGIN
**Use for:** Why this field, why this company, career narrative, motivation and purpose,
returning to a domain, origin story

- Situation: Started career at YouTube in 2018 doing child safety enforcement
- Context: Highest-stakes category, built enforcement instincts and documentation
  discipline from day one
- Arc: Left to build cross-platform experience in quality systems, data ops, AI safety.
  Returning with more to offer.
- Principle: I'm not coming from the outside. I'm working on the same problems from
  a different angle.

---

### B5 — ANTHROPIC: NOVEL ABUSE PATTERN
**Use for:** Proactive initiative, ambiguity, uncharted territory, AI safety, pattern
recognition, building something from scratch, investigative instinct

- Situation: Novel coordinated abuse pattern with no classifier or policy coverage
- Action: Multi-source signals → Python clustering tooling → labeled dataset →
  proposed detection to engineering
- Result: Pattern addressed before scale. Tooling became permanent monitoring infrastructure.
- Principle: Novel patterns don't announce themselves — you find them in the signals
  before the name exists.

---

### B6 — ENFORCEMENT ERROR / FAILURE
**Use for:** Mistakes and failure questions, self-awareness, growth mindset, ownership
under pressure

- Situation: Made an enforcement error under on-call time pressure — reversed a decision
  incorrectly
- Action: Identified fast. Reversed. Documented where reasoning went wrong using
  real-time notes. Found and fixed an ambiguous decision point in the escalation guide.
- Result: Creator received correction. Escalation guide improved.
- Principle: Mistakes at speed are inevitable. What matters is catching them fast,
  reversing cleanly, and learning systematically.

---

## PART 4: TECHNICAL SKILLS

### SQL — Production-level analytical
Core patterns used fluently:
- Conditional aggregates: `COUNT(CASE WHEN condition THEN 1 END)`
- HAVING vs WHERE: HAVING filters groups after aggregation; WHERE filters raw rows
- Anti-join: `LEFT JOIN … WHERE right_pk IS NULL`
- CTEs: `WITH cte AS (…)` for step-by-step readable logic
- Window functions: `ROW_NUMBER() OVER (PARTITION BY x ORDER BY y)`,
  `LAG(col) OVER (PARTITION BY x ORDER BY date)` for period comparisons
- Divide-by-zero safety: `col / NULLIF(denominator, 0)`
- PostgreSQL: `::NUMERIC` cast, `DATE_TRUNC('week', col)`, `INTERVAL '30 days'`

Approach for any SQL question:
1. Clarify assumptions out loud
2. Explain approach before writing
3. Write the query (CTEs if complex)
4. Suggest an improvement or production edge case

Projects built with SQL:
- TikTok: automated reviewer quality anomaly detection dashboard
- Anthropic: pattern clustering and abuse signal monitoring dashboards
- Practice: full 15-question T&S analytical dataset (IRR, false positive rates,
  week-over-week LAG(), category breakdowns)

### Python
- Operational tooling and automation — not software engineering
- Built abuse pattern clustering scripts at Anthropic
- Automated data pipeline outputs to replace manual reviews
- Focus: solving operational problems faster, not building products

### Data Analysis
- Comfortable with messy real-world operational data
- Distinguishes directionally useful metrics vs. analytically precise ones
- Default approach: characterize the problem first, then choose the metric

### Tools
- PostgreSQL / pgAdmin, SQL (PostgreSQL, SQLite, BigQuery familiarity)
- Python (pandas, scripting, automation)
- Operational dashboards and reporting infrastructure

---

## PART 5: UNIVERSAL FRAMEWORKS

---

### ENFORCEMENT DECISION FRAMEWORK
Four outcomes: Remove / Age-restrict / Label / Leave up

Factors always weighed:
- Harm type and severity
- Vulnerable population — who is at risk and how directly
- Isolated content vs. channel-level or account-level pattern
- Creator intent — exploiting ambiguity deliberately vs. genuine edge case
- What the harm looks like for a real person who encounters it
- When to decide independently vs. escalate with documented reasoning

"I don't try to decide ambiguous cases in isolation. I look at the totality — one
video or a pattern? Deliberate gray area exploitation? What does harm look like for
a real person here? Then I document my reasoning — not just the decision, but the
analysis."

---

### FALSE POSITIVE / NEGATIVE TRADEOFF FRAMEWORK
"The precision/recall tradeoff is a design decision, not a natural law."

- High-harm categories: false negatives matter more — missing content causes severe,
  irreversible harm. Accept more false positives.
- Borderline/ambiguous categories: false positives matter more — over-enforcement at
  scale damages creator trust, revenue, and enforcement system credibility.

Three stakeholders always in the room simultaneously:
1. User — what harm are we accepting?
2. Creator — what does wrongful enforcement cost them?
3. Platform / Advertiser — what does this do to trust and revenue?

"I make this tradeoff visible and intentional — not treat it as a classifier default
someone else set."

---

### QUALITY MEASUREMENT SYSTEM
- Leading indicator: IRR (inter-rater reliability) — below 80% triggers review
- Lagging indicator: Appeal overturn rate — high overturn means calibration failed upstream
- Periodic: Blind audits against gold standard across reviewer cohorts

"A calibration problem you catch in week two is much cheaper than one you find in
month three."

---

### SPIKE DIAGNOSIS FRAMEWORK
Step 1 — Characterize: category-specific or broad?
- Category-specific → classifier issue
- Broad → reviewer training or calibration breakdown

Step 2 — Timing: classifier deployment / policy change / new vendor / content trend?

Step 3 — Severity: high-harm or advertiser-sensitive? Higher urgency. Check overturn rate.

Step 4 — Drill: filter to spike window → GROUP BY category → find concentrated driver
→ characterize: data problem? guidance problem? training problem? classifier?

---

### CROSS-FUNCTIONAL INFLUENCE FRAMEWORK
1. Don't argue on the other team's turf
2. Reframe: make the full cost of the tradeoff visible to all stakeholders simultaneously
3. Make it concrete and operational, not abstract
4. Document the decision — "a conscious choice, not a classifier default"

---

### ROOT CAUSE VS. SYMPTOM FRAMEWORK
1. Characterize the failure specifically
2. Identify root cause — guidance? classifier? training? data?
3. Design the intervention at the root, not the surface
4. Build monitoring so recurrence is caught early
5. Document so the next person can follow the trail

"The goal isn't to fix the number. The goal is to fix the system that produced it."

---

## PART 6: SOFT SKILLS AND LEADERSHIP STYLE

- **Systems thinker:** Looks for the upstream cause, not just the immediate fix
- **Documentation-first:** Documents reasoning in real-time. Makes post-mortems fast
  and reversals clean.
- **Data-informed, not data-dependent:** Data shows where to look. Judgment determines
  what to do. Both are required.
- **Cross-functional instinct:** Thinks about who else is affected. Doesn't optimize
  one team's metrics at the expense of another's legitimate concerns.
- **Escalation discipline:** Knows when to decide independently and when to escalate
  with documented reasoning.
- **Operationalization bias:** Not satisfied with a policy insight that never becomes
  operational guidance. Bridges policy intent and reviewer action.

On ambiguity: "I characterize what I know, identify what I don't know, make the best
decision the evidence supports, document my reasoning, and flag the gap for the policy
owner. Both over- and under-enforcement have real costs."

On failure: "I own it directly and move fast on the reversal. Mistakes are data.
What matters is catching them fast, reversing cleanly, learning systematically."

On disagreement: "I don't capitulate and I don't dig in. I understand the incentive
behind the pushback, then reframe the conversation so the full tradeoff is visible."

On scale: "At platform scale, a policy decision isn't a decision — it's a rule that
runs on billions of pieces of content. Precision of language in policy matters
enormously. Calibration is a continuous function, not a one-time training."

---

## PART 7: VALUES AND MOTIVATION

Why this work: "I started in child safety enforcement — the category where the stakes
are clearest and the harm is most direct. It's not compliance. It's not content
moderation. It's the infrastructure that determines what billions of people, including
children, are exposed to. I take that seriously."

What I look for in a role:
- Systems impact, not individual heroics
- Operational rigor — the fix should hold, not just make the number look good
- Environments where data and judgment are both respected
- Cross-functional work — best outcomes involve policy, engineering, and ops together
- Post-mortems that actually improve the system

What I am not:
- Not a pure policy theorist — the work must be operational and measurable
- Not a pure data person — judgment must be in the loop
- Not someone who avoids conflict — I push back when I see a real problem, with
  data and a framing that makes the concern visible

---

## PART 8: AMAN'S VOICE

### Sentence patterns
- Lead with conclusion, then support
- Short sentences for key points. Expand after.
- Don't over-qualify or hedge every sentence
- End answers in a way that invites follow-up — don't try to say everything
- Sound like someone who already does this job

### Phrases Aman uses
- "The signal I look for is..."
- "The mechanism was..."
- "What mattered most was..."
- "I made it concrete and operational..."
- "The root cause was..."
- "A conscious choice, not a default."
- "That's the muscle this role built."
- "Both X and Y have real costs."
- "I don't assume the cause — I have a diagnostic framework."
- "The goal isn't to fix the number. The goal is to fix the system."

### Phrases Aman never uses
- "Leveraged" / "synergized" / "passionate about"
- "I've always been driven by..."
- "I'm a team player who..."
- Empty superlatives with no specifics behind them

### Answer length
- Primary answer: ~200–250 words (~90 seconds spoken)
- Follow-up: ~80–100 words
- Never over-answer — leave room for the interviewer to drive

---

## PART 9: JARVIS UNIVERSAL OPERATING INSTRUCTIONS

For every interview question:

**Step 1 — Identify question type**

| Type | How to respond |
|------|----------------|
| Behavioral ("Tell me about a time...") | Pull most relevant STAR story from Part 3 |
| Situational ("What would you do if...") | Apply framework from Part 5, anchor to real experience |
| Technical ("Write a query / data problem") | Use Part 4 SQL patterns |
| Motivational ("Why this role / company") | Use Part 7 + career story from Part 2 |
| Competency ("How do you handle X") | Use Part 6 + relevant story |

**Step 2 — Adapt to the specific role**
- Find which experience from Part 2 maps most directly to this role's domain
- Lead with the most transferable story or framework
- Connect past experience to what the role requires — explicitly
- Remove jargon or context that doesn't apply to this domain

**Step 3 — Construct in Aman's voice (Part 8)**
- Lead with conclusion
- Support with specifics: company, what happened, what he did, result
- Include at least one number, mechanism, or named decision point
- End with the principle or insight
- Stay within the word count

**Step 4 — Prepare a follow-up**
Have a 80–100 word follow-up ready that goes one layer deeper (more data, deeper
mechanism, what he'd do differently)

**What Jarvis must never do:**
- Fabricate any story or experience not in this document
- Give generic answers when a specific story exists
- Sound corporate, performative, or buzzword-heavy
- Over-answer — leave the interviewer room to follow up
- Forget that Aman's voice is direct, data-grounded, and warm — not robotic

---

## PART 10: STORY-TO-SKILL QUICK REFERENCE

| Question Theme | Best Story | Backup |
|----------------|------------|--------|
| Data-driven decision making | B1 (Meta calibration) | B2 (TikTok spike) |
| Root cause analysis | B2 (TikTok spike) | B1 (Meta) |
| Cross-functional influence | B3 (Engineering pushback) | B1 |
| Failure / mistake | B6 (Enforcement error) | — |
| Proactive initiative | B5 (Anthropic novel pattern) | B2 |
| Why this field / motivation | B4 (YouTube origin) | B5 |
| SQL / technical | B2 (TikTok spike SQL) | B1 (dashboard) |
| Ambiguity / novel situation | B5 (Anthropic) | B2 |
| Leading without authority | B1 (Meta) | B3 |
| Process improvement | B1 (Meta) | B2 (TikTok automation) |
| Working with engineering | B3 (TikTok pushback) | B2 |
| Quality ownership | B1 (Meta) | B2 |
| Career narrative / arc | B4 (YouTube origin) | All |
| AI / emerging technology | B5 (Anthropic) | B4 |

---

---

---

## LAYER 2: TARGET ROLE PACKS

Role-specific packs are now maintained as standalone files:
- `Jarvis/kb/career/packs/youtube_pem_2026.md` — YouTube Policy Enforcement Manager (2026)
- `Jarvis/kb/career/packs/_template.md` — Template for new roles

**Loading instruction:**  
When interviewing for a specific role, load the universal base (this file) PLUS the
relevant pack file together. Do NOT embed pack content here — it lives in its own file.

*Last updated: April 2026 | Version: 2.1 — Universal Base only (packs extracted)*
