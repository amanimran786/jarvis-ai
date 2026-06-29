#!/usr/bin/env python3
"""Jarvis AI Dashboard - server-side rendered, no JS fetch required."""
import json
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

BASE = Path(__file__).parent
app = FastAPI()

def _load(fname, default):
    try:
        return json.load(open(BASE / fname))
    except Exception:
        return default

def _badge(s):
    colors = {"pending":"#f0c040","queued":"#f0c040","running":"#4fc3f7",
              "active":"#4fc3f7","done":"#66bb6a","completed":"#66bb6a",
              "failed":"#ef5350","stalled":"#ef5350","fired":"#ab47bc"}
    c = colors.get(str(s).lower(), "#888")
    return f"<span style='background:{c};color:#111;padding:2px 8px;border-radius:4px;font-size:.85em'>{s}</span>"

def _sessions_list(raw):
    """Normalize ACTIVE_SESSIONS.json to a flat list of dicts regardless of format."""
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

def _work_queue_table():
    tasks = _load("WORK_QUEUE.json", [])
    if not tasks:
        return "<p style='color:#888'>No tasks in WORK_QUEUE.json</p>"
    rows = "".join(
        f"<tr><td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{t.get('id','')}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{t.get('title','')}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{_badge(t.get('status',''))}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa'>{t.get('assigned_ai','')}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.8em'>{str(t.get('created_at',''))[:16]}</td></tr>"
        for t in tasks
    )
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr style='color:#4fc3f7'>"
        "<th align='left' style='padding:7px 10px'>ID</th>"
        "<th align='left' style='padding:7px 10px'>Title</th>"
        "<th align='left' style='padding:7px 10px'>Status</th>"
        "<th align='left' style='padding:7px 10px'>AI</th>"
        "<th align='left' style='padding:7px 10px'>Created</th></tr>"
        + rows + "</table>"
    )

def _sessions_table():
    raw = _load("ACTIVE_SESSIONS.json", {})
    items = _sessions_list(raw)
    if not items:
        return "<p style='color:#888'>No active sessions</p>"
    rows = "".join(
        f"<tr><td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#aaa;font-size:.85em'>{str(s.get('session_id', s.get('_key','?')))[:20]}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{s.get('task_id', s.get('title',''))}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e'>{_badge(s.get('status','active'))}</td>"
        f"<td style='padding:7px 10px;border-bottom:1px solid #1e1e1e;color:#888;font-size:.8em'>{str(s.get('started_at',''))[:16]}</td></tr>"
        for s in items
    )
    return (
        "<table style='width:100%;border-collapse:collapse'>"
        "<tr style='color:#4fc3f7'>"
        "<th align='left' style='padding:7px 10px'>Session ID</th>"
        "<th align='left' style='padding:7px 10px'>Task</th>"
        "<th align='left' style='padding:7px 10px'>Status</th>"
        "<th align='left' style='padding:7px 10px'>Started</th></tr>"
        + rows + "</table>"
    )

def _log_tail():
    try:
        lines = (BASE / "logs" / "MASTER_LOG.jsonl").read_text().splitlines()[-20:]
        out = []
        for line in lines:
            try:
                obj = json.loads(line)
                ts = obj.get("timestamp", obj.get("ts",""))[:19]
                lvl = obj.get("level","INFO")
                msg = obj.get("message", obj.get("msg", line))
                c = "#ef5350" if lvl=="ERROR" else "#f0c040" if lvl=="WARNING" else "#ccc"
                out.append(f'<span style="color:#555">{ts}</span> <span style="color:{c}">[{lvl}]</span> {msg}')
            except Exception:
                out.append(line)
        return "<br>".join(out) or "(empty)"
    except Exception as e:
        return f"(log not found: {e})"

@app.get("/", response_class=HTMLResponse)
def index():
    tasks = _load("WORK_QUEUE.json", [])
    raw_sessions = _load("ACTIVE_SESSIONS.json", {})
    sessions = _sessions_list(raw_sessions)
    queue = _load("LAUNCH_QUEUE.json", [])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(tasks)
    pending = sum(1 for t in tasks if t.get("status") in ("pending","queued"))
    done = sum(1 for t in tasks if t.get("status") in ("done","completed"))
    failed = sum(1 for t in tasks if t.get("status") == "failed")
    active = sum(1 for s in sessions if s.get("status") == "active")
    fired = sum(1 for q in queue if q.get("status") == "fired")

    def card(label, val, color="#4fc3f7"):
        return (f"<div style='background:#1e1e1e;border:1px solid #333;border-radius:8px;"
                f"padding:14px 20px;text-align:center;min-width:100px'>"
                f"<div style='font-size:1.8em;font-weight:bold;color:{color}'>{val}</div>"
                f"<div style='color:#888;font-size:.8em;margin-top:4px'>{label}</div></div>")

    return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset=UTF-8><meta http-equiv=refresh content=30>
<title>Jarvis AI Dashboard</title></head>
<body style='background:#0d1117;color:#e6edf3;font-family:monospace;padding:24px;margin:0'>
<h1 style='color:#4fc3f7;margin-bottom:4px'>Jarvis AI Dashboard</h1>
<p style='color:#888;margin-bottom:20px'>{now} &nbsp;·&nbsp; auto-refresh 30s</p>
<div style='display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px'>
{card("Total Tasks", total)}
{card("Pending", pending, "#f0c040")}
{card("Done", done, "#66bb6a")}
{card("Failed", failed, "#ef5350")}
{card("Active Sessions", active, "#4fc3f7")}
{card("Fired", fired, "#ab47bc")}
</div>
<h2 style='color:#4fc3f7;border-bottom:1px solid #333;padding-bottom:6px;margin-top:0'>Work Queue</h2>
{_work_queue_table()}
<h2 style='color:#4fc3f7;border-bottom:1px solid #333;padding-bottom:6px;margin-top:24px'>Active Sessions</h2>
{_sessions_table()}
<h2 style='color:#4fc3f7;border-bottom:1px solid #333;padding-bottom:6px;margin-top:24px'>Recent Logs</h2>
<pre style='background:#161b22;padding:12px;border-radius:6px;font-size:.78em;overflow-x:auto;white-space:pre-wrap;line-height:1.5'>{_log_tail()}</pre>
<p style='color:#333;font-size:.75em;margin-top:24px'>Jarvis AI · Port 7842 · Server-side rendered</p>
</body></html>""")

@app.get("/api/status")
def api_status():
    return {
        "tasks": _load("WORK_QUEUE.json", []),
        "sessions": _sessions_list(_load("ACTIVE_SESSIONS.json", {})),
        "queue": _load("LAUNCH_QUEUE.json", []),
        "timestamp": datetime.now().isoformat(),
    }

if __name__ == "__main__":
    import threading, webbrowser
    threading.Thread(target=lambda: __import__("time").sleep(1.5) or webbrowser.open("http://localhost:7842"), daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=7842, log_level="info")
