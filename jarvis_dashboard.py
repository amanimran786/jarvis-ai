#!/usr/bin/env python3
"""
jarvis_dashboard.py — Jarvis AI unified master dashboard
Usage: python jarvis_dashboard.py
Opens http://localhost:7842

Tabs:
  Overview  — stats bar + orchestrator sessions + ollama models
  Work Queue — kanban (queued / in-progress / done)
  Training  — live training charts, benchmark history, model routing
  Ops       — embedded Agent Ops dashboard (iframe → localhost:8765)
  Activity  — MASTER_LOG.md feed + self-eval scores
"""

import json
import pathlib
import datetime
import subprocess
from collections import defaultdict

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Jarvis Master Dashboard")
REPO = pathlib.Path("/Users/truthseeker/jarvis-ai")

CATEGORIES = ["voice", "calendar", "code", "memory", "tools", "conversation", "meeting"]
CATEGORY_LABELS = {
    "voice": "Voice / STT", "calendar": "Calendar", "code": "Code",
    "memory": "Memory", "tools": "Tools",
    "conversation": "Conversation", "meeting": "Meeting",
}


# ─── data helpers ────────────────────────────────────────────────────────────

def _read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default if default is not None else {}


def _read_jsonl(path):
    lines = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return lines


def _infer_ai(task):
    combined = ((task.get("assigned_to") or "") + " " + (task.get("session_name") or "")).lower()
    if "gemini" in combined:
        return "Gemini"
    if "codex" in combined:
        return "Codex"
    return "Claude"


def _infer_domain(task):
    session = (task.get("session_name") or "").lower()
    if "audit" in session:    return "audit"
    if "eval" in session or "self" in session: return "eval"
    if "local" in session:    return "local-llm"
    if "codex" in session:    return "code"
    if "gemini" in session:   return "general"
    return "general"


def _time_ago(iso_str):
    if not iso_str:
        return ""
    try:
        ts = iso_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = now - dt
        s = int(diff.total_seconds())
        if s < 60:    return f"{s}s ago"
        if s < 3600:  return f"{s // 60}m ago"
        if s < 86400: return f"{s // 3600}h ago"
        return f"{s // 86400}d ago"
    except Exception:
        return ""


def _get_tasks():
    raw = _read_json(REPO / "WORK_QUEUE.json", default=[])
    tasks = []
    for t in (raw if isinstance(raw, list) else []):
        status = t.get("status", "queued")
        task_text = t.get("task") or ""
        snippet = (task_text[:120] + "…") if len(task_text) > 120 else task_text
        time_ref = t.get("completed_at") or t.get("assigned_at") or t.get("created_at")
        tasks.append({
            "id": t.get("session_name", "") + "-" + str(t.get("priority", 0)),
            "title": task_text,
            "snippet": snippet,
            "notes": (t.get("notes") or "")[:200],
            "status": status,
            "priority": t.get("priority", 99),
            "assigned_ai": _infer_ai(t),
            "domain": _infer_domain(t),
            "time_ago": _time_ago(time_ref),
            "completed_at": t.get("completed_at"),
            "assigned_at": t.get("assigned_at"),
            "session_name": t.get("session_name", ""),
            "commit": t.get("commit"),
        })
    return tasks


def _get_active_sessions():
    data = _read_json(REPO / "ACTIVE_SESSIONS.json", default={})
    sessions = data.get("sessions", []) if isinstance(data, dict) else []
    return [{
        "session_id": s.get("session_id", ""),
        "task_id": s.get("task_id", ""),
        "status": s.get("status", "active"),
        "time_ago": _time_ago(s.get("claimed_at") or s.get("last_updated")),
    } for s in sessions]


def _get_log_lines():
    try:
        text = (REPO / "MASTER_LOG.md").read_text()
        lines = [l for l in text.splitlines() if l.strip()]
        return lines[-30:]
    except Exception:
        return []


def _get_orchestrator():
    data = _read_json(REPO / "ORCHESTRATOR_STATUS.json", default={})
    return [{
        "name": s.get("name", ""),
        "status": s.get("status", "unknown"),
        "owner": s.get("owner", ""),
        "last_active": _time_ago(s.get("last_active")),
        "current_task": (s.get("current_task") or "")[:100],
        "note": (s.get("orchestrator_note") or "")[:120],
    } for s in data.get("sessions", [])]


def _get_models():
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        models = []
        for line in result.stdout.strip().splitlines()[1:]:
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            size = parts[2] if len(parts) > 2 else "?"
            mod = " ".join(parts[3:5]) if len(parts) > 4 else (parts[3] if len(parts) > 3 else "")
            models.append({"name": name, "size": size, "modified": mod, "status": "ready"})
        return models
    except Exception:
        return []


def _get_stats():
    tasks = _get_tasks()
    done_count   = sum(1 for t in tasks if t["status"] == "done")
    queued_count = sum(1 for t in tasks if t["status"] == "queued")
    inprog_count = sum(1 for t in tasks if t["status"] == "in_progress")

    evals = _read_jsonl(REPO / "logs" / "self_eval.jsonl")
    avg_quality = 0.0
    if evals:
        scores = [e.get("response_quality", 0) for e in evals if "response_quality" in e]
        avg_quality = round(sum(scores) / len(scores), 3) if scores else 0.0

    budget_lines = _read_jsonl(REPO / "logs" / "budget.jsonl")
    today = datetime.date.today().isoformat()
    today_tokens_in = today_tokens_out = 0
    for b in budget_lines:
        if (b.get("ts") or "")[:10] == today:
            today_tokens_in  += b.get("tokens_in", 0)
            today_tokens_out += b.get("tokens_out", 0)

    return {
        "total_done": done_count,
        "queued": queued_count,
        "in_progress": inprog_count,
        "avg_quality": avg_quality,
        "tokens_in_today": today_tokens_in,
        "tokens_out_today": today_tokens_out,
        "total_tasks": len(tasks),
    }


# ─── training data helpers ────────────────────────────────────────────────────

