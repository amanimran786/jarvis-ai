#!/usr/bin/env python3
"""Jarvis AI Dashboard — interactive ops console, server-side rendered."""
import json
import hmac
import os
import secrets
import sys
import threading
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

import runtime_state
from harness.audit import get_audit_error_count
from harness.state_lock import queue_state_lock

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

app = FastAPI()


def _load_dashboard_token() -> str:
    configured = os.getenv("JARVIS_DASHBOARD_TOKEN", "").strip()
    if configured:
        return configured
    token_path = runtime_state.app_data_dir() / ".jarvis_dashboard_token"
    try:
        persisted = token_path.read_text(encoding="utf-8").strip()
        if len(persisted) < 32:
            raise RuntimeError("persisted dashboard token is invalid")
        token_path.chmod(0o600)
        return persisted
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        persisted = token_path.read_text(encoding="utf-8").strip()
        if len(persisted) < 32:
            raise RuntimeError("persisted dashboard token is invalid")
        return persisted
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(generated)
        handle.flush()
        os.fsync(handle.fileno())
    return generated


_DASHBOARD_TOKEN = _load_dashboard_token()
_DASHBOARD_COOKIE = "jarvis_dashboard_token"
_STATE_LOCK = threading.Lock()
_RUN_LOOP_LOCK = threading.Lock()


def _validated_dashboard_host() -> str:
    host = os.getenv("JARVIS_DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Jarvis dashboard must bind to a loopback address")
    return host


def dashboard_bootstrap_url(host: str, port: int = 7842) -> str:
    """Return the local browser URL without sending its token to the server."""
    url_host = "[::1]" if host == "::1" else host
    return f"http://{url_host}:{port}/session-bootstrap#{quote(_DASHBOARD_TOKEN)}"


def _request_token(request: Request) -> str:
    return _bearer_token(request) or request.cookies.get(
        _DASHBOARD_COOKIE, ""
    ).strip()


def _bearer_token(request: Request) -> str:
    bearer = request.headers.get("Authorization", "")
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return ""


