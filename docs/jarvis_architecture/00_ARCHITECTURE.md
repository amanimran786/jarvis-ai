# JARVIS — SYSTEM ARCHITECTURE
# Version 1.0

> The harness is the product. Models are dependencies.
> Routing, memory, evals, tools, permissions, reflection, and capability packs
> are what make Jarvis intelligent — not the underlying model.

---

## SECTION 1: CORE PHILOSOPHY

### What Jarvis is
Jarvis is a personal AI operating system. It knows who Aman is, how he thinks,
what he's working on, and what he cares about. It uses that knowledge to give
high-signal, specific, non-generic answers — not the kind any anonymous user
would get from the same query.

### What Jarvis is not
- Not a chatbot wrapper. Generic LLM behavior is a failure mode.
- Not a static prompt. Jarvis learns, adapts, and updates its model of Aman.
- Not a single-layer system. Context, memory, and capability are all modular.
- Not model-locked. The harness works across model providers.

### The three laws of Jarvis
1. **Specificity over completeness.** A short, correct, personalized answer beats
   a long generic one every time.
2. **Memory over repetition.** Aman should never have to re-explain context.
   If Jarvis has seen it before, it remembers it.
3. **Harness over model.** Routing, memory retrieval, and context assembly happen
   before the model sees a single token. The quality of Jarvis is determined there.

---

## SECTION 2: SYSTEM COMPONENTS

```
┌─────────────────────────────────────────────────────────────┐
│                        JARVIS HARNESS                        │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │    INPUT     │───▶│   ROUTER     │───▶│   CONTEXT    │  │
│  │   LAYER      │    │   ENGINE     │    │  ASSEMBLER   │  │
│  └──────────────┘    └──────────────┘    └──────┬───────┘  │
│                                                 │           │
│  ┌──────────────────────────────────────────────▼───────┐  │
│  │                    MEMORY SYSTEM                      │  │
│  │  L0: Core Identity  │  L1: Semantic  │  L2: Episodic │  │
│  │  L3: Working (session) │  L4: Private (encrypted)    │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │               CAPABILITY MODULES                      │  │
│  │  InterviewIntel │ CareerOS │ TechAssist │ StrategyOS  │  │
│  │  DailyOS        │ ResearchEngine │ CommunicationCraft │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │   MODEL      │    │   TOOLS      │    │  REFLECTION  │  │
│  │   LAYER      │    │   LAYER      │    │    LOOP      │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

---

## SECTION 3: REQUEST LIFECYCLE

Every request Jarvis receives passes through this pipeline in order:

### Step 1 — Input Processing
```
raw_input → normalize → extract_intent_signals → classify_domain
```
- Strip noise, normalize format
- Extract: domain signals, entities, urgency, mode hints
- Classify: which capability module(s) are relevant

### Step 2 — Routing
```
classified_input → route_to_modules → determine_context_requirements
```
- Primary module: the one that owns this request
- Secondary modules: supporting modules to load in background
- Context budget: how many tokens are available for context assembly

### Step 3 — Context Assembly
```
context_requirements → memory_retrieval → kb_loading → context_window_assembly
```
Order of operations:
1. Always load: `kb/core/identity.md` (non-negotiable)
2. Load: system prompt behavioral rules (always)
3. Retrieve: relevant semantic memory chunks (top-k by cosine similarity)
4. Retrieve: relevant episodic memory (recent + relevant)
5. Load: activated module's static context
6. If InterviewIntel module: load career universal base + target-role pack (if set) +
   company pack (if set)
7. Load: working memory from current session
8. Inject: assembled context into model prompt

### Step 4 — Model Execution
```
assembled_context + user_input → model → raw_response
```
- Model selection: route to appropriate model based on task type
  - Fast/chat tasks: local Ollama model
  - Complex reasoning: local reasoning model first, with optional cloud only outside open-source mode
  - Code: local coding model first
  - Long-context: local retrieval + local reasoning first, with optional cloud only as a non-default fallback
- Temperature: varies by task (0.2 for factual/code, 0.7 for creative/strategy)

### Step 5 — Post-processing
```
raw_response → format_check → memory_write → reflection_check → final_output
```
- Format enforcement: apply output schema for this task type
- Memory write: extract and store any new facts/events/preferences
- Reflection: flag if response quality is below threshold for retry
- Output: deliver final response

### Step 6 — Reflection Loop (async)
```
response → quality_eval → memory_update → pattern_detection
```
- Runs asynchronously after response delivery
- Evaluates: was this response specific enough? Did it use memory correctly?
- Updates: memory relevance scores, module performance metrics
- Flags: patterns in requests that suggest new modules or KB entries needed

---

## SECTION 4: ROUTING ENGINE

### Domain Classification
The router classifies every input into one or more of these domains:

| Domain | Trigger Signals | Primary Module |
|--------|----------------|----------------|
| interview_prep | interview, role, hiring, answer this, mock, prep, question about experience | InterviewIntel |
| career_ops | apply, job search, outreach, resume, LinkedIn, recruiter, negotiation | CareerOS |
| technical | code, SQL, query, debug, error, build, architecture, system design, script | TechAssist |
| strategy | decision, tradeoff, should I, thinking through, long-term, weighing | StrategyOS |
| daily_life | remind, schedule, task, note, draft, email, message, today, this week | DailyOS |
| research | research, find out, what is, explain, summarize, who is, look up | ResearchEngine |
| communication | write, draft, respond to, message, email, reply, tone, voice | CommunicationCraft |
| memory_ops | remember, update, forget, what do you know, recall | MemoryManager |

### Multi-domain Routing
Many requests span domains. Rules:

1. If a request is 60%+ one domain → single module primary
2. If split between two → primary + secondary (secondary loads read-only context)
3. If three or more → decompose request into sequential sub-tasks, each routed
   independently

### Confidence Thresholds
- HIGH (>0.8): route immediately, no clarification
- MEDIUM (0.5–0.8): route but state the assumption at top of response
- LOW (<0.5): ask one targeted clarifying question before routing

---

## SECTION 5: INTERVIEW INTELLIGENCE ROUTING (LAYERED)

This module has its own sub-routing logic with three layers:

```
Interview Request
      │
      ▼