def _run_eval_data(run):
    stages = run.get("stages") or {}
    nested = stages.get("eval") or {}
    passed = run.get("eval_passed") or nested.get("passed")
    total  = run.get("eval_total")  or nested.get("total")
    try:
        p, t = int(passed), int(total)
        return {"passed": p, "total": t, "score": p / t if t else None}
    except (TypeError, ValueError):
        return {"passed": None, "total": None, "score": None}


def _run_label(run, idx):
    ts = str(run.get("timestamp") or "")
    if "T" in ts:
        return ts.replace("T", " ").replace("Z", "")[5:16]
    return str(run.get("date") or f"Run {idx+1}")


def _run_duration(run):
    for key in ("duration_seconds", "duration_sec"):
        try:
            v = float(run.get(key) or 0)
            if v: return v
        except (TypeError, ValueError):
            pass
    stages = (run.get("stages") or {})
    tr = stages.get("training") or {}
    for key in ("duration_seconds", "duration_sec"):
        try:
            v = float(tr.get(key) or 0)
            if v: return v
        except (TypeError, ValueError):
            pass
    return 0.0


def _run_trained_ok(run):
    stages = run.get("stages") or {}
    tr = stages.get("training") or {}
    return bool(tr.get("ok"))


def _get_training_data():
    overnight_runs = _read_jsonl(REPO / "training" / "overnight_log.jsonl")
    benchmarks     = _read_jsonl(REPO / "training" / "benchmarks.jsonl")
    state          = _read_json(REPO / "training" / "overnight_state.json", default={})

    # Run history (most recent first, capped at 25 for table)
    run_history = []
    for i, run in enumerate(overnight_runs):
        ev = _run_eval_data(run)
        dur = _run_duration(run)
        dur_str = (f"{int(dur//60)}m {int(dur%60)}s" if dur else "—")
        stages = run.get("stages") or {}
        tr_stage = stages.get("training") or {}
        ex = run.get("examples_count") or tr_stage.get("examples_count") or 0
        run_history.append({
            "label": _run_label(run, i),
            "score": round(ev["score"] * 100, 1) if ev["score"] is not None else None,
            "passed": ev["passed"],
            "total": ev["total"],
            "examples": ex,
            "duration": dur_str,
            "promoted": bool(run.get("promoted") or _run_trained_ok(run)),
        })
    run_history.reverse()

    # Score chart data (chronological)
    chart_labels = [r["label"] for r in reversed(run_history)]
    chart_scores = [r["score"] for r in reversed(run_history)]

    # Latest benchmark for KPI
    latest_bench = None
    if benchmarks:
        latest_bench = benchmarks[-1]
    elif overnight_runs:
        last = overnight_runs[-1]
        ev = _run_eval_data(last)
        if ev["score"] is not None:
            latest_bench = {
                "overall": ev["score"],
                "total_passed": ev["passed"],
                "total_tests": ev["total"],
                "categories": {},
            }

    # Category scores from latest state eval
    last_session = state.get("last_session") or {}
    last_stages  = last_session.get("stages") or {}
    last_eval    = last_stages.get("eval") or {}
    cat_scores_raw = last_eval.get("categories") or {}

    cat_labels, cat_scores, cat_colors = [], [], []
    for k in CATEGORIES:
        label = CATEGORY_LABELS.get(k, k)
        data  = cat_scores_raw.get(k) or {}
        p = data.get("passed")
        t = data.get("total")
        score = (p / t * 100) if (p is not None and t and t > 0) else None
        cat_labels.append(label)
        cat_scores.append(round(score, 1) if score is not None else 0)
        if score is None:
            cat_colors.append("#4A8FA8")
        elif score >= 90:
            cat_colors.append("#00FF88")
        elif score >= 75:
            cat_colors.append("#00D4FF")
        elif score >= 60:
            cat_colors.append("#FFAA00")
        else:
            cat_colors.append("#FF4444")

    # KPI numbers
    total_runs    = len(overnight_runs)
    trained_ok    = sum(1 for r in overnight_runs if _run_trained_ok(r))
    overall_score = None
    if latest_bench:
        ov = latest_bench.get("overall")
        if ov is not None:
            overall_score = round(float(ov) * 100, 1) if float(ov) <= 1 else round(float(ov), 1)

    # Last training timestamp from state
    last_ts = last_session.get("timestamp") or ""
    if "T" in last_ts:
        last_ts_fmt = last_ts.replace("T", " ").replace("Z", "")[:16]
    else:
        last_ts_fmt = last_ts

    # Model routing stats
    routing_log = _read_jsonl(REPO / "logs" / "self_eval.jsonl")
    route_tally: dict = defaultdict(int)
    for entry in routing_log:
        route = entry.get("route") or "unknown"
        route_tally[route] += 1
    total_routed = sum(route_tally.values())

    return {
        "kpi": {
            "total_runs": total_runs,
            "trained_ok": trained_ok,
            "overall_score": overall_score,
            "last_ts": last_ts_fmt,
            "latest_passed": latest_bench.get("total_passed") if latest_bench else None,
            "latest_total":  latest_bench.get("total_tests")  if latest_bench else None,
        },
        "chart_labels": chart_labels[-41:],
        "chart_scores": chart_scores[-41:],
        "cat_labels": cat_labels,
        "cat_scores": cat_scores,
        "cat_colors": cat_colors,
        "run_history": run_history[:25],
        "cat_detail": [
            {
                "label": CATEGORY_LABELS.get(k, k),
                "score": cat_scores[i],
                "color": cat_colors[i],
                "passed": (cat_scores_raw.get(k) or {}).get("passed"),
                "total":  (cat_scores_raw.get(k) or {}).get("total"),
            }
            for i, k in enumerate(CATEGORIES)
        ],
        "routing": {
            "total": total_routed,
            "tiers": dict(sorted(route_tally.items(), key=lambda x: -x[1])),
        },
    }


def _get_self_eval():
    evals = _read_jsonl(REPO / "logs" / "self_eval.jsonl")
    recent = evals[-50:]
    return [{
        "ts": e.get("ts", ""),
        "query": (e.get("query") or "")[:80],
        "route": e.get("route") or "—",
        "quality": e.get("response_quality"),
        "relevance": e.get("response_relevance"),
        "flags": e.get("flags") or [],
    } for e in reversed(recent)]


