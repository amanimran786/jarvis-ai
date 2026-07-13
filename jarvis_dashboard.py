#!/usr/bin/env python3
"""Jarvis AI Dashboard — interactive ops console, server-side rendered."""
import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

BASE = Path(__file__).parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

app = FastAPI()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _load(fname, default):
    try:
        return json.load(open(BASE / fname))
    except Exception:
        return default

def _save(fname, data):
    path = BASE / fname
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def _badge(s):
    colors = {
        "pending": "#f0c040", "queued": "#f0c040", "in_progress": "#f0c040",
        "running": "#4fc3f7", "active": "#4fc3f7",
        "done": "#66bb6a", "completed": "#66bb6a",
        "blocked": "#ef5350", "failed": "#ef5350", "stalled": "#ef5350",
        "fired": "#ab47bc", "awaiting_approval": "#f0a000",
    }
    c = colors.get(str(s).lower(), "#888")
    return f"<span style='background:{c};color:#111;padding:2px 8px;border-radius:4px;font-size:.82em;white-space:nowrap'>{s}</span>"

def _btn(label, action, color="#4fc3f7", confirm=None):
    onclick = f" onclick=\"return confirm('{confirm}')\"" if confirm else ""
    return (f"<form method='post' action='{action}' style='display:inline'>"
            f"<button type='submit'{onclick} style='background:{color};color:#0d1117;border:none;"
            f"padding:3px 11px;border-radius:4px;cursor:pointer;font-family:monospace;font-size:.8em'>"
            f"{label}</button></form>")

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
            out.append(f'<span style="color:{c}">{line}</span>')
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
                out.append(f'<span style="color:#555">{ts}</span> <span style="color:#4fc3f7">{evt}</span> {payload}')
            except Exception:
                out.append(line)
        return "<br>".join(out) or "(empty)"
    except Exception as e:
        return f"(no log: {e})"

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
        return f"<p style='color:#ef5350'>Could not load approvals: {exc}</p>"
    if not pending:
        return "<p style='color:#66bb6a;margin:0'>✓ Nothing awaiting approval</p>"
    rows = []
    for item in pending:
        tid = item["task_id"]
        desc = item.get("description", "")[:120]
        status = item.get("status", "")
        already = item.get("approval_logged", False)
        btn = ("<span style='color:#66bb6a'>✓ logged</span>" if already
               else _btn("Approve", f"/approve/{tid}"))
        rows.append(
            f"<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid #1e1e1e;font-size:.8em;color:#aaa'>{tid}</td>"
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
        task_text = t.get("task", "")
        notes = t.get("notes", "") or t.get("result", "")
        detail = f"<div style='color:#555;font-size:.78em;margin-top:2px'>{notes[:120]}</div>" if notes else ""
        actions = ""
        if status in ("blocked", "stalled", "failed"):
            actions = _btn("Requeue", f"/requeue/{idx}", "#f0c040")
        elif status == "in_progress":
            actions = _btn("Reset", f"/requeue/{idx}", "#ef5350",
                           "Reset this in-progress task back to queued?")
        rows.append(
            f"<tr>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa;font-size:.82em;white-space:nowrap'>{t.get('session_name','')}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{task_text[:80]}{detail}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;white-space:nowrap'>{_badge(status)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;text-align:center'>{t.get('priority','')}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.78em;white-space:nowrap'>{str(t.get('created_at',''))[:10]}</td>"
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
        expire_btn = (_btn("Expire", f"/expire-session/{sid}", "#ef5350",
                           f"Expire session {sid[:16]}?")
                      if status != "done" else "")
        rows.append(
            f"<tr>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa;font-size:.82em'>{sid[:24]}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{s.get('task_id', s.get('title',''))}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{_badge(status)}</td>"
            f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.8em'>{str(s.get('started_at',''))[:16]}</td>"
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
    queued  = sum(1 for t in tasks if t.get("status") == "queued")
    waiting = sum(1 for t in tasks if t.get("status") == "awaiting_approval")
    active  = sum(1 for s in sessions if s.get("status") == "active")
    stalled = sum(1 for s in sessions if s.get("status") == "stalled")
    fired   = sum(1 for q in queue if q.get("status") == "fired")

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
  {card("Approval", waiting, "#f0a000" if waiting else "#888")}
  {card("Active", active, "#4fc3f7" if active else "#888")}
  {card("Stalled", stalled, "#ef5350" if stalled else "#888")}
  {card("Fired", fired, "#ab47bc" if fired else "#888")}
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
    def _go():
        try:
            from orchestrator_loop import run_loop
            run_loop(max_concurrent=3, dry_run=False)
        except Exception as exc:
            print(f"[dashboard] run-loop error: {exc}")
    threading.Thread(target=_go, daemon=True, name="DashRunLoop").start()
    return RedirectResponse(url="/", status_code=303)


@app.post("/clear-stalled")
def clear_stalled():
    """Expire all stalled sessions and requeue their tasks."""
    try:
        from harness.session_tracker import SessionTracker
        from orchestrator_loop import _expire_stalled_sessions
        tracker = SessionTracker()
        _expire_stalled_sessions(tracker, timeout_minutes=0)  # 0 = expire all stalled now
    except Exception as exc:
        print(f"[dashboard] clear-stalled error: {exc}")
    return RedirectResponse(url="/", status_code=303)


@app.post("/requeue/{idx:int}")
def requeue_task(idx: int):
    """Reset a task at position idx back to queued status."""
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
    try:
        from harness.approval_workflow import record_approval, requeue_approved_task
        record_approval(task_id, approved_by="dashboard")
        requeue_approved_task(task_id)
    except Exception:
        pass
    return RedirectResponse(url="/", status_code=303)


@app.post("/expire-session/{session_id:path}")
def expire_session(session_id: str):
    """Mark one session as stalled and requeue its task."""
    try:
        from harness.session_tracker import SessionTracker
        tracker = SessionTracker()
        data = tracker._load()
        now = datetime.utcnow().isoformat() + "Z"
        for s in data.get("sessions", []):
            if str(s.get("session_id", "")) == session_id:
                s["status"] = "stalled"
                s["last_updated"] = now
                s["stall_reason"] = "manually expired via dashboard"
        tracker._save(data)
        # Requeue any task linked to this session
        tasks = _load("WORK_QUEUE.json", [])
        changed = False
        for t in tasks:
            if t.get("status") == "in_progress" and str(t.get("session_id", "")) == session_id:
                t["status"] = "queued"
                changed = True
        if changed:
            _save("WORK_QUEUE.json", tasks)
    except Exception as exc:
        print(f"[dashboard] expire-session error: {exc}")
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
    threading.Thread(
        target=lambda: __import__("time").sleep(1.5) or webbrowser.open("http://localhost:7842"),
        daemon=True,
    ).start()
    uvicorn.run(app, host="0.0.0.0", port=7842, log_level="info")
