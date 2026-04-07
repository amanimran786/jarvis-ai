# JARVIS KNOWLEDGE BASE — DECISION FRAMEWORKS
# Aman's operating frameworks for decisions, tradeoffs, and strategy
# Loaded by: StrategyOS module
# Privacy tier: Public

---

## HOW AMAN THINKS ABOUT DECISIONS

Three principles that run across all his frameworks:

1. **Make the tradeoff visible** — both over- and under- anything have real costs.
   The job is not to eliminate the tradeoff; it's to make it explicit and conscious.
2. **Name who bears which cost** — every tradeoff has a winner and a loser.
   Naming them changes the conversation from abstract to operational.
3. **Root cause before solution** — treating the symptom is a temporary fix.
   The system that produced the problem will produce it again.

---

## FRAMEWORK 1 — SPIKE DIAGNOSIS

Use when: something unexpected moves in the data (a rate, a count, a signal).

```
Step 1: CHARACTERIZE — is this concentrated or broad?
  Concentrated (1–2 categories/reviewers/surfaces) → likely a specific mechanism
    (classifier update, policy change, content trend, individual performance)
  Broad (across many categories/reviewers) → likely a systemic mechanism
    (reviewer training issue, calibration breakdown, tooling change)

Step 2: TIME — what changed at the same time?
  Classifier deployment? Policy update? New vendor onboarding? Content trend?
  These four cover ~80% of quality signal movements.

Step 3: SEVERITY CALIBRATION — how fast does this need a response?
  Is this spike in high-harm or advertiser-sensitive categories?
    → Faster path to business impact → higher urgency
  What does the overturn rate look like in the following period?
    → Spike in removals + high overturn next week = over-enforcement upstream

Step 4: DRILL — filter to the spike window, GROUP BY the most likely driver
  Find the concentrated source → characterize the mechanism
  Mechanism types: data quality problem / guidance problem /
                   training problem / classifier problem
```

---

## FRAMEWORK 2 — PRECISION / RECALL TRADEOFF

Use when: deciding how to set enforcement thresholds, classifier boundaries,
escalation triggers — any decision that involves catching more vs. catching accurately.

**The core framing:**
"The precision/recall tradeoff is a design decision, not a natural law."
It should be made consciously with all stakeholders visible, not accepted as a default.

**Calibration by harm severity:**
- High-harm categories (CSAM, child exploitation, coordinated abuse):
  → False negatives matter more — missing content causes irreversible harm
  → Accept more false positives to catch more violations
- Borderline/ambiguous categories (mature-but-legal, gray-area content):
  → False positives matter more — over-enforcement at scale damages creator trust,
    has revenue implications, and erodes the credibility of the enforcement system
  → Tighten precision before chasing recall

**Three-stakeholder framing (use with engineering):**
Don't frame precision/recall abstractly. Frame it as:
1. User — what harm exposure are we accepting?
2. Creator — what does a false positive cost them (earnings, trust, appeal burden)?
3. Platform/Advertiser — what does this do to brand safety and revenue?

This makes the tradeoff productive rather than abstract.

---

## FRAMEWORK 3 — CALIBRATION PROBLEM DIAGNOSIS

Use when: reviewer quality degrades, enforcement rates shift, IRR drops.

**The fundamental insight:**
Calibration problems are almost never people problems — they're clarity problems.
Reviewers do what the guidance tells them. If decisions are drifting, look at the
guidance first.

**Diagnostic ladder:**
```
1. Is the problem isolated or distributed?
   Isolated (specific category, reviewer cohort, time window) → guidance or training issue
   Distributed (broadly across team) → policy update propagation issue

2. When did it start? Does it map to a policy update or a team change?
   Yes → the policy update created ambiguity
   No → something else changed (content mix, external context)

3. Run blind audits against the gold standard
   Which specific decision points are diverging?
   Is it an over-enforcement error or an under-enforcement error?

4. Design the fix at the right level:
   Decision-point ambiguity → rewrite the decision tree for that specific case
   Training gap → targeted recalibration session on the specific failure mode
   Gold standard gap → the standard itself may need updating
```