# ─── API ─────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status():
    tasks = _get_tasks()
    queued = sorted([t for t in tasks if t["status"] == "queued"], key=lambda x: x["priority"])
    in_progress = [t for t in tasks if t["status"] == "in_progress"]
    done = sorted(
        [t for t in tasks if t["status"] == "done"],
        key=lambda x: x.get("completed_at") or "",
        reverse=True
    )[:20]
    return JSONResponse({
        "queued": queued,
        "in_progress": in_progress,
        "done": done,
        "active_sessions": _get_active_sessions(),
        "log_lines": _get_log_lines(),
        "orchestrator": _get_orchestrator(),
        "models": _get_models(),
        "stats": _get_stats(),
        "refreshed_at": datetime.datetime.utcnow().isoformat() + "Z",
    })


@app.get("/api/training")
def training():
    return JSONResponse(_get_training_data())


@app.get("/api/activity")
def activity():
    return JSONResponse({
        "log_lines": _get_log_lines(),
        "self_eval": _get_self_eval(),
        "refreshed_at": datetime.datetime.utcnow().isoformat() + "Z",
    })


# ─── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S. — Master Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0d1117;
    --surface:   #161b22;
    --border:    #21262d;
    --border-hi: #30363d;
    --text:      #e6edf3;
    --muted:     #8b949e;
    --blue:      #58a6ff;
    --green:     #3fb950;
    --yellow:    #d29922;
    --red:       #ff7b72;
    --purple:    #bc8cff;
    --orange:    #f0883e;
    --cyan:      #00CFFF;
    --radius:    8px;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky; top: 0; z-index: 200;
  }
  header h1 { font-size: 15px; font-weight: 600; color: var(--text); letter-spacing: .3px; }
  .logo { font-size: 18px; }
  .refresh-badge {
    margin-left: auto; font-size: 11px; color: var(--muted);
    display: flex; align-items: center; gap: 6px;
  }
  .pulse-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }

  /* ── Tab nav ── */
  .tab-nav {
    display: flex; gap: 0;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 16px;
    position: sticky; top: 49px; z-index: 150;
    overflow-x: auto;
  }
  .tab-nav::-webkit-scrollbar { height: 3px; }
  .tab-nav::-webkit-scrollbar-thumb { background: var(--border-hi); }
  .tab-btn {
    padding: 10px 18px;
    background: none; border: none; border-bottom: 2px solid transparent;
    color: var(--muted); font-size: 13px; cursor: pointer;
    white-space: nowrap; transition: color .15s, border-color .15s;
    font-family: inherit;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); font-weight: 600; }

  /* ── Panes ── */
  .pane { display: none; }
  .pane.active { display: block; }

  /* ── Stats bar ── */
  .stats-bar {
    display: flex; gap: 0;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .stat {
    flex: 1; padding: 10px 20px;
    border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 2px;
  }
  .stat:last-child { border-right: none; }
  .stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: .8px; color: var(--muted); font-weight: 500; }
  .stat-value { font-size: 22px; font-weight: 700; color: var(--text); font-variant-numeric: tabular-nums; }
  .stat-sub   { font-size: 11px; color: var(--muted); }

  /* ── Main container ── */
  .main { padding: 20px 24px; display: flex; flex-direction: column; gap: 20px; }

  /* ── Panels ── */
  .panel {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); overflow: hidden; display: flex; flex-direction: column;
  }
  .panel-header {
    padding: 9px 14px; font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .5px; color: var(--muted);
    border-bottom: 1px solid var(--border); background: var(--bg); flex-shrink: 0;
  }

  /* ── Kanban ── */
  .kanban { display: grid; grid-template-columns: repeat(3,1fr); gap: 16px; align-items: start; }
  .column { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .col-header {
    padding: 10px 14px; font-size: 11px; font-weight: 600; letter-spacing: .3px;
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid var(--border); text-transform: uppercase;
  }
  .col-count { background: var(--border-hi); color: var(--muted); border-radius: 12px; padding: 1px 8px; font-size: 11px; font-weight: 600; }
  .col-queued .col-header   { color: var(--yellow); }
  .col-progress .col-header { color: var(--blue); }
  .col-done .col-header     { color: var(--green); }
  .cards { padding: 10px; display: flex; flex-direction: column; gap: 8px; max-height: 65vh; overflow-y: auto; }
  .cards::-webkit-scrollbar { width: 4px; }
  .cards::-webkit-scrollbar-thumb { background: var(--border-hi); border-radius: 2px; }

  .card {
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px; transition: border-color .15s, box-shadow .15s;
  }
  .card:hover { border-color: var(--blue); box-shadow: 0 0 0 1px rgba(88,166,255,.2), 0 2px 8px rgba(0,0,0,.4); }
  .card-done   { opacity: .65; }
  .card-active { border-left: 3px solid var(--blue); animation: active-glow 2s ease-in-out infinite; }
  @keyframes active-glow { 0%,100% { border-left-color:var(--blue); } 50% { border-left-color:rgba(88,166,255,.4); } }
  .card-title { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 4px; line-height: 1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .card-snippet { font-size: 11px; color: var(--muted); margin-bottom: 8px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .card-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .tag { font-size: 10px; font-weight: 600; padding: 1px 7px; border-radius: 10px; letter-spacing: .2px; }
  .tag-claude  { background:rgba(240,136,62,.15);  color:var(--orange); border:1px solid rgba(240,136,62,.3); }
  .tag-codex   { background:rgba(88,166,255,.12);  color:var(--blue);   border:1px solid rgba(88,166,255,.25); }
  .tag-gemini  { background:rgba(188,140,255,.12); color:var(--purple); border:1px solid rgba(188,140,255,.25); }
  .tag-domain  { background:rgba(255,255,255,.05); color:var(--muted);  border:1px solid var(--border); font-weight:400; }
  .time-ago    { margin-left:auto; font-size:10px; color:var(--muted); }
  .commit-badge { font-size:10px; font-family:ui-monospace,monospace; color:var(--green); background:rgba(63,185,80,.1); border:1px solid rgba(63,185,80,.2); border-radius:4px; padding:1px 5px; }
  .empty-state { padding:20px; text-align:center; color:var(--muted); font-size:12px; }

  /* ── Bottom grid ── */
  .bottom-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 16px; }

  /* ── Activity feed ── */
  .activity-feed {
    padding: 10px 14px; font-family:ui-monospace,"SF Mono",Menlo,monospace;
    font-size: 11px; line-height: 1.7; color: var(--muted);
    overflow-y: auto; max-height: 340px; flex: 1;
  }
  .activity-feed::-webkit-scrollbar { width:4px; }
  .activity-feed::-webkit-scrollbar-thumb { background:var(--border-hi); border-radius:2px; }
  .log-line { padding:1px 0; border-bottom:1px solid rgba(255,255,255,.03); }
  .log-line:last-child { border-bottom:none; }
  .log-ts    { color:var(--blue); }
  .log-actor { color:var(--orange); font-weight:600; }
  .log-body  { color:var(--text); }

  /* ── Model & orch lists ── */
  .model-list, .orch-list { padding:8px 0; overflow-y:auto; max-height:260px; flex:1; }
  .model-row { display:flex; align-items:center; gap:10px; padding:7px 14px; border-bottom:1px solid rgba(255,255,255,.04); font-size:12px; }
  .model-row:last-child { border-bottom:none; }
  .model-name { font-family:ui-monospace,monospace; font-size:11px; color:var(--text); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .model-size { color:var(--muted); font-size:11px; min-width:50px; text-align:right; }
  .model-status { font-size:10px; font-weight:600; padding:2px 8px; border-radius:10px; }
  .status-ready { background:rgba(63,185,80,.12); color:var(--green); border:1px solid rgba(63,185,80,.25); }
  .orch-row { display:flex; align-items:flex-start; gap:10px; padding:8px 14px; border-bottom:1px solid rgba(255,255,255,.04); }
  .orch-row:last-child { border-bottom:none; }
  .orch-dot { width:8px; height:8px; border-radius:50%; margin-top:4px; flex-shrink:0; }
  .orch-active { background:var(--green); animation:pulse 2s infinite; }
  .orch-idle   { background:var(--border-hi); }
  .orch-info   { flex:1; min-width:0; }
  .orch-name   { font-size:12px; font-weight:600; color:var(--text); }
  .orch-owner  { font-size:10px; color:var(--muted); }
  .orch-task   { font-size:11px; color:var(--muted); margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .orch-time   { font-size:10px; color:var(--muted); flex-shrink:0; }

  /* ── Training tab ── */
  .train-main { padding:20px 24px; display:flex; flex-direction:column; gap:20px; }
  .train-kpi-row { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }
  .train-kpi {
    background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
    padding:16px 18px; position:relative;
  }
  .train-kpi::before { content:''; position:absolute; top:0; left:0; width:8px; height:8px; border-top:2px solid var(--cyan); border-left:2px solid var(--cyan); }
  .train-kpi::after  { content:''; position:absolute; bottom:0; right:0; width:8px; height:8px; border-bottom:2px solid var(--cyan); border-right:2px solid var(--cyan); }
  .train-kpi-label { font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); margin-bottom:6px; }
  .train-kpi-value { font-size:28px; font-weight:700; color:var(--cyan); line-height:1; text-shadow:0 0 12px rgba(0,207,255,.4); }
  .train-kpi-sub   { font-size:11px; color:var(--muted); margin-top:4px; }

  .train-grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .train-grid3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; }
  .train-card  { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:16px; overflow:hidden; }
  .train-card-title { font-size:9px; letter-spacing:2px; text-transform:uppercase; color:var(--muted); padding-bottom:10px; margin-bottom:12px; border-bottom:1px solid rgba(0,207,255,.1); display:flex; align-items:center; gap:6px; }
  .train-card-title::before { content:'//'; color:rgba(0,207,255,.4); }
  .chart-wrap { position:relative; height:200px; }

  /* Training table */
  .train-table { width:100%; border-collapse:collapse; font-size:11px; }
  .train-table th { color:var(--muted); font-size:9px; letter-spacing:2px; text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); font-weight:500; }
  .train-table td { padding:6px 10px; border-bottom:1px solid rgba(33,38,45,.7); color:var(--text); }
  .train-table tr:last-child td { border-bottom:none; }
  .train-table tr:hover td { background:rgba(88,166,255,.03); }
  .bar-track { background:rgba(33,38,45,.9); border-radius:2px; height:4px; overflow:hidden; margin-top:3px; }
  .bar-fill  { height:100%; border-radius:2px; }

  /* ── Ops iframe tab ── */
  .ops-frame-wrap { height:calc(100vh - 110px); display:flex; flex-direction:column; }
  #ops-iframe { flex:1; border:none; width:100%; }
  .ops-banner {
    background:var(--surface); border-bottom:1px solid var(--border);
    padding:8px 24px; font-size:11px; color:var(--muted);
    display:flex; align-items:center; gap:10px;
  }
  .ops-banner strong { color:var(--text); }
  .ops-error {
    display:none; flex:1; align-items:center; justify-content:center; flex-direction:column;
    gap:12px; padding:40px; text-align:center; color:var(--muted);
  }
  .ops-error.visible { display:flex; }
  .ops-error h3 { color:var(--text); font-size:14px; }

  /* ── Activity/eval tab ── */
  .activity-main { padding:20px 24px; display:flex; flex-direction:column; gap:20px; }
  .eval-table { width:100%; border-collapse:collapse; font-size:11px; }
  .eval-table th { color:var(--muted); font-size:9px; letter-spacing:2px; text-align:left; padding:7px 10px; border-bottom:1px solid var(--border); font-weight:500; }
  .eval-table td { padding:7px 10px; border-bottom:1px solid rgba(33,38,45,.7); color:var(--text); max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .eval-table tr:last-child td { border-bottom:none; }
  .eval-table tr:hover td { background:rgba(88,166,255,.03); }
  .score-pill { display:inline-block; padding:1px 7px; border-radius:10px; font-size:10px; font-weight:600; }
  .score-high { background:rgba(63,185,80,.15);  color:var(--green); border:1px solid rgba(63,185,80,.3); }
  .score-mid  { background:rgba(210,153,34,.12); color:var(--yellow); border:1px solid rgba(210,153,34,.3); }
  .score-low  { background:rgba(255,123,114,.1); color:var(--red);    border:1px solid rgba(255,123,114,.3); }
  .flag-pill  { display:inline-block; padding:1px 6px; border-radius:8px; font-size:9px; background:rgba(255,123,114,.08); color:var(--red); border:1px solid rgba(255,123,114,.2); margin:1px; }

  /* Responsive */
  @media (max-width:900px) {
    .kanban { grid-template-columns:1fr; }
    .bottom-grid, .train-grid2, .train-grid3, .train-kpi-row { grid-template-columns:1fr; }
    .stats-bar { flex-wrap:wrap; }
  }
</style>
</head>
<body>

<header>
  <span class="logo">🤖</span>
  <h1>J.A.R.V.I.S. — Master Dashboard</h1>
  <div class="refresh-badge">
    <div class="pulse-dot"></div>
    <span id="refresh-ts">loading…</span>
  </div>
</header>

<!-- Tab nav -->
<nav class="tab-nav">
  <button class="tab-btn active" data-tab="overview"  onclick="switchTab('overview')">⬛ Overview</button>
  <button class="tab-btn"        data-tab="workqueue" onclick="switchTab('workqueue')">📋 Work Queue</button>
  <button class="tab-btn"        data-tab="training"  onclick="switchTab('training')">🧠 Training</button>
  <button class="tab-btn"        data-tab="ops"       onclick="switchTab('ops')">⚙️ Ops</button>
  <button class="tab-btn"        data-tab="activity"  onclick="switchTab('activity')">📊 Activity</button>
</nav>

<!-- ═══════════════ OVERVIEW ═══════════════ -->
<div id="pane-overview" class="pane active">
  <div class="stats-bar" id="stats-bar">
    <div class="stat"><div class="stat-label">Done</div><div class="stat-value" id="s-done">—</div><div class="stat-sub">total tasks</div></div>
    <div class="stat"><div class="stat-label">In Progress</div><div class="stat-value" id="s-inprog">—</div><div class="stat-sub">active now</div></div>
    <div class="stat"><div class="stat-label">Queued</div><div class="stat-value" id="s-queued">—</div><div class="stat-sub">waiting</div></div>
    <div class="stat"><div class="stat-label">Avg Quality</div><div class="stat-value" id="s-qual">—</div><div class="stat-sub">self-eval score</div></div>
    <div class="stat"><div class="stat-label">Tokens In Today</div><div class="stat-value" id="s-tokens">—</div><div class="stat-sub">budget usage</div></div>
  </div>
  <div class="main">
    <div class="bottom-grid">
      <div class="panel">
        <div class="panel-header">📋 MASTER_LOG.md — Recent Activity</div>
        <div class="activity-feed" id="ov-log-feed"></div>
      </div>
      <div style="display:flex;flex-direction:column;gap:16px;">
        <div class="panel">
          <div class="panel-header">🧠 Local Models (ollama)</div>
          <div class="model-list" id="ov-model-list"></div>
        </div>
        <div class="panel">
          <div class="panel-header">⚙️ Orchestrator Sessions</div>
          <div class="orch-list" id="ov-orch-list"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ WORK QUEUE ═══════════════ -->
<div id="pane-workqueue" class="pane">
  <div class="main">
    <div class="kanban">
      <div class="column col-queued">
        <div class="col-header">🟡 Queued <span class="col-count" id="cnt-queued">0</span></div>
        <div class="cards" id="col-queued"></div>
      </div>
      <div class="column col-progress">
        <div class="col-header">🔵 In Progress <span class="col-count" id="cnt-inprog">0</span></div>
        <div class="cards" id="col-inprog"></div>
      </div>
      <div class="column col-done">
        <div class="col-header">✅ Done <span class="col-count" id="cnt-done">0</span></div>
        <div class="cards" id="col-done"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══════════════ TRAINING ═══════════════ -->
<div id="pane-training" class="pane">
  <div class="train-main">
    <!-- KPI row -->
    <div class="train-kpi-row">
      <div class="train-kpi">
        <div class="train-kpi-label">Training Runs</div>
        <div class="train-kpi-value" id="tr-runs">—</div>
        <div class="train-kpi-sub" id="tr-runs-sub">promoted</div>
      </div>
      <div class="train-kpi">
        <div class="train-kpi-label">Overall Benchmark</div>
        <div class="train-kpi-value" id="tr-score" style="color:var(--green)">—</div>
        <div class="train-kpi-sub" id="tr-score-sub">latest run</div>
      </div>
      <div class="train-kpi">
        <div class="train-kpi-label">Tests Passing</div>
        <div class="train-kpi-value" id="tr-tests" style="color:var(--yellow)">—</div>
        <div class="train-kpi-sub">passed / total</div>
      </div>
      <div class="train-kpi">
        <div class="train-kpi-label">Last Training</div>
        <div class="train-kpi-value" id="tr-last" style="font-size:13px;padding-top:4px">—</div>
        <div class="train-kpi-sub">MLX overnight</div>
      </div>
    </div>

    <!-- Charts row -->
    <div class="train-grid2">
      <div class="train-card">
        <div class="train-card-title">EVAL SCORE — TRAINING HISTORY</div>
        <div class="chart-wrap"><canvas id="tr-score-chart"></canvas></div>
      </div>
      <div class="train-card">
        <div class="train-card-title">CATEGORY BENCHMARK SCORES</div>
        <div class="chart-wrap"><canvas id="tr-cat-chart"></canvas></div>
      </div>
    </div>

    <!-- Tables row -->
    <div class="train-grid2">
      <!-- Category detail -->
      <div class="train-card">
        <div class="train-card-title">BENCHMARK BY CATEGORY</div>
        <table class="train-table">
          <thead><tr><th>CATEGORY</th><th>SCORE</th><th>PASS/TOTAL</th><th>BAR</th></tr></thead>
          <tbody id="tr-cat-table"></tbody>
        </table>
      </div>
      <!-- Run history -->
      <div class="train-card">
        <div class="train-card-title">OVERNIGHT RUN HISTORY</div>
        <div style="max-height:260px;overflow-y:auto;">
          <table class="train-table">
            <thead><tr><th>DATE</th><th>SCORE</th><th>PASS/TOTAL</th><th>EX</th><th>DUR</th><th>↑</th></tr></thead>
            <tbody id="tr-run-table"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Routing -->
    <div class="train-card">
      <div class="train-card-title">SELF-EVAL ROUTE DISTRIBUTION</div>
      <div id="tr-routing" style="font-size:12px;color:var(--muted);padding:4px 0;">Loading…</div>
    </div>
  </div>
</div>

<!-- ═══════════════ OPS ═══════════════ -->
<div id="pane-ops" class="pane">
  <div class="ops-frame-wrap">
    <div class="ops-banner">
      <strong>Agent Ops Dashboard</strong>
      <span>— embedded from <code style="color:var(--cyan)">http://localhost:8765/dashboard</code></span>
      <span style="margin-left:auto;">
        <a href="http://localhost:8765/dashboard" target="_blank"
           style="color:var(--blue);text-decoration:none;font-size:11px;">↗ Open standalone</a>
      </span>
    </div>
    <iframe id="ops-iframe" src="about:blank" title="Agent Ops Dashboard"></iframe>
    <div class="ops-error" id="ops-error">
      <h3>⚙️ Ops server not reachable</h3>
      <p>Start the Jarvis API server on port 8765 to see the full Ops dashboard here.</p>
      <p style="font-size:11px;margin-top:8px;">
        <code style="color:var(--cyan)">python3 -m uvicorn api:app --port 8765</code>
      </p>
    </div>
  </div>
</div>

<!-- ═══════════════ ACTIVITY ═══════════════ -->
<div id="pane-activity" class="pane">
  <div class="activity-main">
    <!-- Log feed -->
    <div class="panel" style="flex:none;">
      <div class="panel-header">📋 MASTER_LOG.md — Full Feed</div>
      <div class="activity-feed" id="act-log-feed" style="max-height:300px;"></div>
    </div>

    <!-- Self-eval table -->
    <div class="panel">
      <div class="panel-header">📊 Self-Eval Scores — Recent 50</div>
      <div style="overflow-x:auto;max-height:400px;overflow-y:auto;">
        <table class="eval-table">
          <thead style="position:sticky;top:0;background:var(--bg);">
            <tr>
              <th>TIME</th><th>QUERY</th><th>ROUTE</th>
              <th>QUALITY</th><th>RELEVANCE</th><th>FLAGS</th>
            </tr>
          </thead>
          <tbody id="act-eval-table"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>


<script>
// ── helpers ──────────────────────────────────────────────────────────────────
function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function fmt(n) { return n != null ? n.toLocaleString() : '—'; }

function aiTagClass(ai) {
  const a = (ai || '').toLowerCase();
  if (a.includes('codex'))  return 'tag-codex';
  if (a.includes('gemini')) return 'tag-gemini';
  return 'tag-claude';
}

function renderCard(t, isActive) {
  const aiClass = aiTagClass(t.assigned_ai);
  const commit = t.commit ? `<span class="commit-badge">${esc(t.commit.slice(0,7))}</span>` : '';
  return `<div class="card ${isActive?'card-active':''} ${t.status==='done'?'card-done':''}">
    <div class="card-title">${esc(t.title)}</div>
    ${t.notes ? `<div class="card-snippet">${esc(t.notes)}</div>` : ''}
    <div class="card-meta">
      <span class="tag ${aiClass}">${esc(t.assigned_ai)}</span>
      <span class="tag tag-domain">${esc(t.domain)}</span>
      ${commit}
      <span class="time-ago">${esc(t.time_ago)}</span>
    </div>
  </div>`;
}

function renderLog(lines, maxH) {
  if (!lines || !lines.length) return '<div class="empty-state">No log entries.</div>';
  return lines.slice().reverse().map(line => {
    const m = line.match(/^\[([^\]]+)\]\s*\[([^\]]+)\]\s*(.*)$/);
    if (m) return `<div class="log-line"><span class="log-ts">[${esc(m[1])}]</span> <span class="log-actor">[${esc(m[2])}]</span> <span class="log-body">${esc(m[3])}</span></div>`;
    return `<div class="log-line">${esc(line)}</div>`;
  }).join('');
}

