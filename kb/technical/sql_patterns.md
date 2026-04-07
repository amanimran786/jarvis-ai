# JARVIS KNOWLEDGE BASE — SQL PATTERNS
# Aman's established SQL fluency and patterns
# Loaded by: TechAssist module when SQL domain is detected
# Privacy tier: Public

---

## DIALECT DEFAULTS
- **Primary:** PostgreSQL
- **Secondary:** SQLite (local development, sql.js in browser)
- **Familiarity:** BigQuery syntax, Snowflake basics

---

## CORE PATTERNS — USE THESE BY DEFAULT

### 1. Conditional Aggregates
Count specific subsets within a GROUP BY without a subquery.
```sql
SELECT
  category,
  COUNT(*)                                              AS total,
  COUNT(CASE WHEN status = 'removed'    THEN 1 END)    AS removed,
  COUNT(CASE WHEN status = 'age_restricted' THEN 1 END) AS age_restricted,
  ROUND(
    COUNT(CASE WHEN status = 'removed' THEN 1 END)::NUMERIC
    / NULLIF(COUNT(*), 0) * 100, 2
  )                                                     AS removal_rate_pct
FROM content_reviews
GROUP BY category;
```
**Use when:** counting events that meet a condition inside a broader GROUP BY.
**Don't use:** WHERE clause filtering before GROUP BY (loses the denominator).

---

### 2. HAVING vs. WHERE
```sql
-- WHERE: filters BEFORE aggregation (operates on raw rows)
SELECT reviewer_id, COUNT(*) AS decisions
FROM reviewer_actions
WHERE action_date >= CURRENT_DATE - INTERVAL '30 days'   -- ← raw row filter
GROUP BY reviewer_id;

-- HAVING: filters AFTER aggregation (operates on groups)
SELECT reviewer_id, COUNT(*) AS decisions
FROM reviewer_actions
GROUP BY reviewer_id
HAVING COUNT(*) > 50;                                    -- ← group-level filter
```
**The rule:** WHERE = rows. HAVING = groups. Never filter aggregated values with WHERE.

---

### 3. CTEs (Common Table Expressions)
Use for any query with 2+ logical steps. Makes logic auditable and debuggable.
```sql
WITH daily_decisions AS (
  SELECT
    reviewer_id,
    DATE_TRUNC('day', action_ts)  AS day,
    COUNT(*)                       AS total,
    COUNT(CASE WHEN action = 'no_action' THEN 1 END) AS no_action
  FROM reviewer_actions
  GROUP BY 1, 2
),
daily_rates AS (
  SELECT
    reviewer_id,
    day,
    total,
    ROUND(no_action::NUMERIC / NULLIF(total, 0) * 100, 2) AS no_action_rate
  FROM daily_decisions
)
SELECT *
FROM daily_rates
WHERE no_action_rate > 80
ORDER BY day DESC, no_action_rate DESC;
```

---

### 4. Anti-Join (LEFT JOIN + IS NULL)
Find records in Table A that have NO matching record in Table B.
```sql
-- Users who have never had a reviewer action taken against them
SELECT u.user_id, u.username
FROM users u
LEFT JOIN reviewer_actions ra ON u.user_id = ra.user_id
WHERE ra.user_id IS NULL;
```
**Use when:** "find all X that have no Y" — more readable than NOT IN / NOT EXISTS
for most cases.

---

### 5. Window Functions — ROW_NUMBER (deduplication / ranking)
```sql
-- Most recent decision per user per category (keep only latest)
SELECT *
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, category
      ORDER BY action_ts DESC
    ) AS rn
  FROM reviewer_actions
) ranked
WHERE rn = 1;
```

---

### 6. Window Functions — LAG (period comparison)
Week-over-week, day-over-day comparisons without a self-join.
```sql
WITH weekly_rates AS (
  SELECT
    reviewer_id,
    DATE_TRUNC('week', action_ts)  AS week,
    ROUND(
      COUNT(CASE WHEN action = 'removed' THEN 1 END)::NUMERIC
      / NULLIF(COUNT(*), 0) * 100, 2
    ) AS removal_rate
  FROM reviewer_actions
  GROUP BY 1, 2
)
SELECT
  reviewer_id,
  week,
  removal_rate,
  LAG(removal_rate) OVER (
    PARTITION BY reviewer_id
    ORDER BY week
  )                                    AS prior_week_rate,
  removal_rate - LAG(removal_rate) OVER (
    PARTITION BY reviewer_id
    ORDER BY week
  )                                    AS week_over_week_change
FROM weekly_rates
ORDER BY reviewer_id, week;
```

---

### 7. NULLIF — Divide-by-Zero Safety
Always wrap denominators in NULLIF(expr, 0) when computing rates.
```sql
-- WRONG — crashes when denominator is 0
ROUND(numerator::NUMERIC / total * 100, 2)

-- RIGHT — returns NULL instead of division error
ROUND(numerator::NUMERIC / NULLIF(total, 0) * 100, 2)
```

---

### 8. PostgreSQL-Specific Syntax
```sql
-- Casting for ROUND(AVG()) — required in PostgreSQL
ROUND(AVG(score)::NUMERIC, 2)

-- Date arithmetic
WHERE action_ts >= CURRENT_DATE - INTERVAL '30 days'
WHERE action_ts >= CURRENT_DATE - INTERVAL '1 week'

-- Date truncation for grouping
DATE_TRUNC('week', action_ts)    -- truncates to Monday
DATE_TRUNC('month', action_ts)
DATE_TRUNC('day', action_ts)

-- NOT IN vs LEFT JOIN anti-join: prefer LEFT JOIN for NULLable FK columns
```

---

## APPROACH PROTOCOL FOR ANY SQL QUESTION

1. **Clarify assumptions** — state what you're assuming about schema/data before writing
2. **Explain the approach** — 1–2 sentences on the structure before the SQL
3. **Write the query** — CTE structure for anything with 2+ steps
4. **Suggest the production improvement** — what would make this production-quality

---

## PROJECTS BUILT WITH SQL (real experience to reference)

**TikTok — Automated reviewer quality dashboard**
- Daily false positive rate by category
- Week-over-week trend using LAG()
- Auto-flagging when rate deviated >2 stddev from rolling average
- Replaced monthly manual audit pull

**Anthropic — Abuse pattern monitoring**
- Behavioral clustering queries to surface similar abuse cases
- Anomaly detection on model output patterns
- Session-level signal aggregation for investigation triage

**Practice — ts_practice database**
- 50 users, 165 content reports, 82 reviewer actions
- Built and solved 15 analytical questions including:
  - IRR calculation (conditional aggregate approach)
  - False positive rates by category with NULLIF safety
  - Reviewer week-over-week accuracy using LAG()
  - Users with no review actions (LEFT JOIN anti-join)
  - Category-level quality breakdowns with HAVING

---

*SQL patterns version: 1.0*
*Last updated: April 2026*