@app.middleware("http")
async def _require_dashboard_auth(request: Request, call_next):
    if request.url.path == "/session-bootstrap":
        return await call_next(request)
    supplied = _request_token(request)
    if not supplied or not hmac.compare_digest(supplied, _DASHBOARD_TOKEN):
        return JSONResponse(
            {"error": "dashboard_auth_required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _bearer_token(request):
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.headers.get("Origin", "") != expected_origin:
            return JSONResponse({"error": "dashboard_origin_required"}, status_code=403)
    return await call_next(request)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _load(fname, default):
    try:
        with (BASE / fname).open(encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default

def _save(fname, data):
    path = BASE / fname
    tmp = path.with_suffix(
        path.suffix
        + f".{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
    )
    with _STATE_LOCK:
        try:
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def _badge(s):
    colors = {
        "pending": "#f0c040", "queued": "#f0c040", "in_progress": "#f0c040",
        "running": "#4fc3f7", "active": "#4fc3f7",
        "done": "#66bb6a", "completed": "#66bb6a",
        "blocked": "#ef5350", "failed": "#ef5350", "stalled": "#ef5350",
        "fired": "#ab47bc", "awaiting_approval": "#f0a000",
        "needs_review": "#ffa726",
    }
    c = colors.get(str(s).lower(), "#888")
    return f"<span style='background:{c};color:#111;padding:2px 8px;border-radius:4px;font-size:.82em;white-space:nowrap'>{escape(str(s))}</span>"

def _btn(label, action, color="#4fc3f7", confirm=None):
    onclick = ""
    if confirm:
        expression = f"return confirm({json.dumps(str(confirm))})"
        onclick = f' onclick="{escape(expression, quote=True)}"'
    return (f"<form method='post' action='{escape(str(action), quote=True)}' style='display:inline'>"
            f"<button type='submit'{onclick} style='background:{color};color:#0d1117;border:none;"
            f"padding:3px 11px;border-radius:4px;cursor:pointer;font-family:monospace;font-size:.8em'>"
            f"{escape(str(label))}</button></form>")

def _sessions_list(raw):
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        flat = []
        for k, v in raw.items():
            if isinstance(v, dict):
                flat.append({"_key": k, **v})
            elif isinstance(v, list):
                flat.extend(x for x in v if isinstance(x, dict))
        return flat
    return []

def _log_tail():
    md_path = BASE / "MASTER_LOG.md"
    try:
        lines = [l.strip() for l in md_path.read_text().splitlines() if l.strip()][-30:]
        out = []
        for line in lines:
            c = "#ef5350" if "ERROR" in line or "FAIL" in line.upper() else \
                "#f0c040" if "WARN" in line else "#ccc"
            out.append(f'<span style="color:{c}">{escape(line)}</span>')
        return "<br>".join(out) or "(empty)"
    except Exception:
        pass
    try:
        lines = (BASE / "logs" / "audit.jsonl").read_text().splitlines()[-20:]
        out = []
        for line in lines:
            try:
                obj = json.loads(line)
                ts = str(obj.get("ts", ""))[:19]
                evt = obj.get("event_type", "")
                payload = json.dumps(obj.get("payload", {}), ensure_ascii=False)[:80]
                out.append(
                    f'<span style="color:#555">{escape(ts)}</span> '
                    f'<span style="color:#4fc3f7">{escape(str(evt))}</span> '
                    f'{escape(payload)}'
                )
            except Exception:
                out.append(escape(line))
        return "<br>".join(out) or "(empty)"
    except Exception as e:
        return f"(no log: {escape(str(e))})"

# ── HTML sections ──────────────────────────────────────────────────────────────

def _action_bar() -> str:
    return f"""
<div style='display:flex;gap:10px;flex-wrap:wrap;margin-bottom:28px;align-items:center'>
  <span style='color:#888;font-size:.85em'>Actions:</span>
  {_btn("▶ Run Loop Now", "/run-loop", "#66bb6a")}
  {_btn("⚡ Clear Stalled Sessions", "/clear-stalled", "#f0c040",
        "Expire all stalled sessions and requeue their tasks?")}
  {_btn("↻ Refresh", "/", "#555")}
</div>"""

def _pending_approvals_section() -> str:
    try:
        from harness.approval_workflow import list_pending_approvals
        pending = list_pending_approvals()
    except Exception as exc:
        return f"<p style='color:#ef5350'>Could not load approvals: {escape(str(exc))}</p>"
    if not pending:
        return "<p style='color:#66bb6a;margin:0'>✓ Nothing awaiting approval</p>"
    rows = []
    for item in pending:
        tid = str(item["task_id"])
        desc = escape(str(item.get("description", ""))[:120])
        status = item.get("status", "")
        already = item.get("approval_logged", False)
        btn = ("<span style='color:#66bb6a'>✓ logged</span>" if already
               else _btn("Approve", f"/approve/{quote(tid, safe='')}"))
        rows.append(
            f"<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #1e1e1e;font-size:.8em;color:#aaa'>{escape(tid)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #1e1e1e'>{desc}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #1e1e1e'>{_badge(status)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #1e1e1e'>{btn}</td>"
            f"</tr>"
        )
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr style='color:#f0c040'>"
        "<th align='left' style='padding:8px 10px'>Task ID</th>"
        "<th align='left' style='padding:8px 10px'>Description</th>"
        "<th align='left' style='padding:8px 10px'>Status</th>"
        "<th align='left' style='padding:8px 10px'>Action</th></tr>"
        + "".join(rows) + "</table>"
    )

def _work_queue_table() -> str:
    tasks = _load("WORK_QUEUE.json", [])
    if not tasks:
        return "<p style='color:#888'>No tasks in WORK_QUEUE.json</p>"
    rows = []
    for idx, t in enumerate(tasks):
        status = t.get("status", "")
        task_text = escape(str(t.get("task", ""))[:80])
        notes = t.get("notes", "") or t.get("result", "")
        detail = f"<div style='color:#555;font-size:.78em;margin-top:2px'>{escape(str(notes)[:120])}</div>" if notes else ""
        actions = ""
        if status in ("blocked", "stalled", "failed", "needs_review"):
            actions = _btn("Requeue", f"/requeue/{idx}", "#f0c040")
        elif status == "in_progress":
            actions = _btn("Reset", f"/requeue/{idx}", "#ef5350",
                           "Reset this in-progress task back to queued?")
        rows.append(
            f"<tr>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa;font-size:.82em;white-space:nowrap'>{escape(str(t.get('session_name','')))}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{task_text}{detail}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;white-space:nowrap'>{_badge(status)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;text-align:center'>{escape(str(t.get('priority','')))}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.78em;white-space:nowrap'>{escape(str(t.get('created_at',''))[:10])}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{actions}</td>"
            f"</tr>"
        )
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr style='color:#4fc3f7'>"
        "<th align='left' style='padding:7px 10px'>Session</th>"
        "<th align='left' style='padding:7px 10px'>Task</th>"
        "<th align='left' style='padding:7px 10px'>Status</th>"
        "<th align='left' style='padding:7px 10px'>Pri</th>"
        "<th align='left' style='padding:7px 10px'>Created</th>"
        "<th align='left' style='padding:7px 10px'>Action</th></tr>"
        + "".join(rows) + "</table>"
    )

def _sessions_table() -> str:
    raw = _load("ACTIVE_SESSIONS.json", {})
    items = _sessions_list(raw)
    if not items:
        return "<p style='color:#888'>No active sessions</p>"
    rows = []
    for s in items:
        sid = str(s.get("session_id", s.get("_key", "?")))
        status = s.get("status", "active")
        expire_btn = (_btn("Expire", f"/expire-session/{quote(sid, safe='')}", "#ef5350",
                           f"Expire session {sid[:16]}?")
                      if status != "done" else "")
        rows.append(
            f"<tr>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa;font-size:.82em'>{escape(sid[:24])}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{escape(str(s.get('task_id', s.get('title',''))))}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{_badge(status)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.8em'>{escape(str(s.get('started_at',''))[:16])}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{expire_btn}</td>"
            f"</tr>"
        )
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr style='color:#4fc3f7'>"
        "<th align='left' style='padding:7px 10px'>Session ID</th>"
        "<th align='left' style='padding:7px 10px'>Task</th>"
        "<th align='left' style='padding:7px 10px'>Status</th>"
        "<th align='left' style='padding:7px 10px'>Started</th>"
        "<th align='left' style='padding:7px 10px'>Action</th></tr>"
        + "".join(rows) + "</table>"
    )

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/session-bootstrap", response_class=HTMLResponse)
def session_bootstrap():
    """Exchange a URL-fragment token without placing it in an HTTP request URL."""
    return HTMLResponse("""<!doctype html><meta charset="utf-8">
<title>Jarvis Ops</title>
<body style="background:#0d1117;color:#e6edf3;font-family:monospace;padding:24px">
Opening Jarvis Ops...
<script>
const token = decodeURIComponent(location.hash.slice(1));
history.replaceState(null, "", "/session-bootstrap");
fetch("/session-bootstrap", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({token})
}).then(response => {
  if (!response.ok) throw new Error("authorization failed");
  location.replace("/");
}).catch(() => { document.body.textContent = "Dashboard authorization failed."; });
</script></body>""")