function renderModels(models) {
  if (!models || !models.length) return '<div class="empty-state">ollama not running or no models.</div>';
  return models.map(m => `<div class="model-row">
    <span class="model-name" title="${esc(m.name)}">${esc(m.name)}</span>
    <span class="model-size">${esc(m.size)}</span>
    <span class="model-status status-ready">${esc(m.status)}</span>
  </div>`).join('');
}

function renderOrch(sessions) {
  if (!sessions || !sessions.length) return '<div class="empty-state">No orchestrator sessions.</div>';
  return sessions.map(s => `<div class="orch-row">
    <div class="orch-dot ${s.status==='active'?'orch-active':'orch-idle'}"></div>
    <div class="orch-info">
      <div class="orch-name">${esc(s.name)}</div>
      <div class="orch-owner">${esc(s.owner)} · ${esc(s.last_active)}</div>
      <div class="orch-task">${esc(s.current_task||s.note)}</div>
    </div>
  </div>`).join('');
}

// ── status polling ────────────────────────────────────────────────────────────
let _statusData = null;

async function fetchStatus() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _statusData = await r.json();
    applyStatus(_statusData);
  } catch(e) {
    document.getElementById('refresh-ts').textContent = 'Error: ' + e.message;
  }
}

function applyStatus(data) {
  const st = data.stats || {};
  document.getElementById('s-done').textContent   = fmt(st.total_done);
  document.getElementById('s-inprog').textContent = fmt(st.in_progress);
  document.getElementById('s-queued').textContent = fmt(st.queued);
  document.getElementById('s-qual').textContent   = st.avg_quality != null ? (st.avg_quality*100).toFixed(1)+'%' : '—';
  document.getElementById('s-tokens').textContent = st.tokens_in_today != null ? fmt(st.tokens_in_today) : '—';

  // Kanban
  const qEl = document.getElementById('col-queued');
  qEl.innerHTML = data.queued && data.queued.length
    ? data.queued.map(t => renderCard(t, false)).join('')
    : '<div class="empty-state">Nothing queued 🎉</div>';
  document.getElementById('cnt-queued').textContent = (data.queued||[]).length;

  const ipEl = document.getElementById('col-inprog');
  ipEl.innerHTML = data.in_progress && data.in_progress.length
    ? data.in_progress.map(t => renderCard(t, true)).join('')
    : '<div class="empty-state">No active tasks.</div>';
  document.getElementById('cnt-inprog').textContent = (data.in_progress||[]).length;

  const dEl = document.getElementById('col-done');
  dEl.innerHTML = data.done && data.done.length
    ? data.done.map(t => renderCard(t, false)).join('')
    : '<div class="empty-state">No completed tasks.</div>';
  document.getElementById('cnt-done').textContent = st.total_done || 0;

  // Overview panels
  const logHtml = renderLog(data.log_lines);
  const ovFeed = document.getElementById('ov-log-feed');
  if (ovFeed) { ovFeed.innerHTML = logHtml; ovFeed.scrollTop = ovFeed.scrollHeight; }

  document.getElementById('ov-model-list').innerHTML = renderModels(data.models);
  document.getElementById('ov-orch-list').innerHTML = renderOrch(data.orchestrator);

  const ts = data.refreshed_at ? new Date(data.refreshed_at).toLocaleTimeString() : '—';
  document.getElementById('refresh-ts').textContent = 'Updated ' + ts;
}

