CORE_GOLDEN_CASES = [
    {
        "id": "personal_context_alignment",
        "suite": "core",
        "prompt": "Tell me something interesting based on what you know about me.",
        "expected_label": "Status",
        "must_include_any": [
            "Anthropic",
            "Jarvis AI",
            "AI Malware Detection System",
            "MindRight App",
        ],
    },
    {
        "id": "self_improve_evidence_gate",
        "suite": "core",
        "prompt": "If I ask you to improve yourself right now, what evidence would you need before changing code?",
        "expected_label": "Self-Improve",
        "must_include_all": [
            "recent eval failures",
            "at least two",
            "syntax-validate",
        ],
    },
    {
        "id": "vault_citation_summary",
        "suite": "core",
        "prompt": "Search the vault for Jarvis Vault Strategy and summarize it in two sentences with the exact local file and heading you used.",
        "expected_label": "Knowledge",
        "must_include_all": [
            "raw/jarvis_vault_strategy.md",
            "Jarvis Vault Strategy",
        ],
    },
    {
        "id": "browser_api_summary",
        "suite": "core",
        "prompt": "Browse to openai.com, click API, and then summarize the page you land on in two sentences.",
        "expected_label": "Browser",
        "must_include_any": [
            "https://openai.com/api/",
            "OpenAI API",
            "API platform",
        ],
    },
    {
        "id": "science_entropy_expert",
        "suite": "core",
        "prompt": "What is the difference between entropy in thermodynamics and entropy in information theory?",
        # Accepts both specialized-agent path (cloud) and direct local path (open-source mode)
        "expected_label": "Open-Source",
        "must_include_all": [
            "thermodynamics",
        ],
        "must_include_any": [
            "information theory",
            "Shannon entropy",
            "uncertainty",
            "microscopic",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
            "I wasn't able to complete",
        ],
    },
    {
        "id": "locking_tradeoff_answer",
        "suite": "core",
        "prompt": "Compare optimistic locking and pessimistic locking and tell me when each one is the better choice.",
        "expected_label": "Sonnet",
        "must_include_all": [
            "Optimistic locking",
            "Pessimistic locking",
        ],
        "must_include_any": [
            "tradeoff",
            "throughput",
            "conflict",
        ],
    },
    {
        "id": "python_memory_leak_triage",
        "suite": "core",
        "prompt": "I have a Python service leaking memory over time. Give me the most likely causes and a concrete debugging sequence.",
        "expected_label": "Specialized Agents",
        "must_include_any": [
            "cache",
            "circular",
            "connection",
            "client",
            "diagnosis",
            "objgraph",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
        ],
    },
    {
        "id": "self_review_shortcomings",
        "suite": "core",
        "prompt": "Review your own code and tell me your top shortcomings.",
        "expected_label": "Self-Review",
        "must_include_any": [
            "shortcomings",
            "weakness",
            "recent eval evidence",
            "failure",
        ],
    },
]


ENGINEERING_GOLDEN_CASES = [
    {
        "id": "fastapi_nginx_502_debug",
        "suite": "engineering",
        "prompt": "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence.",
        "expected_label": "Specialized Agents",
        "must_include_any": [
            "0.0.0.0",
            "proxy_pass",
            "upstream",
            "port",
            "timeout",
            "logs",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
        ],
    },
    {
        "id": "auth_flow_security_review",
        "suite": "engineering",
        "prompt": "Review this authentication design for security issues. It stores JWT access tokens in localStorage and trusts frontend role checks before showing admin actions.",
        "expected_label": "Specialized Agents",
        "must_include_any": [
            "localStorage",
            "XSS",
            "server-side",
            "authorization",
            "token",
            "permissions",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
        ],
    },
    {
        "id": "database_index_tradeoff",
        "suite": "engineering",
        "prompt": "When should I add a database index, and when can it hurt performance?",
        "expected_label": "Sonnet",
        "must_include_any": [
            "read performance",
            "write amplification",
            "storage",
            "selective",
            "insert",
        ],
    },
    {
        "id": "postgres_zero_downtime_required_column",
        "suite": "engineering",
        "prompt": "Give me a zero-downtime rollout plan for making a nullable Postgres column required in production.",
        # Local model answers this well — accept both routing paths
        "expected_label": "Open-Source",
        "must_include_any": [
            "backfill",
            "constraint",
            "NOT NULL",
            "two-phase",
            "rollback",
            "validate",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
            "I wasn't able to complete",
        ],
    },
    {
        "id": "python_race_condition_debug",
        "suite": "engineering",
        "prompt": "I think I have a race condition in a Python worker. How would you narrow it down and make it reproducible?",
        "expected_label": "Specialized Agents",
        "must_include_any": [
            "shared state",
            "logging",
            "thread",
            "lock",
            "reproduce",
            "stress test",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
            "I wasn't able to complete",
            "Local model error",
        ],
    },
    {
        "id": "stale_read_cache_vs_replica",
        "suite": "engineering",
        "prompt": "Users sometimes see stale data after writes. How would you debug whether this is a cache invalidation problem or a replica lag problem?",
        "expected_label": "Specialized Agents",
        "must_include_any": [
            "cache invalidation",
            "replica lag",
            "read-after-write",
            "primary",
            "TTL",
            "correlation",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
            "I wasn't able to complete",
            "Local model error",
        ],
    },
]


GOLDEN_CASES = CORE_GOLDEN_CASES + ENGINEERING_GOLDEN_CASES
