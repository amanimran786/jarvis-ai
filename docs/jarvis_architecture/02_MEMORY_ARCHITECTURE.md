# JARVIS — MEMORY ARCHITECTURE
# Version 1.0

> Aman should never have to re-explain himself to Jarvis.
> Memory is what separates a personal AI from a generic one.

---

## OVERVIEW

Jarvis has five memory layers. Each layer has a different scope, persistence model,
retrieval mechanism, and privacy tier. They are assembled into the context window
in a specific order for every request.

```
┌────────────────────────────────────────────────────────────────┐
│                    MEMORY LAYER STACK                          │
│                                                                │
│  L0  Core Identity      [IMMUTABLE]  [ALWAYS LOADED]          │
│  ──────────────────────────────────────────────────────────    │
│  L1  Semantic Memory    [PERSISTENT] [RETRIEVED: top-k]        │
│  ──────────────────────────────────────────────────────────    │
│  L2  Episodic Memory    [PERSISTENT] [RETRIEVED: recent+rel]   │
│  ──────────────────────────────────────────────────────────    │
│  L3  Working Memory     [SESSION]    [ALWAYS LOADED]           │
│  ──────────────────────────────────────────────────────────    │
│  L4  Private Vault      [PERSISTENT] [EXPLICIT UNLOCK ONLY]    │
└────────────────────────────────────────────────────────────────┘
```

---

## LAYER 0 — CORE IDENTITY
**File:** `kb/core/identity.md`
**Scope:** Universal — always in context, every request
**Persistence:** Immutable (updated only by deliberate human decision)
**Privacy:** Public (safe to send to any model)
**Size target:** Keep under 2,000 tokens