// ── training charts ────────────────────────────────────────────────────────────
let _trScoreChart = null, _trCatChart = null;

const CHART_OPTS = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { labels: { color:'#8b949e', font:{family:'system-ui',size:9}, boxWidth:10, padding:10 } },
    tooltip: { backgroundColor:'#161b22', borderColor:'#21262d', borderWidth:1, titleColor:'#58a6ff', bodyColor:'#e6edf3' },
  },
  scales: {
    x: { ticks:{color:'#8b949e',font:{size:9}}, grid:{color:'rgba(33,38,45,.8)'} },
    y: { ticks:{color:'#8b949e',font:{size:9}}, grid:{color:'rgba(33,38,45,.8)'}, min:0, max:100 },
  },
};

async function fetchTraining() {
  try {
    const r = await fetch('/api/training');
    const d = await r.json();
    applyTraining(d);
  } catch(e) { console.error('Training fetch error:', e); }
}

function applyTraining(d) {
  const kpi = d.kpi || {};
  document.getElementById('tr-runs').textContent = kpi.total_runs ?? '—';
  document.getElementById('tr-runs-sub').textContent = kpi.trained_ok != null ? `${kpi.trained_ok} promoted` : '—';

  const sc = kpi.overall_score;
  document.getElementById('tr-score').textContent = sc != null ? sc.toFixed(1)+'%' : '—';
  if (sc != null) {
    document.getElementById('tr-score').style.color = sc >= 99 ? 'var(--green)' : sc >= 90 ? 'var(--cyan)' : 'var(--yellow)';
  }

  const p = kpi.latest_passed, t = kpi.latest_total;
  document.getElementById('tr-tests').textContent = (p != null && t != null) ? `${p}/${t}` : '—';
  document.getElementById('tr-last').textContent = kpi.last_ts || '—';

  // Score history chart
  const labels = d.chart_labels || [];
  const scores = d.chart_scores || [];
  if (_trScoreChart) { _trScoreChart.destroy(); }
  const ctx1 = document.getElementById('tr-score-chart').getContext('2d');
  _trScoreChart = new Chart(ctx1, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Eval %',
        data: scores,
        borderColor: '#00CFFF',
        backgroundColor: 'rgba(0,207,255,.07)',
        fill: true, tension: 0.42,
        pointRadius: 3, pointBackgroundColor: '#00CFFF',
        borderWidth: 2, spanGaps: true,
      }],
    },
    options: { ...CHART_OPTS, plugins:{ ...CHART_OPTS.plugins, legend:{display:false} } },
  });

  // Category bar chart
  if (_trCatChart) { _trCatChart.destroy(); }
  const ctx2 = document.getElementById('tr-cat-chart').getContext('2d');
  _trCatChart = new Chart(ctx2, {
    type: 'bar',
    data: {
      labels: d.cat_labels || [],
      datasets: [{
        label: 'Score %',
        data: d.cat_scores || [],
        backgroundColor: (d.cat_colors || []).map(c => c + '30'),
        borderColor: d.cat_colors || [],
        borderWidth: 1.5, borderRadius: 3,
      }],
    },
    options: {
      ...CHART_OPTS,
      plugins:{ ...CHART_OPTS.plugins, legend:{display:false} },
      scales:{ x:{ticks:{color:'#8b949e',font:{size:9}},grid:{display:false}}, y:{ticks:{color:'#8b949e',font:{size:9}},grid:{color:'rgba(33,38,45,.8)'},min:0,max:100} },
    },
  });

  // Category table
  const catRows = (d.cat_detail || []).map(c => {
    const sc = c.score;
    const colStr = c.color || '#8b949e';
    const pct = sc != null ? sc.toFixed(1)+'%' : '—';
    const pt  = (c.passed != null && c.total != null) ? `${c.passed}/${c.total}` : 'SKIP';
    const barW = sc != null ? sc : 0;
    return `<tr>
      <td style="color:${colStr};font-weight:600">${esc(c.label)}</td>
      <td style="color:${colStr}">${pct}</td>
      <td style="color:var(--muted)">${pt}</td>
      <td style="min-width:80px"><div class="bar-track"><div class="bar-fill" style="width:${barW}%;background:${colStr}"></div></div></td>
    </tr>`;
  }).join('');
  document.getElementById('tr-cat-table').innerHTML = catRows || '<tr><td colspan="4" style="color:var(--muted);padding:12px">No category data</td></tr>';

  // Run history table
  const runRows = (d.run_history || []).map(r => {
    const sc = r.score;
    const scStr = sc != null ? sc.toFixed(1)+'%' : '—';
    const scColor = sc != null ? (sc >= 99 ? '#3fb950' : sc >= 95 ? '#00CFFF' : '#d29922') : '#8b949e';
    const pt = (r.passed != null && r.total != null) ? `${r.passed}/${r.total}` : '—';
    const promo = r.promoted ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--border-hi)">—</span>';
    return `<tr>
      <td style="color:var(--muted);white-space:nowrap">${esc(r.label)}</td>
      <td style="color:${scColor}">${scStr}</td>
      <td style="color:var(--muted)">${pt}</td>
      <td style="color:var(--muted)">${r.examples||'—'}</td>
      <td style="color:var(--muted);white-space:nowrap">${esc(r.duration)}</td>
      <td>${promo}</td>
    </tr>`;
  }).join('');
  document.getElementById('tr-run-table').innerHTML = runRows || '<tr><td colspan="6" style="color:var(--muted);padding:12px">No runs yet</td></tr>';

  // Routing
  const routing = d.routing || {};
  const tiers = routing.tiers || {};
  const total = routing.total || 0;
  if (total === 0) {
    document.getElementById('tr-routing').innerHTML = '<span style="color:var(--muted)">No routing data</span>';
  } else {
    const rows = Object.entries(tiers).map(([k, v]) => {
      const pct = total ? (v / total * 100).toFixed(1) : '0.0';
      const w = total ? (v / total * 100) : 0;
      const col = k.includes('local') ? 'var(--green)' : k.includes('sonnet') ? 'var(--cyan)' : k.includes('mini') ? 'var(--blue)' : 'var(--yellow)';
      return `<div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px">
          <span style="color:var(--text);font-weight:600">${esc(k)}</span>
          <span style="color:${col}">${v.toLocaleString()} (${pct}%)</span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${col}"></div></div>
      </div>`;
    }).join('');
    document.getElementById('tr-routing').innerHTML = `<div style="font-size:11px;color:var(--muted);margin-bottom:10px">${total.toLocaleString()} total self-eval entries</div>${rows}`;
  }
}