┌─────────────────────────────────┐
│     LAYER 0: Universal Base     │  ← Always loaded
│  kb/career/universal_base.md    │    (stories, frameworks, voice, skills)
└─────────────────┬───────────────┘
                  │
                  ▼
      Is there an active target-role pack?
      (user set: JARVIS_ACTIVE_ROLE)
                  │
        YES ──────┴────── NO
         │                │
         ▼                ▼
┌────────────────┐   Use universal base only.
│  LAYER 1:      │   Map Aman's most relevant
│  Target-Role   │   experience to the detected
│  Pack          │   domain/role type.
│  kb/career/    │
│  packs/        │
│  {role}.md     │
└────────┬───────┘
         │
         ▼
   Is there a company pack?
   (user set: JARVIS_ACTIVE_COMPANY)
         │
   YES ──┴── NO
    │         │
    ▼         ▼
┌────────────────┐   Use role pack only.
│  LAYER 2:      │   No company-specific
│  Company Pack  │   context contamination.
│  kb/career/    │
│  packs/        │
│  {co}_{role}.md│
└────────────────┘
```

### State Variables
These are set by the user (or auto-detected from context):
```
JARVIS_ACTIVE_ROLE=null          # e.g. "youtube_pem_2026"
JARVIS_ACTIVE_COMPANY=null       # e.g. "youtube"
JARVIS_INTERVIEW_MODE=false      # activates interview behavior profile
```

### Pack Loading Rules
- Universal base: ALWAYS loaded when interview domain detected, regardless of packs
- Target-role pack: loaded ONLY when JARVIS_ACTIVE_ROLE is set
- Company pack: loaded ONLY when both JARVIS_ACTIVE_COMPANY is set AND a matching
  pack file exists at `kb/career/packs/{company}_{role}.md`
- If no pack matches: fallback to universal base + role-agnostic domain inference
- Packs SUPPLEMENT the universal base — they never override or replace it

---

## SECTION 6: MEMORY ARCHITECTURE OVERVIEW

Full schema in `02_MEMORY_ARCHITECTURE.md`. Summary:

| Layer | Name | Scope | Persistence | Privacy |
|-------|------|-------|-------------|---------|
| L0 | Core Identity | Always in context | Immutable | Public |
| L1 | Semantic Memory | Retrieved per query | Persistent | Semi-private |
| L2 | Episodic Memory | Retrieved per query | Persistent | Semi-private |
| L3 | Working Memory | Session-scoped | Volatile | Session |
| L4 | Private Vault | Explicit unlock only | Persistent | Encrypted |

---

## SECTION 7: CAPABILITY MODULES OVERVIEW

Full specs in `03_CAPABILITY_MODULES.md`. Summary:

| Module | Domain | Key Behaviors |
|--------|--------|---------------|
| InterviewIntel | Career/Interview | Layered pack loading, STAR story routing, voice enforcement |
| CareerOS | Job Search | Application tracking, outreach drafting, negotiation strategy |
| TechAssist | Technical | Code, SQL, debugging, system design — with Aman's patterns |
| StrategyOS | Decision-making | Tradeoff framing, long-horizon thinking, devil's advocate |
| DailyOS | Life ops | Tasks, notes, scheduling, reminders, quick drafts |
| ResearchEngine | Research | Deep synthesis, source triangulation, structured briefings |
| CommunicationCraft | Writing | Voice-matched drafts, tone calibration, email/message generation |
| MemoryManager | Meta | Explicit memory reads/writes, knowledge base updates |

---

## SECTION 8: MODEL ROUTING

Jarvis is model-agnostic. The harness selects the right model per task:

```python
def select_model(task_type, context_length, latency_requirement):
    if task_type == "chat" and latency_requirement == "fast":
        return LOCAL_MODEL  # Mistral-7B, Phi-3, Llama3-8B
    elif task_type == "code":
        return CODE_MODEL   # Codestral, DeepSeek-Coder-33B
    elif task_type == "long_context" and context_length > 32000:
        return LONG_CONTEXT_MODEL  # Gemini 1.5 Pro, Claude 3.5
    elif task_type in ["strategy", "complex_reasoning", "interview"]:
        return PREMIUM_MODEL  # GPT-4o, Claude 3.5 Sonnet
    else:
        return DEFAULT_MODEL  # Configurable fallback
