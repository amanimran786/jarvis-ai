# <div align="center">Jarvis AI</div>

<div align="center">
  <img src="assets/readme-hero.svg" alt="Jarvis AI hero" width="100%" />
</div>

<div align="center">

[![Platform](https://img.shields.io/badge/platform-macOS-111827?style=for-the-badge&logo=apple)](https://www.apple.com/macos/)
[![Python](https://img.shields.io/badge/python-3.12+-1f6feb?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Ollama](https://img.shields.io/badge/local_models-Ollama-0f172a?style=for-the-badge)](https://ollama.com/)
[![Desktop App](https://img.shields.io/badge/desktop-PyQt6-12324a?style=for-the-badge)](ui.py)
[![Mode](https://img.shields.io/badge/default-open--source-0b7285?style=for-the-badge)](config.py)

</div>

Jarvis is a local-first desktop AI assistant for macOS.

The simple version:

- you talk to it
- it understands your request
- it looks up the right memory, tools, and context
- it answers or acts on your Mac

The ambitious version:

Jarvis is trying to become your own private, open-source-first desktop intelligence layer. Not just a chat box. Not just a prompt wrapper. A real system that can think, listen, see, remember, inspect code, control tools, and keep improving over time.

## Start Here

If you are new to this repo, read these sections in order:

1. `What Is Jarvis?`
2. `How Jarvis Works`
3. `Current State`
4. `Roadmap`
5. `Repo Map`

## What Is Jarvis?

Imagine a helpful robot friend living on your computer.

That friend should be able to:

- hear you
- talk back
- look at your screen
- remember important things
- help with coding
- use tools on your Mac
- stay private by running locally whenever possible

That is what Jarvis is meant to be.

For engineers, the more exact definition is:

Jarvis is a local-first assistant runtime with:

- a desktop app
- a local API
- model routing
- memory and retrieval
- skills, connectors, and plugins
- a managed task runtime
- multimodal perception
- local-model tuning and eval loops

## Why This Repo Exists

Most AI products today depend on cloud models as the foundation.

Jarvis is trying to invert that:

- local by default
- cloud only as fallback or explicit escalation
- grounded in your files, tools, and environment
- honest about what it knows and what it does not know

## Jarvis In One Diagram

```mermaid
flowchart LR
    User["You"] --> UI["Desktop UI / CLI"]
    UI --> API["Local API"]
    API --> Router["Router + Orchestrator"]
    Router --> Skills["Skills"]
    Router --> Memory["Memory + Vault + Graph Context"]
    Router --> Tools["Tools + Connectors"]
    Router --> Models["Local Models"]
    Router --> Tasks["Managed Task Runtime"]
    Tools --> Mac["Browser / Terminal / macOS / Devices"]
```

## How Jarvis Works

Every request follows roughly this path:

1. You ask Jarvis something.
2. Jarvis decides what kind of request it is.
3. Jarvis loads the right skills and context.
4. Jarvis chooses the right model and tools.
5. Jarvis answers, acts, or starts a managed task.
6. Jarvis can store useful memory for later.

## The Main Systems

### 1. Desktop App

This is the visible Jarvis window and compact shell.

Main files:

- [main.py](main.py)
- [ui.py](ui.py)
- [desktop/](desktop)

What it does:

- shows the UI
- starts the local API/runtime
- lets you talk to Jarvis like a desktop product instead of a terminal script

### 2. Local API

This is the shared runtime surface that both the desktop app and CLI can talk to.

Main file:

- [api.py](api.py)

What it does:

- exposes chat, memory, runtime, task, skills, connectors, and plugin endpoints
- makes Jarvis act like a real local service instead of a one-off script

### 3. Router and Orchestrator

This is the decision-making layer before any model answers.

Main files:

- [router.py](router.py)
- [orchestrator.py](orchestrator.py)
- [model_router.py](model_router.py)

What it does:

- figures out what kind of request you made
- decides whether Jarvis should answer directly, use a skill, or start a task
- chooses the model path
- attaches relevant context first

### 4. Memory and Grounding

This is how Jarvis avoids feeling generic.

Main files:

- [memory.py](memory.py)
- [semantic_memory.py](semantic_memory.py)
- [vault.py](vault.py)
- [graph_context.py](graph_context.py)

What it does:

- stores facts and preferences
- searches local markdown knowledge
- grounds repo questions in generated graph artifacts
- gives Jarvis durable context across sessions

### 5. Skills, Connectors, and Plugins

This is Jarvis's extensibility layer.

Main files:

- [skills/](skills)
- [skills.py](skills.py)
- [connectors/index.json](connectors/index.json)
- [plugins/index.json](plugins/index.json)
- [extension_registry.py](extension_registry.py)

What each one means:

- `Skills`: instruction packs for specific types of work
- `Connectors`: integrations into real capabilities like browser, terminal, vault, and Google Workspace
- `Plugins`: bundles of skills, connectors, and agents that feel like complete features

### 6. Managed Task Runtime

This is the part that lets Jarvis act more like an operator than a chatbot.

Main files:

- [task_runtime.py](task_runtime.py)
- [jarvis_daemon.py](jarvis_daemon.py)
- [worktree_manager.py](worktree_manager.py)

What it does:

- registers named agents
- creates and tracks tasks
- streams task output
- prepares isolated code workspaces for code tasks

### 7. Voice, Meetings, Vision, and Device Awareness

This is the multimodal layer.

Main files:

- [voice.py](voice.py)
- [meeting_listener.py](meeting_listener.py)
- [camera.py](camera.py)
- [browser.py](browser.py)
- [hardware.py](hardware.py)

What it does:

- speech input
- speech output
- meeting assist
- screen and camera understanding
- browser control
- nearby device awareness

## Current Capability Overview

| Area | What Jarvis can do now |
|---|---|
| Chat | Answer questions, explain concepts, plan work, and reason about technical topics |
| Coding | Read the repo, explain code, debug problems, review code, and run managed coding tasks |
| Voice | Use local STT and local TTS in the main path |
| Memory | Remember facts, preferences, projects, and recent conversation summaries |
| Repo grounding | Use Graphify and vault-based context instead of pure guesswork |
| Browser + system | Read pages, click controls, open apps, change settings, and take screenshots |
| Managed runtime | Inspect agents, create tasks, stream task output, and isolate code-task workspaces |
| Extensions | Expose discoverable skills, connectors, and plugins through API and CLI |

## Current Local Stack

Jarvis is configured around a local-first stack:

- default local chat: `gemma4:e4b`
- deeper local reasoning model: `deepseek-r1:14b`
- local coding model: `qwen2.5-coder:7b`
- local STT: `faster-whisper`
- local TTS: macOS `say`
- local embeddings model available: `nomic-embed-text`
- local vision model available: `llava:7b`

You can inspect the live runtime stack with:

```bash
curl http://127.0.0.1:8765/local/capabilities
```

## Honest Current State

This project is strong enough to be useful now, but it is not done.

Important truth:

- Jarvis is local-first today
- Jarvis is not yet perfectly 100 percent local across every subsystem
- some cloud fallback paths still exist in code
- the repo is actively moving those remaining paths behind stricter open-source-mode boundaries

That honesty matters because the goal here is not marketing. The goal is to build the real thing.

## Architecture Roadmap

Jarvis is moving through a few clear stages.

### Phase 1: Stable Runtime

Goal:

Make Jarvis a real long-lived assistant runtime, not just a window that happens to call a model.

Main work:

- move more ownership into [jarvis_daemon.py](jarvis_daemon.py)
- keep [runtime_state.py](runtime_state.py) as the shared source of truth
- make UI and CLI act like clients of the runtime

### Phase 2: Strong Local Brain

Goal:

Make the default path local for chat, coding, speech, retrieval, and vision.

Main work:

- strengthen [model_router.py](model_router.py)
- keep local models as the main path
- reduce or remove cloud dependency from meeting, vision, and retrieval paths

### Phase 3: Better Grounding

Goal:

Make Jarvis answer from memory, repo structure, and real observations instead of fluent guessing.

Main work:

- improve [semantic_memory.py](semantic_memory.py)
- unify memory, vault, and graph grounding
- add stronger retrieval and reranking

### Phase 4: Better Tools and Actions

Goal:

Let Jarvis act safely and reliably on the machine.

Main work:

- cleaner connector boundaries
- better verification for multi-step actions
- clearer safe vs privileged tool categories

### Phase 5: Multimodal Jarvis

Goal:

Make Jarvis feel like a real desktop assistant, not just a text engine.

Main work:

- better local meeting assist
- stronger local screen and camera understanding
- better device and environment awareness

### Phase 6: Self-Improving Jarvis

Goal:

Let Jarvis get better through evals and controlled local-model improvement.

Main work:

- stronger eval suites
- safer self-improvement loops
- better local-model training and promotion workflows

## Repo Map

If the repo feels big, this is the shortest useful map:

| Path | Purpose |
|---|---|
| [main.py](main.py) | App startup |
| [ui.py](ui.py) | Main desktop UI |
| [api.py](api.py) | Local API surface |
| [router.py](router.py) | Main request routing |
| [model_router.py](model_router.py) | Model choice and mode policy |
| [memory.py](memory.py) | Stored facts and profile memory |
| [semantic_memory.py](semantic_memory.py) | Retrieval over memory data |
| [vault.py](vault.py) | Local markdown knowledge vault |
| [graph_context.py](graph_context.py) | Repo graph grounding |
| [voice.py](voice.py) | Speech in and speech out |
| [meeting_listener.py](meeting_listener.py) | Meeting assist |
| [camera.py](camera.py) | Screen and camera understanding |
| [browser.py](browser.py) | Browser control |
| [hardware.py](hardware.py) | Mac and device awareness |
| [task_runtime.py](task_runtime.py) | Managed task system |
| [skills/](skills) | Skill packs |
| [connectors/](connectors) | Connector definitions |
| [plugins/](plugins) | Plugin definitions |
| [docs/jarvis_architecture/](docs/jarvis_architecture) | Architecture docs and roadmap |

## Extension Surface

Jarvis now exposes a Claude-style discovery layer:

```bash
./venv/bin/python jarvis_cli.py --skills
./venv/bin/python jarvis_cli.py --connectors
./venv/bin/python jarvis_cli.py --plugins
curl http://127.0.0.1:8765/extensions
```

## API Surface

Core runtime:

- `GET /status`
- `GET /runtime/state`
- `POST /chat`
- `GET /mode`
- `POST /mode`

Extensions:

- `GET /extensions`
- `GET /skills`
- `GET /skills/{skill_id}`
- `GET /connectors`
- `GET /connectors/{connector_id}`
- `GET /plugins`
- `GET /plugins/{plugin_id}`

Managed runtime:

- `GET /agents`
- `GET /tasks`
- `POST /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/events`
- `GET /tasks/{task_id}/stream`

Memory and vault:

- `GET /memory`
- `GET /memory/status`
- `POST /memory/add`
- `POST /memory/forget`
- `GET /vault`
- `POST /vault/build`

## CLI Examples

```bash
./venv/bin/python jarvis_cli.py --status
./venv/bin/python jarvis_cli.py --skills
./venv/bin/python jarvis_cli.py --connectors
./venv/bin/python jarvis_cli.py --plugins
./venv/bin/python jarvis_cli.py "explain optimistic locking like I'm 10"
./venv/bin/python jarvis_cli.py --task "summarize the current repo architecture"
./venv/bin/python jarvis_cli.py --task-code "refactor the auth middleware"
```

## Quick Start

### 1. Clone and create a venv

```bash
git clone https://github.com/amanimran786/jarvis-ai.git
cd jarvis-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Install and start Ollama

```bash
brew install ollama
brew services start ollama
```

### 3. Pull the recommended local models

```bash
ollama pull gemma4:e4b
ollama pull deepseek-r1:14b
ollama pull qwen2.5-coder:7b
ollama pull nomic-embed-text
ollama pull llava:7b
```

### 4. Run Jarvis

```bash
./run.sh
```

Headless:

```bash
./run.sh --no-ui
```

### 5. Build and install the desktop app

```bash
bash scripts/install_jarvis_app.sh
```

That rebuilds the latest packaged app and installs it to both:

- `Applications/Jarvis.app`
- `Desktop/Jarvis.app`

## Architecture Docs

For the deeper system docs, start here:

- [docs/jarvis_architecture/00_ARCHITECTURE.md](docs/jarvis_architecture/00_ARCHITECTURE.md)
- [docs/jarvis_architecture/02_MEMORY_ARCHITECTURE.md](docs/jarvis_architecture/02_MEMORY_ARCHITECTURE.md)
- [docs/jarvis_architecture/03_CAPABILITY_MODULES.md](docs/jarvis_architecture/03_CAPABILITY_MODULES.md)
- [docs/jarvis_architecture/05_OPEN_SOURCE_FIRST_ROADMAP.md](docs/jarvis_architecture/05_OPEN_SOURCE_FIRST_ROADMAP.md)
- [docs/jarvis_architecture/06_PROJECT_AUDIT_2026_04_09.md](docs/jarvis_architecture/06_PROJECT_AUDIT_2026_04_09.md)
- [docs/jarvis_architecture/07_MEMPALACE_GRAPHIFY_ADOPTION.md](docs/jarvis_architecture/07_MEMPALACE_GRAPHIFY_ADOPTION.md)

## Final Mental Model

If you remember only one thing, remember this:

Jarvis is not one model.

Jarvis is:

- a runtime
- a memory system
- a router
- a tool layer
- a desktop app
- an extension surface
- and a roadmap toward a real local AI assistant

The model is just one part inside that machine.