// ── activity tab ──────────────────────────────────────────────────────────────
async function fetchActivity() {
  try {
    const r = await fetch('/api/activity');
    const d = await r.json();
    applyActivity(d);
  } catch(e) { console.error('Activity fetch error:', e); }
}

function scoreClass(q) {
  if (q == null) return '';
  if (q >= 0.7) return 'score-high';
  if (q >= 0.5) return 'score-mid';
  return 'score-low';
}

function applyActivity(d) {
  const logFeed = document.getElementById('act-log-feed');
  if (logFeed) { logFeed.innerHTML = renderLog(d.log_lines); logFeed.scrollTop = logFeed.scrollHeight; }

  const evalRows = (d.self_eval || []).map(e => {
    const q = e.quality;
    const qStr = q != null ? (q*100).toFixed(0)+'%' : '—';
    const rel  = e.relevance != null ? (e.relevance*100).toFixed(0)+'%' : '—';
    const ts   = e.ts ? e.ts.slice(0,16).replace('T',' ') : '—';
    const flags = (e.flags || []).map(f => `<span class="flag-pill">${esc(f)}</span>`).join('');
    return `<tr>
      <td style="color:var(--muted);white-space:nowrap;font-family:ui-monospace,monospace">${esc(ts)}</td>
      <td title="${esc(e.query)}">${esc(e.query)}</td>
      <td style="color:var(--muted)">${esc(e.route)}</td>
      <td><span class="score-pill ${scoreClass(q)}">${qStr}</span></td>
      <td><span class="score-pill ${scoreClass(e.relevance)}">${rel}</span></td>
      <td>${flags}</td>
    </tr>`;
  }).join('');
  document.getElementById('act-eval-table').innerHTML = evalRows || '<tr><td colspan="6" style="color:var(--muted);padding:16px">No self-eval data yet.</td></tr>';
}