@app.post("/session-bootstrap")
async def establish_dashboard_session(request: Request):
    try:
        supplied = str((await request.json()).get("token") or "").strip()
    except (AttributeError, TypeError, ValueError):
        supplied = ""
    if not supplied or not hmac.compare_digest(supplied, _DASHBOARD_TOKEN):
        return JSONResponse({"error": "dashboard_auth_required"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        _DASHBOARD_COOKIE,
        _DASHBOARD_TOKEN,
        httponly=True,
        samesite="strict",
        secure=False,
    )
    return response

@app.get("/", response_class=HTMLResponse)
def index():
    tasks = _load("WORK_QUEUE.json", [])
    sessions = _sessions_list(_load("ACTIVE_SESSIONS.json", {}))
    queue = _load("LAUNCH_QUEUE.json", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total   = len(tasks)
    in_prog = sum(1 for t in tasks if t.get("status") == "in_progress")
    done    = sum(1 for t in tasks if t.get("status") == "done")
    blocked = sum(1 for t in tasks if t.get("status") == "blocked")
    needs_review = sum(1 for t in tasks if t.get("status") == "needs_review")
    queued  = sum(1 for t in tasks if t.get("status") == "queued")
    waiting = sum(1 for t in tasks if t.get("status") == "awaiting_approval")
    active  = sum(1 for s in sessions if s.get("status") == "active")
    stalled = sum(1 for s in sessions if s.get("status") == "stalled")
    fired   = sum(1 for q in queue if q.get("status") == "fired")
    try:
        audit_errors_24h = get_audit_error_count()
    except Exception:
        audit_errors_24h = 0

    def card(label, val, color="#4fc3f7"):
        return (f"<div style='background:#1e1e1e;border:1px solid #333;border-radius:8px;"
                f"padding:14px 20px;text-align:center;min-width:90px'>"
                f"<div style='font-size:1.8em;font-weight:bold;color:{color}'>{val}</div>"
                f"<div style='color:#888;font-size:.78em;margin-top:4px'>{label}</div></div>")

    H = "<h2 style='color:{c};border-bottom:1px solid #333;padding-bottom:6px;margin-top:28px'>{t}</h2>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset=UTF-8><meta http-equiv=refresh content=30>
<title>Jarvis Ops</title></head>
<body style='background:#0d1117;color:#e6edf3;font-family:monospace;padding:24px;margin:0;max-width:1400px'>
<div style='display:flex;align-items:baseline;gap:16px;margin-bottom:4px'>
  <h1 style='color:#4fc3f7;margin:0'>Jarvis Ops</h1>
  <span style='color:#555;font-size:.85em'>{now} · auto-refresh 30s</span>
</div>

<div style='display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 8px'>
  {card("Total", total)}
  {card("Queued", queued, "#f0c040" if queued else "#888")}
  {card("In Progress", in_prog, "#f0c040" if in_prog else "#888")}
  {card("Done", done, "#66bb6a")}
  {card("Blocked", blocked, "#ef5350" if blocked else "#888")}
  {card("Needs Review", needs_review, "#ffa726" if needs_review else "#888")}
  {card("Approval", waiting, "#f0a000" if waiting else "#888")}
  {card("Active", active, "#4fc3f7" if active else "#888")}
  {card("Stalled", stalled, "#ef5350" if stalled else "#888")}
  {card("Fired", fired, "#ab47bc" if fired else "#888")}
  {card("Audit Errors (24h)", audit_errors_24h, "#ef5350" if audit_errors_24h else "#888")}
</div>

{_action_bar()}

{H.format(c="#f0c040", t="⏳ Pending Approvals")}
{_pending_approvals_section()}

{H.format(c="#4fc3f7", t="Work Queue")}
{_work_queue_table()}

{H.format(c="#4fc3f7", t="Sessions")}
{_sessions_table()}

{H.format(c="#4fc3f7", t="Recent Log")}
<pre style='background:#161b22;padding:12px;border-radius:6px;font-size:.76em;
overflow-x:auto;white-space:pre-wrap;line-height:1.6;margin:0'>{_log_tail()}</pre>

<p style='color:#333;font-size:.72em;margin-top:20px'>
  Jarvis AI · port 7842 ·
  <a href='/api/status' style='color:#555'>status JSON</a> ·
  <a href='/api/pending' style='color:#555'>pending JSON</a>
</p>
</body></html>""")


@app.post("/run-loop")
def run_loop_now():
    """Trigger one orchestrator loop iteration in a background thread."""
    if not _RUN_LOOP_LOCK.acquire(blocking=False):
        return JSONResponse({"error": "orchestrator_loop_already_running"}, status_code=409)

    def _go():
        try:
            from orchestrator_loop import run_loop
            run_loop(max_concurrent=3, dry_run=False)
        except Exception as exc:
            print(f"[dashboard] run-loop error: {exc}")
        finally:
            _RUN_LOOP_LOCK.release()
    threading.Thread(target=_go, daemon=True, name="DashRunLoop").start()
    return RedirectResponse(url="/", status_code=303)


@app.post("/clear-stalled")
def clear_stalled():
    """Expire all stalled sessions and requeue their tasks."""
    try:
        from harness.session_tracker import SessionTracker
        from orchestrator_loop import _expire_stalled_sessions
        tracker = SessionTracker()
        with queue_state_lock(BASE / "WORK_QUEUE.json"):
            _expire_stalled_sessions(
                tracker, timeout_minutes=0
            )  # 0 = expire all stalled now
    except Exception as exc:
        print(f"[dashboard] clear-stalled error: {exc}")
        return JSONResponse({"error": "clear_stalled_failed"}, status_code=500)
    return RedirectResponse(url="/", status_code=303)


@app.post("/requeue/{idx:int}")
def requeue_task(idx: int):
    """Reset a task at position idx back to queued status."""
    with queue_state_lock(BASE / "WORK_QUEUE.json"):
        tasks = _load("WORK_QUEUE.json", [])
        if 0 <= idx < len(tasks):
            tasks[idx]["status"] = "queued"
            tasks[idx].pop("blocked_reason", None)
            tasks[idx].pop("blocked_at", None)
            tasks[idx].pop("assigned_at", None)
            _save("WORK_QUEUE.json", tasks)
    return RedirectResponse(url="/", status_code=303)


@app.post("/approve/{task_id:path}")
def approve_task(task_id: str):
    record = None
    created = False
    try:
        from harness.approval_workflow import (
            consume_approval,
            record_approval,
            requeue_approved_task,
        )
        record, created = record_approval(task_id, approved_by="dashboard")
        if not requeue_approved_task(task_id):
            if created:
                consume_approval(
                    record["task_id"],
                    task_contract_sha256=record["task_contract_sha256"],
                    task_spec_sha256=record["task_spec_sha256"],
                )
            return JSONResponse(
                {"error": "approved_task_could_not_be_requeued"}, status_code=409
            )
    except Exception as exc:
        if created and record is not None:
            try:
                consume_approval(
                    record["task_id"],
                    task_contract_sha256=record["task_contract_sha256"],
                    task_spec_sha256=record["task_spec_sha256"],
                )
            except Exception:
                pass
        print(f"[dashboard] approval failed for {task_id}: {exc}")
        return JSONResponse({"error": "approval_failed"}, status_code=409)
    return RedirectResponse(url="/", status_code=303)


@app.post("/expire-session/{session_id:path}")
def expire_session(session_id: str):
    """Mark one session as stalled and requeue its task."""
    try:
        from harness.session_tracker import SessionTracker
        tracker = SessionTracker()
        with queue_state_lock(BASE / "WORK_QUEUE.json"):
            data = tracker._load()
            now = datetime.now(timezone.utc).isoformat()
            for s in data.get("sessions", []):
                if str(s.get("session_id", "")) == session_id:
                    s["status"] = "stalled"
                    s["last_updated"] = now
                    s["stall_reason"] = "manually expired via dashboard"
            tracker._save(data)
            # Requeue any task linked to this session.
            tasks = _load("WORK_QUEUE.json", [])
            changed = False
            for t in tasks:
                assigned_session = t.get("assigned_to") or t.get("session_id")
                if (
                    t.get("status") == "in_progress"
                    and str(assigned_session or "") == session_id
                ):
                    t["status"] = "queued"
                    changed = True
            if changed:
                _save("WORK_QUEUE.json", tasks)
    except Exception as exc:
        print(f"[dashboard] expire-session error: {exc}")
        return JSONResponse({"error": "expire_session_failed"}, status_code=500)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/status")
def api_status():
    return {
        "tasks": _load("WORK_QUEUE.json", []),
        "sessions": _sessions_list(_load("ACTIVE_SESSIONS.json", {})),
        "queue": _load("LAUNCH_QUEUE.json", []),
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/api/pending")
def api_pending():
    try:
        from harness.approval_workflow import list_pending_approvals
        return list_pending_approvals()
    except Exception as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    import webbrowser
    try:
        host = _validated_dashboard_host()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    dashboard_url = dashboard_bootstrap_url(host)
    threading.Thread(
        target=lambda: __import__("time").sleep(1.5) or webbrowser.open(dashboard_url),
        daemon=True,
    ).start()
    uvicorn.run(app, host=host, port=7842, log_level="info")