**Measurement system:**
- Leading indicator: IRR (drops below 80% trigger review)
- Lagging indicator: Appeal overturn rate (high overturn = calibration failed upstream)
- Audit cadence: periodic blind audits across cohorts against gold standard

---

## FRAMEWORK 4 — CROSS-FUNCTIONAL ALIGNMENT

Use when: need buy-in from a team with different incentives (engineering, product, legal).

**The core insight:**
The best cross-functional outcomes don't come from winning arguments. They come from
reframing the question so the other team sees the full cost of the tradeoff, not just
the cost to them.

**Protocol:**
```
1. Don't argue on their turf
   Engineering will always win an argument about engineering cost.
   Legal will always win an argument about legal risk.
   Stop arguing on their terms.

2. Reframe with all stakeholders visible simultaneously
   Show the full cost matrix:
   - What does [option A] cost the user?
   - What does it cost the creator?
   - What does it cost the platform/advertiser?
   Make all three rows visible at once.

3. Make the decision conscious and documented
   "We're choosing to accept [X cost] in order to avoid [Y cost]."
   Document it explicitly — "a conscious choice, not a default."
   This makes the decision defensible in post-mortems and makes future
   cross-functional conversations easier (both sides have skin in it).

4. Separate the decision from the implementation
   Agreement on the tradeoff doesn't mean agreement on the implementation.
   Once the tradeoff is agreed, implementation can be negotiated separately.
```

---

## FRAMEWORK 5 — ROOT CAUSE VS. SYMPTOM

Use when: diagnosing any quality, operational, or technical failure.

```
1. Characterize the failure specifically
   What exactly happened? What is the mechanism of failure?
   (Not "quality dropped" — "false positive rate in category X increased 20%
   in the 2 weeks following the v2.3 classifier deployment")

2. Identify the root cause
   Guidance problem? Classifier problem? Training problem? Data problem?
   Staffing/capacity problem? Process problem?

3. Design the intervention at the root
   Fix the guidance, not the reviewer.
   Fix the classifier, not the threshold.
   Fix the process, not the person.

4. Build monitoring for recurrence
   What's the earliest signal that this problem is returning?
   Build that signal into ongoing monitoring.

5. Document so the next person can follow the trail
   The post-mortem should be useful to someone with no prior context.
```

---

## FRAMEWORK 6 — MAKE OR BUY / BUILD OR INTEGRATE

Use when: deciding whether to build a capability vs. use an existing tool/service.

**Aman's default orientation:** build the harness, buy the components.
- Models are commodities — don't bet on a specific model
- Infrastructure (routing, memory, permissions) is proprietary — own it
- Domain knowledge (patterns, frameworks, heuristics) is the moat — codify it

**Decision criteria:**
```
Build when:
  - The capability is core to the differentiated value
  - Off-the-shelf options require you to fit their model (not your needs)
  - You need fine-grained control over behavior, privacy, or performance
  - The cost of vendor lock-in exceeds the cost of building

Buy/integrate when:
  - The capability is a commodity (embeddings, OCR, speech-to-text)
  - Time-to-value matters more than control
  - The vendor's specialization exceeds what you'd build in reasonable time
  - Maintenance burden would exceed ongoing value
```

---

## FRAMEWORK 7 — LONG-HORIZON DECISION FRAMEWORK

Use when: decisions with consequences that play out over months or years.

```
1. Separate the knowable from the unknowable
   What do you actually know now vs. what are you assuming?
   What's the half-life of the assumption? (Markets change, people change, etc.)

2. Identify the reversible vs. irreversible
   Reversible decisions: optimize for speed and learning
   Irreversible decisions: optimize for correctness, slow down

3. What would have to be true for the opposite to be right?
   This surfaces the load-bearing assumptions.
   If those assumptions are wrong, does the decision change?

4. Second-order effects
   What happens after the obvious outcome?
   Who benefits that shouldn't? Who gets hurt that you didn't expect?

5. Conditions under which you'd change the decision
   State these explicitly upfront.
   "I'd choose differently if [X]. Currently I believe [X is not true]."
```

---

*Decision frameworks version: 1.0*
*Last updated: April 2026*
