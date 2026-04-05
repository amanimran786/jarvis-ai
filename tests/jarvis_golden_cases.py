GOLDEN_CASES = [
    {
        "id": "personal_context_alignment",
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
        "prompt": "Search the vault for Jarvis Vault Strategy and summarize it in two sentences with the exact local file and heading you used.",
        "expected_label": "Knowledge",
        "must_include_all": [
            "raw/jarvis_vault_strategy.md",
            "Jarvis Vault Strategy",
        ],
    },
    {
        "id": "browser_api_summary",
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
        "prompt": "What is the difference between entropy in thermodynamics and entropy in information theory?",
        "expected_label": "Specialized Agents",
        "must_include_all": [
            "thermodynamics",
        ],
        "must_include_any": [
            "information theory",
            "Shannon entropy",
        ],
        "must_exclude_all": [
            "Specialized agents used:",
        ],
    },
    {
        "id": "locking_tradeoff_answer",
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