```

### Model Registry (configure in .env)
```
JARVIS_LOCAL_MODEL=mistral:7b-instruct
JARVIS_CODE_MODEL=codestral:latest
JARVIS_PREMIUM_MODEL=gpt-4o
JARVIS_LONG_CONTEXT_MODEL=claude-3-5-sonnet-20241022
JARVIS_DEFAULT_MODEL=mistral:7b-instruct
```

---

## SECTION 9: TOOLS LAYER

Jarvis has access to the following tool categories. Each capability module
declares which tools it can invoke:

| Tool Category | Examples | Modules With Access |
|---------------|---------|---------------------|
| web_search | Tavily, SerpAPI, Brave | ResearchEngine, CareerOS |
| web_fetch | URL content retrieval | ResearchEngine, InterviewIntel |
| file_ops | read/write local files | All modules |
| code_exec | Python sandbox, SQL runner | TechAssist |
| calendar | read events, create reminders | DailyOS |
| email_draft | compose, thread analysis | CommunicationCraft, CareerOS |
| memory_read | semantic + episodic retrieval | All modules |
| memory_write | store new facts/events | All modules |
| kb_read | load KB files | All modules |
| kb_write | update KB files | MemoryManager only |

---

## SECTION 10: EVALUATION AND REFLECTION

### Response Quality Signals
Jarvis evaluates its own responses against these criteria:

1. **Specificity score** — Does the response reference Aman's actual experience,
   not generic best practices? (0–1)
2. **Memory utilization** — Did Jarvis use relevant memory that was available? (0–1)
3. **Voice fidelity** — Does the response match Aman's communication style? (0–1)
4. **Module correctness** — Was the right module activated? (0–1)
5. **Context contamination** — Did role-specific content leak into universal responses?
   (0 = no leaks, 1 = contaminated)

### Reflection Triggers
Auto-retry if:
- Specificity score < 0.6
- Response uses generic phrases on the never-use list
- Context contamination > 0

Log for review if:
- Memory was available but not used
- Module confidence was LOW and routing assumption was wrong
- User immediately rephrases the same question (implicit rejection signal)

---

## SECTION 11: PRIVACY AND SECURITY MODEL

### Memory Privacy Tiers
```
PUBLIC    → can be sent to any model, any API
SEMI-PRIVATE → prefer local model; if external, strip PII before sending
PRIVATE   → local model ONLY; never sent to external API
ENCRYPTED → requires explicit unlock; stored encrypted at rest
```

### Context Contamination Rules
- Professional context (L0, L1 career) NEVER mixes with personal context (L4)
  in the same prompt unless user explicitly requests it
- Interview packs are loaded in isolation — universal base + target pack + company pack
  in a dedicated context window, not the general working memory context
- Private vault (L4) requires:
  1. Explicit user command: `jarvis unlock private`
  2. Local model only (no external API calls while private context is active)
  3. Auto-lock after session end

### .gitignore Requirements
```
# Always gitignore these
kb/personal/
.env
*.vault
memory/episodic/personal/
memory/semantic/private/
```

---

## SECTION 12: FILE STRUCTURE

```
Jarvis/
├── 00_ARCHITECTURE.md          ← This file
├── 01_SYSTEM_PROMPT.md         ← Injectable system prompt
├── 02_MEMORY_ARCHITECTURE.md   ← Memory layer schemas
├── 03_CAPABILITY_MODULES.md    ← Module specs
├── 04_BEHAVIORAL_RULES.md      ← Behavioral spec
│
├── kb/                         ← Knowledge base
│   ├── core/
│   │   └── identity.md         ← Always-loaded (L0)
│   ├── career/
│   │   ├── universal_base.md   ← Pointer to Jarvis_Universal_Interview_Context.md
│   │   └── packs/
│   │       ├── _template.md    ← Template for new role packs
│   │       └── youtube_pem_2026.md  ← YouTube PEM pack (extracted from universal doc)
│   ├── technical/
│   │   ├── sql_patterns.md     ← SQL knowledge
│   │   └── python_patterns.md  ← Python patterns
│   ├── strategy/
│   │   └── decision_frameworks.md
│   └── personal/               ← .gitignored
│       └── .gitkeep
│
├── memory/                     ← Runtime memory store
│   ├── semantic/               ← Vector-indexed facts
│   ├── episodic/               ← Timestamped events
│   └── working/                ← Session state
│
└── evals/                      ← Evaluation logs
    └── reflection_log.jsonl
