"""
Jarvis local API server — runs on http://127.0.0.1:8765

Starts automatically inside the Jarvis process (GUI or --no-ui).
Shares the same memory, model router, and conversation state.

Endpoints:
  POST /chat          — send a message, get a response (or stream it)
  POST /feedback      — log a bad answer or tool failure
  GET  /evals/summary — inspect recent eval state
  GET  /status        — current mode, online check
  GET  /memory        — facts, topics, recent conversations
  POST /memory/add    — add a fact
  POST /memory/forget — forget by keyword
  GET  /mode          — current model routing mode
  POST /mode          — set mode: {"mode": "local"|"cloud"|"auto"}
"""

import json
import threading
import uvicorn
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from router import route_stream
import memory as mem
import model_router
import hardware as hw
import evals

app = FastAPI(title="Jarvis", version="1.0")


# ── Request models ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    stream: bool = False


class FactRequest(BaseModel):
    fact: str


class ForgetRequest(BaseModel):
    keyword: str


class ModeRequest(BaseModel):
    mode: str


class FeedbackRequest(BaseModel):
    issue: str
    interaction_id: str | None = None
    expected: str = ""
    user_input: str = ""
    response: str = ""
    model: str = ""
    source: str = "user_feedback"


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    """Send a message to Jarvis and get a response."""
    if req.stream:
        def generate():
            stream, model = route_stream(req.message)
            chunks = []
            for chunk in stream:
                chunks.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk, 'model': model})}\n\n"
            response = "".join(chunks)
            interaction = evals.log_interaction(req.message, response, model, source="api_stream")
            evals.maybe_log_automatic_failure(interaction)
            yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'type': 'meta'})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    stream, model = route_stream(req.message)
    response = "".join(stream)
    interaction = evals.log_interaction(req.message, response, model)
    evals.maybe_log_automatic_failure(interaction)
    return {"response": response, "model": model, "interaction_id": interaction["id"]}


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    entry = evals.log_failure(
        issue=req.issue,
        interaction_id=req.interaction_id,
        expected=req.expected,
        user_input=req.user_input,
        response=req.response,
        model=req.model,
        source=req.source,
    )
    return {"ok": True, "failure": entry}


@app.get("/evals/summary")
def eval_summary(hours: int = 24 * 7):
    return evals.summary(hours=hours)


@app.get("/status")
def status():
    return {
        "status": "online",
        "mode": model_router.get_mode(),
        "local_available": model_router._has_local(),
    }


@app.get("/memory")
def get_memory():
    return {
        "facts": mem.list_facts(),
        "preferences": mem.get_all_preferences(),
        "top_topics": mem.get_top_topics(5),
        "recent_conversations": mem.get_recent_conversations(3),
    }


@app.post("/memory/add")
def add_memory(req: FactRequest):
    mem.add_fact(req.fact)
    return {"ok": True, "fact": req.fact}


@app.post("/memory/forget")
def forget_memory(req: ForgetRequest):
    removed = mem.forget(req.keyword)
    return {"ok": removed, "keyword": req.keyword}


@app.get("/mode")
def get_mode():
    return {"mode": model_router.get_mode()}


@app.post("/mode")
def set_mode(req: ModeRequest):
    result = model_router.set_mode(req.mode)
    return {"ok": True, "message": result, "mode": model_router.get_mode()}


# ── Server startup ─────────────────────────────────────────────────────────────

_port: int = 8765  # actual port after binding


def _find_free_port(start: int = 8765, attempts: int = 10) -> int:
    import socket
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port found in range 8765-8774")


def get_port() -> int:
    return _port


hw.register_api_routes(app)


def start(host: str = "127.0.0.1", port: int = 8765) -> threading.Thread:
    """Start the API server in a background daemon thread."""
    global _port
    _port = _find_free_port(port)

    def _run():
        uvicorn.run(app, host=host, port=_port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="JarvisAPI")
    t.start()
    # Write port to file so jarvis_cli.py can find it
    import os
    port_file = os.path.join(os.path.dirname(__file__), ".jarvis_port")
    with open(port_file, "w") as f:
        f.write(str(_port))
    print(f"[API] Jarvis API running at http://{host}:{_port}")
    return t
