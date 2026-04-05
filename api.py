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
import conversation_context as ctx
import vault
import source_ingest
import skill_factory
import local_training

app = FastAPI(title="Jarvis", version="1.0")
_CHAT_LOCK = threading.Lock()


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


class VaultIngestRequest(BaseModel):
    source: str
    source_type: str = "auto"
    auto_build: bool = True


class SkillCreateRequest(BaseModel):
    query: str
    tool: str = "chat"
    cost_hint: str = "local"


class SkillPromoteRequest(BaseModel):
    min_failures: int = 2


class SelfReviewRequest(BaseModel):
    area: str = ""


class LocalTrainingExportRequest(BaseModel):
    limit: int = 150
    cloud_only: bool = True


class LocalTrainingDistillRequest(BaseModel):
    limit: int = 12
    teacher_model: str = "claude-sonnet-4-6"


class LocalTrainingModelfileRequest(BaseModel):
    base_model: str = ""
    target_name: str = ""


class LocalTrainingRunRequest(BaseModel):
    export_limit: int = 150
    distill_limit: int = 8
    teacher_model: str = "claude-sonnet-4-6"
    cloud_only_export: bool = True
    base_model: str = ""
    target_name: str = ""


class LocalTrainingHandoffRequest(BaseModel):
    pack_path: str = ""
    targets: list[str] = []


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.post("/chat")
def chat(req: ChatRequest):
    """Send a message to Jarvis and get a response."""
    if req.stream:
        def generate():
            with _CHAT_LOCK:
                stream, model = route_stream(req.message)
                chunks = []
                for chunk in stream:
                    chunks.append(chunk)
                    yield f"data: {json.dumps({'chunk': chunk, 'model': model})}\n\n"
                response = "".join(chunks)
                context_stats = ctx.record_request_stats(model, source="api_stream")
                interaction = evals.log_interaction(req.message, response, model, source="api_stream", context=context_stats)
                evals.maybe_log_automatic_failure(interaction)
                yield f"data: {json.dumps({'interaction_id': interaction['id'], 'model': model, 'type': 'meta'})}\n\n"
                yield "data: [DONE]\n\n"
        return StreamingResponse(generate(), media_type="text/event-stream")

    with _CHAT_LOCK:
        stream, model = route_stream(req.message)
        response = "".join(stream)
        context_stats = ctx.record_request_stats(model, source="api")
        interaction = evals.log_interaction(req.message, response, model, context=context_stats)
        evals.maybe_log_automatic_failure(interaction)
        return {"response": response, "model": model, "interaction_id": interaction["id"], "context": context_stats}


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
        "context": ctx.get_stats(),
    }


@app.get("/context")
def get_context_stats():
    return {
        "current": ctx.get_stats(),
        "recent_requests": ctx.recent_request_stats(10),
    }


@app.get("/vault")
def get_vault_status():
    return vault.status()


@app.post("/vault/build")
def build_vault():
    message = vault.build_wiki_text()
    return {"ok": True, "message": message, "vault": vault.status()}


@app.post("/vault/ingest")
def ingest_vault(req: VaultIngestRequest):
    result = source_ingest.ingest_source(req.source, source_type=req.source_type, auto_build=req.auto_build)
    return {"ok": result.get("ok", False), "message": source_ingest.result_text(result), "result": result, "vault": vault.status()}


@app.post("/skills/create")
def create_skill(req: SkillCreateRequest):
    result = skill_factory.create_skill_from_vault(req.query, tool=req.tool, cost_hint=req.cost_hint)
    return {"ok": result.get("ok", False), "message": skill_factory.result_text(result), "result": result}


@app.post("/skills/promote")
def promote_skills(req: SkillPromoteRequest):
    result = skill_factory.promote_failures(min_failures=req.min_failures)
    return {"ok": result.get("ok", False), "message": skill_factory.result_text(result), "result": result}


@app.get("/local/training/status")
def get_local_training_status():
    return {"ok": True, "status": local_training.status()}


@app.post("/local/training/export")
def export_local_training(req: LocalTrainingExportRequest):
    result = local_training.export_sft_dataset(limit=req.limit, cloud_only=req.cloud_only)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/distill")
def distill_local_training(req: LocalTrainingDistillRequest):
    result = local_training.distill_failures(limit=req.limit, teacher_model=req.teacher_model)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/modelfile")
def build_local_training_modelfile(req: LocalTrainingModelfileRequest):
    kwargs = {}
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.target_name:
        kwargs["target_name"] = req.target_name
    result = local_training.build_modelfile(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/run")
def run_local_training(req: LocalTrainingRunRequest):
    kwargs = {
        "export_limit": req.export_limit,
        "distill_limit": req.distill_limit,
        "teacher_model": req.teacher_model,
        "cloud_only_export": req.cloud_only_export,
    }
    if req.base_model:
        kwargs["base_model"] = req.base_model
    if req.target_name:
        kwargs["target_name"] = req.target_name
    result = local_training.build_training_pack(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.post("/local/training/handoff")
def build_local_training_handoff(req: LocalTrainingHandoffRequest):
    kwargs = {}
    if req.pack_path:
        kwargs["pack_path"] = req.pack_path
    if req.targets:
        kwargs["targets"] = req.targets
    result = local_training.build_finetune_handoff(**kwargs)
    return {"ok": result.get("ok", False), "message": local_training.result_text(result), "result": result}


@app.get("/self/review")
def get_self_review(area: str = ""):
    import self_improve as si
    result = si.self_review(area=area or None)
    return {"ok": result.get("ok", False), "message": si.review_text(result), "result": result}


@app.post("/self/review")
def post_self_review(req: SelfReviewRequest):
    import self_improve as si
    result = si.self_review(area=req.area or None)
    return {"ok": result.get("ok", False), "message": si.review_text(result), "result": result}


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