```

---

## SECTION 13: CONFIGURATION

### jarvis.config.json
```json
{
  "version": "1.0",
  "identity": "kb/core/identity.md",
  "system_prompt": "01_SYSTEM_PROMPT.md",
  "behavioral_rules": "04_BEHAVIORAL_RULES.md",
  "memory": {
    "semantic_store": "memory/semantic/",
    "episodic_store": "memory/episodic/",
    "working_store": "memory/working/",
    "top_k_semantic": 5,
    "top_k_episodic": 3,
    "private_store": "reserved for a future encrypted local store"
  },
  "models": {
    "local": "mistral:7b-instruct",
    "code": "codestral:latest",
    "premium": "gpt-4o",
    "long_context": "claude-3-5-sonnet-20241022",
    "default": "mistral:7b-instruct"
  },
  "interview": {
    "active_role": null,
    "active_company": null,
    "mode": false,
    "universal_base": "kb/career/universal_base.md",
    "packs_dir": "kb/career/packs/"
  },
  "privacy": {
    "default_tier": "semi-private",
    "external_api_allowed": ["public", "semi-private"],
    "local_only": ["private", "encrypted"]
  },
  "reflection": {
    "enabled": true,
    "specificity_threshold": 0.6,
    "log_path": "evals/reflection_log.jsonl"
  }
}
```

---

*Architecture version: 1.0*
*Last updated: April 2026*