### What belongs here
- Who Aman is (1-paragraph professional identity)
- Communication style and voice rules
- Values and operating principles
- Career identity (not full history — that's L1)
- How Jarvis should reason about Aman

### What does NOT belong here
- Specific story details (those are in universal base)
- Company-specific context (that's in packs)
- Personal/sensitive info (that's L4)
- Preferences that change over time (that's L1)

### Schema
```yaml
identity:
  name: string
  professional_identity: string  # 1-paragraph
  domain_expertise: [string]
  career_stage: string
  communication_style:
    tone: string
    structure: string
    phrases_used: [string]
    phrases_never_used: [string]
  values: [string]
  operating_principles: [string]
  decision_style: string
```

---

## LAYER 1 — SEMANTIC MEMORY
**Directory:** `memory/semantic/`
**Scope:** Persistent across all sessions
**Retrieval:** Vector similarity search (top-k, configurable)
**Privacy:** Semi-private (prefer local for retrieval; strip PII before external API)
**Write trigger:** After any session where new facts, preferences, or patterns emerge

### What belongs here
- Facts Jarvis has learned about Aman that don't fit elsewhere
- Preferences: tools he prefers, working styles, food, how he likes info presented
- Patterns: recurring themes in his requests, decisions, frustrations
- Relationship metadata: key people, their roles, how Aman relates to them
- Professional knowledge Aman has demonstrated (not his stories — his actual expertise)
- Goals: stated and inferred

### Entry schema
```json
{
  "id": "sem_001",
  "type": "preference | fact | pattern | relationship | expertise | goal",
  "content": "string — the fact/preference/pattern in plain language",
  "source": "conversation | explicit_statement | inference",
  "confidence": 0.0–1.0,
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "last_used_at": "ISO8601",
  "use_count": integer,
  "tags": [string],
  "privacy_tier": "public | semi-private | private"
}
```

### Example entries
```json
[
  {
    "id": "sem_001",
    "type": "preference",
    "content": "Prefers conclusions first, then supporting detail. Gets frustrated when responses start with preamble.",
    "source": "inference",
    "confidence": 0.95,
    "tags": ["communication", "output_style"]
  },
  {
    "id": "sem_002",
    "type": "expertise",
    "content": "Deep SQL fluency: CTEs, window functions (LAG, ROW_NUMBER), conditional aggregates. PostgreSQL preferred.",
    "source": "conversation",
    "confidence": 0.98,
    "tags": ["technical", "sql"]
  },
  {
    "id": "sem_003",
    "type": "pattern",
    "content": "When making cross-functional decisions, consistently reframes to show impact on three stakeholders simultaneously (user, creator, advertiser/platform). This is a signature move.",
    "source": "inference",
    "confidence": 0.9,
    "tags": ["decision_making", "professional_pattern"]
  },
  {
    "id": "sem_004",
    "type": "goal",
    "content": "Primary career goal: land a Policy Enforcement Manager or equivalent T&S leadership role at a major platform (YouTube, Meta, TikTok-level). Timeline: current active search.",
    "source": "explicit_statement",
    "confidence": 0.99,
    "tags": ["career", "goals"]
  },
  {
    "id": "sem_005",
    "type": "preference",
    "content": "Dislikes over-hedged, caveat-heavy responses. Prefers direct statements with confidence where warranted.",
    "source": "inference",
    "confidence": 0.92,
    "tags": ["communication", "output_style"]
  }
]
```

### Retrieval algorithm
```python
def retrieve_semantic_memory(query, top_k=5):
    query_embedding = embed(query)
    candidates = vector_store.search(query_embedding, limit=top_k * 3)

    # Re-rank by: relevance * recency_boost * use_count_boost
    scored = []
    for entry in candidates:
        recency_boost = decay(entry.last_used_at, half_life_days=30)
        score = entry.similarity * recency_boost * (1 + 0.1 * log(entry.use_count + 1))
        scored.append((score, entry))

    return sorted(scored, reverse=True)[:top_k]
```

### Write conditions
Auto-write to semantic memory when:
- Aman explicitly states a preference ("I prefer...", "I always...", "I hate when...")
- A new fact about Aman is revealed that isn't already stored
- A pattern is detected 3+ times across different sessions
- A goal or priority is stated or significantly updated

Do NOT write:
- Transient session context (that's working memory)
- Temporary states ("I'm tired today")
- Sensitive personal data (that's private vault)

---

## LAYER 2 — EPISODIC MEMORY
**Directory:** `memory/episodic/`
**Scope:** Persistent, time-indexed
**Retrieval:** Recency + semantic relevance combined
**Privacy:** Semi-private
**Write trigger:** End of any substantive session

### What belongs here
- Significant conversations and what was decided
- Actions taken on Aman's behalf
- Goals set and their progress
- Drafts created and sent
- Applications submitted, interviews scheduled
- Decisions Aman made with Jarvis's input

### Entry schema
```json
{
  "id": "ep_001",
  "timestamp": "ISO8601",
  "type": "conversation | decision | action | application | interview | draft | milestone",
  "summary": "string — what happened, 1–3 sentences",
  "outcome": "string | null — what was decided or produced",
  "entities": {
    "people": [string],
    "companies": [string],
    "roles": [string],
    "topics": [string]
  },
  "follow_up": "string | null — anything that needs to happen next",
  "tags": [string],
  "privacy_tier": "public | semi-private | private",
  "linked_memory_ids": [string]
}
```

### Example entries
```json
[
  {
    "id": "ep_001",
    "timestamp": "2026-04-06T10:00:00Z",
    "type": "interview",
    "summary": "Prepared for YouTube Policy Enforcement Manager interview with Shirapta Huerta Cruz. Mock interview completed. Scored on 4 dimensions: data layer, framing, SQL, engineering alignment.",
    "outcome": "Interview scheduled for Monday April 6. Thank you note drafted and reviewed.",
    "entities": {
      "companies": ["YouTube", "Google"],
      "roles": ["Policy Enforcement Manager, Age Appropriateness"],
      "people": ["Shirapta Huerta Cruz"]
    },
    "follow_up": "Check in after interview to debrief and update career pack based on actual questions asked.",
    "tags": ["career", "interview", "youtube"]
  },
  {
    "id": "ep_002",
    "timestamp": "2026-04-05T14:00:00Z",
    "type": "action",
    "summary": "Built Jarvis universal interview intelligence system. Layered architecture with universal base + target role packs + company packs.",
    "outcome": "Files saved to Resumes/Jarvis/. Canonical v12 playbook built and synced to 3 locations.",
    "entities": {
      "topics": ["jarvis", "interview_prep", "system_design"]
    },
    "tags": ["jarvis", "career", "system_build"]
  }
]
```

### Retrieval algorithm
```python
def retrieve_episodic_memory(query, recent_n=3, top_k=5):
    # Always include most recent N events
    recent = episodic_store.get_most_recent(n=recent_n)

    # Retrieve semantically relevant older events
    query_embedding = embed(query)
    relevant = episodic_store.search(query_embedding, limit=top_k * 2)

    # Merge, deduplicate, sort by timestamp desc
    combined = deduplicate(recent + relevant)
    return sorted(combined, key=lambda x: x.timestamp, reverse=True)[:top_k]
```

---

## LAYER 3 — WORKING MEMORY
**Directory:** `memory/working/{session_id}.json`
**Scope:** Current session only
**Persistence:** Volatile — cleared at session end (optionally promoted to episodic)
**Privacy:** Session-local
**Write trigger:** Every exchange in the session

### What belongs here
- Current task state
- Active entities in this conversation (people, companies, topics)
- Decisions made in this session
- Drafts in progress
- User's stated context for this session ("I have an interview in 2 hours")

### Schema
```json
{
  "session_id": "uuid",
  "started_at": "ISO8601",
  "active_module": "string",
  "active_role_pack": "string | null",
  "active_company_pack": "string | null",
  "private_unlocked": false,
  "entities": {
    "people": [string],
    "companies": [string],
    "topics": [string]
  },
  "current_task": "string | null",
  "decisions_made": [string],
  "drafts_in_progress": [
    {
      "id": "string",
      "type": "email | message | document | code",
      "status": "draft | revised | final",
      "content_summary": "string"
    }
  ],
  "user_stated_context": "string | null",
  "exchange_count": integer
}
```

### Promotion to episodic memory
At session end, auto-promote if:
- A significant decision was made
- A draft was finalized
- An application was submitted or interview was prepared for
- A new goal or priority was stated
- Exchange count > 5 (substantive session)

---

## LAYER 4 — PRIVATE VAULT
**File:** `memory/private.vault` (encrypted at rest)
**Scope:** Persistent, requires explicit unlock
**Retrieval:** Manual only — no automatic retrieval
**Privacy:** Encrypted, local model only
**Write trigger:** Explicit user command only

### What belongs here
- Personal relationships (family, friends, significant other)
- Health information
- Financial details
- Personal goals outside of career
- Anything Aman explicitly marks as private

### Access protocol
```
# To unlock:
User: "jarvis unlock private"
Jarvis: "Private vault is now active. I'll use local model only for this session.
         What do you need?"

# Auto-lock triggers:
- Session end
- 30 minutes of inactivity
- User command: "jarvis lock"
- Any request to use external API while vault is active → refuse + warn
```

### Vault entry schema
```json
{
  "id": "prv_001",
  "category": "relationship | health | finance | personal_goal | sensitive",
  "content": "encrypted_string",
  "created_at": "ISO8601",
  "updated_at": "ISO8601",
  "access_log": [
    {
      "accessed_at": "ISO8601",
      "reason": "string"
    }
  ]
}
```

---

## MEMORY WRITE RULES

### What triggers a memory write
| Event | Layer | Type |
|-------|-------|------|
| New preference stated | L1 | preference |
| New fact learned | L1 | fact |
| Same pattern 3+ times | L1 | pattern |
| Goal stated or updated | L1 | goal |
| Session with >5 exchanges | L2 | conversation |
| Application submitted | L2 | application |
| Interview completed | L2 | interview |
| Decision made | L2 | decision |
| Draft finalized | L2 | action |
| Private info shared | L4 | category |

### What does NOT get written
- Transient states ("I'm tired," "I'm in a rush")
- Information that's already in L0 (identity)
- Information that's already accurately in L1 (no duplicates)
- Speculation or things Aman didn't actually say

### Conflict resolution
If a new entry contradicts an existing L1 entry:
1. Do not overwrite silently
2. Create a new entry with higher timestamp
3. Flag the contradiction: "This seems to update what I had stored: {old_entry}"
4. Ask for confirmation before updating if the confidence gap is small

---

## MEMORY HYGIENE

### Decay
L1 entries that haven't been accessed in 90 days have their relevance score reduced.
After 180 days with no access, they are flagged for review (not deleted).

### Deduplication
Before writing any new L1 entry, check cosine similarity against existing entries.
If similarity > 0.92: update existing entry rather than creating duplicate.

### Garbage collection
Run monthly:
1. Flag L1 entries with use_count = 0 and age > 90 days
2. Flag L2 entries with no linked follow-up and age > 1 year
3. Present flagged entries to Aman: "These memories haven't been used. Keep or remove?"

---

*Memory architecture version: 1.0*
*Last updated: April 2026*