// ── ops iframe ────────────────────────────────────────────────────────────────
let _opsLoaded = false;

function loadOpsFrame() {
  if (_opsLoaded) return;
  _opsLoaded = true;
  const iframe = document.getElementById('ops-iframe');
  const errDiv = document.getElementById('ops-error');

  // Try loading — detect failure via timeout (iframe doesn't fire onerror for network errors)
  const timeout = setTimeout(() => {
    // If we can't tell, just show the iframe; if it fails it shows browser error
  }, 3000);

  iframe.onload = () => { clearTimeout(timeout); };
  iframe.src = 'http://localhost:8765/dashboard';
}

// ── tab switching ─────────────────────────────────────────────────────────────
const TAB_FETCH = {
  overview:  () => _statusData && applyStatus(_statusData),
  workqueue: () => _statusData && applyStatus(_statusData),
  training:  fetchTraining,
  ops:       loadOpsFrame,
  activity:  fetchActivity,
};
let _currentTab = 'overview';

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.id === 'pane-' + name));
  _currentTab = name;
  if (TAB_FETCH[name]) TAB_FETCH[name]();
}

// ── boot ──────────────────────────────────────────────────────────────────────
fetchStatus();
setInterval(fetchStatus, 15000);
setInterval(() => {
  if (_currentTab === 'training')  fetchTraining();
  if (_currentTab === 'activity')  fetchActivity();
}, 30000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(HTML)


# ─── entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import webbrowser
    import threading

    print("🤖 Jarvis Master Dashboard → http://localhost:7842")
    print("   Tabs: Overview | Work Queue | Training | Ops | Activity")
    print("   Ops tab embeds http://localhost:8765/dashboard (start api.py separately)")
    threading.Timer(1.2, lambda: webbrowser.open("http://localhost:7842")).start()
    uvicorn.run(app, host="127.0.0.1", port=7842, log_level="warning")
