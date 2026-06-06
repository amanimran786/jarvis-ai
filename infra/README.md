# Jarvis Infrastructure Layer

## Production Folder Structure

```
jarvis-ai/
├── api.py                      # Main Jarvis API (port 8765) — OpenAI-compat + agent endpoints
├── config.py                   # Runtime defaults, AGENT_ROSTER, model identifiers
├── agent_dispatch.py           # Routes tasks to agents via ask_local_with_tools
├── model_router.py             # Model selection logic (local vs cloud)
├── orchestrator.py             # Request/runtime coordination
├── router.py                   # Intent routing before LLM
│
├── brains/
│   ├── brain_ollama.py         # Ollama inference + agentic tool loop
│   ├── brain_claude.py         # Claude API backend
│   ├── brain_gemini.py         # Gemini API backend
│   └── brain.py                # Brain selector
│
├── db/
│   └── schema.sql              # PostgreSQL schema (memory, tasks, audit, RBAC)
│
├── infra/
│   ├── event_bus.py            # FastAPI event bus + scheduler (port 8766)
│   ├── Dockerfile.api          # Shared Dockerfile for api + event_bus services
│   └── README.md               # This file
│
├── local_runtime/              # STT, TTS, Kokoro, local model runtimes
├── memory/                     # mem0, Qdrant, memory layer implementations
├── agents/                     # Specialist agent implementations
├── skills/                     # Registered tool skill definitions
├── vault/                      # Obsidian brain (write-on-approval only)
├── tests/                      # pytest suite
│
├── docker-compose.yml          # Infra stack; uses host Ollama by default on macOS
└── requirements.txt
```

## Service Ports

| Service    | Port  | Purpose                                     |
|------------|-------|---------------------------------------------|
| Jarvis API | 8765  | Main API — chat, agents, OpenAI-compat      |
| Event Bus  | 8766  | Redis Streams scheduler + approval gateway  |
| Ollama     | 11434 | Local LLM inference                         |
| OpenClaw   | 18789 | Multi-channel gateway (iMessage, Telegram)  |
| Redis      | 6379  | Event bus, agent inboxes, rate limiting     |
| PostgreSQL | 5432  | Audit log, memory, RBAC                     |
| Qdrant     | 6333  | Vector search (nomic-embed-text 768-dim)    |

## Task Routing Flow

```
Human (desktop/voice/iMessage)
    │
    ▼  POST /tasks
Event Bus (8766)
    │
    ▼  xadd → jarvis:tasks stream
AgentScheduler (reads jarvis:tasks, GROUP=scheduler)
    │
    ▼  xadd → jarvis:agent:{name}
Agent Process  (reads its own inbox, runs ask_local_with_tools)
    │
    ▼  POST /results → jarvis:results stream
ManagerLoop   (reads jarvis:results, GROUP=manager)
    │
    ├── needs_review=false → log + done
    └── needs_review=true  → xadd → jarvis:approvals
                                        │
                                        ▼
                               Human reviews via GET /approvals/pending
                               DELETE /approvals/{id} to ack
```

## Development (local, no Docker)

```bash
# Start infrastructure
redis-server &
docker run -p 5432:5432 -e POSTGRES_PASSWORD=dev pgvector/pgvector:pg16
OLLAMA_CONTEXT_LENGTH=64000 ollama serve &

# Apply schema
psql -U jarvis -d jarvis -f db/schema.sql

# Run event bus
uvicorn infra.event_bus:app --port 8766 --reload

# Run main API
python main.py --no-ui
```

## Development (Docker infra + host Ollama on macOS)

```bash
OLLAMA_CONTEXT_LENGTH=64000 ollama serve &
docker compose up redis postgres qdrant event_bus jarvis_api backend_worker
```

The compose file keeps containerized Ollama behind the `container-ollama`
profile because Jarvis runs best on the M4 Pro through the host Ollama app.
