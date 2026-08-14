"""
Training Dashboard Generator for Jarvis.

Reads overnight_log.jsonl, benchmarks.jsonl, and overnight_state.json
then writes a self-contained training/dashboard.html.

Run manually:   python3 training/dashboard_generator.py
Auto-generated: after every overnight training run
Open:           open training/dashboard.html
"""

from __future__ import annotations

import json
import html as html_lib
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_runtime import local_improvement

TRAINING_ROOT = Path(__file__).parent
OVERNIGHT_LOG = TRAINING_ROOT / "overnight_log.jsonl"
BENCHMARK_LOG = TRAINING_ROOT / "benchmarks.jsonl"
STATE_FILE = TRAINING_ROOT / "overnight_state.json"
OUTPUT = TRAINING_ROOT / "dashboard.html"

CATEGORIES = ["voice", "calendar", "code", "memory", "tools", "conversation", "meeting"]
CATEGORY_LABELS = {
    "voice": "Voice / STT",
    "calendar": "Calendar",
    "code": "Code",
    "memory": "Memory",
    "tools": "Tools",
    "conversation": "Conversation",
    "meeting": "Meeting",
}


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _pct(val: Optional[float]) -> str:
    if val is None:
        return "—"
    return f"{val * 100:.1f}%"


def _delta_str(val: Optional[float]) -> str:
    if val is None:
        return "—"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val * 100:.1f}%"


def _color_for_score(score: Optional[float]) -> str:
    if score is None:
        return "#4A8FA8"
    if score >= 0.90:
        return "#00FF88"
    if score >= 0.75:
        return "#00D4FF"
    if score >= 0.60:
        return "#FFAA00"
    return "#FF4444"


def _count_jsonl_rows(path: str | None) -> int:
    if not path:
        return 0
    p = Path(path)
    if not p.exists():
        return 0
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return 0


def _run_eval(run: dict) -> dict:
    stages = run.get("stages", {}) if isinstance(run.get("stages"), dict) else {}
    nested = stages.get("eval", {}) if isinstance(stages.get("eval"), dict) else {}
    passed = run.get("eval_passed", nested.get("passed"))
    total = run.get("eval_total", nested.get("total"))
    if (not total or total == 0) and nested.get("total"):
        passed = nested.get("passed")
        total = nested.get("total")
    try:
        passed_i = int(passed)
        total_i = int(total)
    except (TypeError, ValueError):
        return {"passed": None, "total": None, "score": None}
    if total_i <= 0:
        return {"passed": passed_i, "total": total_i, "score": None}
    return {"passed": passed_i, "total": total_i, "score": passed_i / total_i}


def _run_examples(run: dict) -> int:
    try:
        count = int(run.get("examples_count") or 0)
    except (TypeError, ValueError):
        count = 0
    if count > 0:
        return count
    stages = run.get("stages", {}) if isinstance(run.get("stages"), dict) else {}
    build = stages.get("build", {}) if isinstance(stages.get("build"), dict) else {}
    return _count_jsonl_rows(build.get("pack_path"))


def _run_duration(run: dict) -> float:
    for key in ("duration_seconds", "duration_sec"):
        try:
            value = float(run.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    stages = run.get("stages", {}) if isinstance(run.get("stages"), dict) else {}
    training = stages.get("training", {}) if isinstance(stages.get("training"), dict) else {}
    for key in ("duration_seconds", "duration_sec"):
        try:
            value = float(training.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0.0


def _run_trained_ok(run: dict) -> bool:
    stages = run.get("stages", {}) if isinstance(run.get("stages"), dict) else {}
    training = stages.get("training", {}) if isinstance(stages.get("training"), dict) else {}
    return bool(training.get("ok"))


def _run_label(run: dict, index: int) -> str:
    timestamp = str(run.get("timestamp") or "")
    if "T" in timestamp:
        return timestamp.replace("T", " ").replace("Z", "")[5:16]
    return str(run.get("date") or f"Run {index + 1}")


def _latest_learning_run(runs: list[dict]) -> dict:
    for run in reversed(runs):
        if _run_trained_ok(run) or run.get("promoted"):
            return run
    return runs[-1] if runs else {}


def _baseline_counts(state: dict, benchmarks: list[dict], runs: list[dict]) -> tuple[int, int]:
    for bench in reversed(benchmarks):
        total = int(bench.get("total_tests") or 0)
        if total > 0:
            return int(bench.get("total_passed") or 0), total
    for run in reversed(runs):
        evaluated = _run_eval(run)
        if evaluated["total"]:
            return int(evaluated["passed"] or 0), int(evaluated["total"] or 0)
    try:
        state_passed = int(state.get("baseline_eval_passed") or 0)
        state_total = int(state.get("baseline_eval_total") or 0)
    except (TypeError, ValueError):
        state_passed = 0
        state_total = 0
    if state_total > 0:
        return state_passed, state_total
    return 0, 1


def _routing_stats(repo_root: Path) -> dict:
    """
    Load routing_log.jsonl and compute tier breakdown and local-usage rate.

    Returns:
        {
          "total": int,
          "tiers": {"local": N, "sonnet": N, ...},
          "local_pct": float,   # 0-1
          "cloud_pct": float,
        }
    """
    routing_log = repo_root / "routing_log.jsonl"
    records = _load_jsonl(routing_log)
    if not records:
        return {"total": 0, "tiers": {}, "local_pct": 0.0, "cloud_pct": 0.0}

    from collections import Counter

    tiers: Counter = Counter()
    for r in records:
        tier = r.get("tier") or "unknown"
        tiers[tier] += 1

    total = len(records)
    local_count = tiers.get("local", 0)
    local_pct = local_count / total if total else 0.0

    return {
        "total": total,
        "tiers": dict(tiers.most_common()),
        "local_pct": local_pct,
        "cloud_pct": 1.0 - local_pct,
    }


def generate() -> Path:
    overnight_runs = _load_jsonl(OVERNIGHT_LOG)
    benchmarks = _load_jsonl(BENCHMARK_LOG)
    state = _load_json(STATE_FILE, {})
    improvement = local_improvement.status()
    improvement_counts = improvement["dataset_counts"]
    improvement_candidate = improvement.get("candidate_model", {})
    improvement_baseline = improvement.get("baseline_model", {})

    # ── Summary stats ──────────────────────────────────────────────────────────
    total_runs = len(overnight_runs)
    promoted_runs = sum(1 for r in overnight_runs if r.get("promoted"))
    trained_runs = sum(1 for r in overnight_runs if _run_trained_ok(r))
    last_run = _latest_learning_run(overnight_runs)
    last_run_date = _run_label(last_run, len(overnight_runs) - 1) if last_run else "—"
    baseline_passed, baseline_total = _baseline_counts(state, benchmarks, overnight_runs)
    baseline_pct = f"{baseline_passed / baseline_total * 100:.1f}%"

    # Latest benchmark
    latest_bench = benchmarks[-1] if benchmarks else {}
    overall_score = latest_bench.get("overall")

    # ── Chart data ─────────────────────────────────────────────────────────────
    # Dates for x-axis (overnight runs)
    run_dates = [_run_label(r, i) for i, r in enumerate(overnight_runs)]
    run_scores = []
    for run in overnight_runs:
        evaluated = _run_eval(run)
        run_scores.append(
            round(evaluated["score"] * 100, 1) if evaluated["score"] is not None else None
        )
    run_examples = [_run_examples(r) for r in overnight_runs]

    # Category scores from latest benchmark
    cat_scores = []
    cat_colors = []
    if latest_bench:
        cats = latest_bench.get("categories", {})
        for cat in CATEGORIES:
            score = cats.get(cat, {}).get("score")
            cat_scores.append(round((score or 0) * 100, 1))
            cat_colors.append(_color_for_score(score))
    else:
        cat_scores = [0] * len(CATEGORIES)
        cat_colors = ["#4A8FA8"] * len(CATEGORIES)

    # Per-category trend lines (last 10 benchmark runs)
    bench_dates = [b.get("run_date", f"B{i+1}") for i, b in enumerate(benchmarks[-10:])]
    cat_trend_datasets = []
    TREND_COLORS = ["#00D4FF", "#00FF88", "#FFAA00", "#FF6B00", "#AA66FF", "#FF4488", "#44FFCC"]
    for i, cat in enumerate(CATEGORIES):
        trend_data = []
        for b in benchmarks[-10:]:
            score = b.get("categories", {}).get(cat, {}).get("score")
            trend_data.append(round(score * 100, 1) if score is not None else None)
        cat_trend_datasets.append({
            "label": CATEGORY_LABELS[cat],
            "data": trend_data,
            "borderColor": TREND_COLORS[i % len(TREND_COLORS)],
            "backgroundColor": "transparent",
            "tension": 0.4,
            "pointRadius": 4,
            "spanGaps": True,
        })

    # ── Category table rows ────────────────────────────────────────────────────
    cat_rows_html = ""
    if latest_bench:
        cats = latest_bench.get("categories", {})
        prev_bench = benchmarks[-2] if len(benchmarks) >= 2 else {}
        prev_cats = prev_bench.get("categories", {}) if prev_bench else {}
        for cat in CATEGORIES:
            info = cats.get(cat, {})
            score = info.get("score")
            passed = info.get("passed", "—")
            total = info.get("total", "—")
            prev_score = prev_cats.get(cat, {}).get("score") if prev_cats else None
            delta = round(score - prev_score, 4) if (score is not None and prev_score is not None) else None
            skipped = info.get("skipped", False)
            color = _color_for_score(score)
            delta_html = ""
            if delta is not None:
                d_color = "#00FF88" if delta >= 0 else "#FF4444"
                d_sign = "+" if delta >= 0 else ""
                delta_html = f'<span style="color:{d_color};font-size:11px">{d_sign}{delta*100:.1f}%</span>'
            status = "SKIP" if skipped else f"{passed}/{total}"
            cat_rows_html += f"""
            <tr>
              <td style="color:{color};font-weight:bold">{CATEGORY_LABELS[cat]}</td>
              <td style="color:{color}">{_pct(score)}</td>
              <td style="color:#A8E6FF">{status}</td>
              <td>{delta_html or "—"}</td>
            </tr>"""
    else:
        cat_rows_html = '<tr><td colspan="4" style="color:#4A8FA8;text-align:center">No benchmark data yet — runs after first overnight training</td></tr>'

    # ── Run history rows ───────────────────────────────────────────────────────
    run_rows_html = ""
    for idx, r in reversed(list(enumerate(overnight_runs[-20:]))):
        evaluated = _run_eval(r)
        ep = evaluated["passed"]
        et = evaluated["total"]
        sc = f"{round(evaluated['score'] * 100, 1)}%" if evaluated["score"] is not None else "—"
        promo = "✓" if r.get("promoted") else "—"
        promo_color = "#00FF88" if r.get("promoted") else "#4A8FA8"
        ex = _run_examples(r)
        dur = _run_duration(r)
        dur_str = f"{int(dur // 60)}m {int(dur % 60)}s" if dur else "—"
        pass_total = f"{ep}/{et}" if et is not None and et > 0 else "—"
        run_rows_html += f"""
        <tr>
          <td style="color:#A8E6FF">{_run_label(r, idx)}</td>
          <td style="color:#00D4FF">{sc}</td>
          <td style="color:#A8E6FF">{pass_total}</td>
          <td style="color:#A8E6FF">{ex}</td>
          <td style="color:#A8E6FF">{dur_str}</td>
          <td style="color:{promo_color};font-weight:bold">{promo}</td>
        </tr>"""
    if not run_rows_html:
        run_rows_html = '<tr><td colspan="6" style="color:#4A8FA8;text-align:center">No training runs yet — first run at 11pm tonight</td></tr>'

    # ── Routing stats ──────────────────────────────────────────────────────────
    routing = _routing_stats(TRAINING_ROOT.parent)
    routing_total = routing["total"]
    routing_local_pct = round(routing["local_pct"] * 100, 1)
    routing_cloud_pct = round(routing["cloud_pct"] * 100, 1)
    routing_tiers = routing["tiers"]  # {"local": N, "sonnet": N, ...}

    # Build tier rows HTML for the routing table
    tier_color_map = {
        "local": "#00FF88",
        "sonnet": "#00D4FF",
        "mini": "#A8E6FF",
        "haiku": "#FFAA00",
        "opus": "#FF6B00",
        "unknown": "#4A8FA8",
    }
    routing_rows_html = ""
    for tier, count in routing_tiers.items():
        pct = round(count / routing_total * 100, 1) if routing_total else 0
        color = tier_color_map.get(tier, "#A8E6FF")
        routing_rows_html += f"""
        <tr>
          <td style="color:{color};font-weight:bold">{tier}</td>
          <td style="color:#A8E6FF">{count:,}</td>
          <td style="color:{color}">{pct}%</td>
        </tr>"""
    if not routing_rows_html:
        routing_rows_html = '<tr><td colspan="3" style="color:#4A8FA8">No routing data yet</td></tr>'

    # Local-use gauge bar (HTML progress bar style)
    local_bar_color = "#00FF88" if routing_local_pct >= 30 else ("#FFAA00" if routing_local_pct >= 15 else "#FF4444")

    # ── Serialize chart data ───────────────────────────────────────────────────
    chart_json = json.dumps({
        "run_dates": run_dates,
        "run_scores": run_scores,
        "run_examples": run_examples,
        "cat_labels": [CATEGORY_LABELS[c] for c in CATEGORIES],
        "cat_scores": cat_scores,
        "cat_colors": cat_colors,
        "bench_dates": bench_dates,
        "cat_trend_datasets": cat_trend_datasets,
    })

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── HTML template ──────────────────────────────────────────────────────────
    _score_color = _color_for_score(overall_score)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="60">
  <title>J.A.R.V.I.S. — Training Intelligence</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    /* ── Reset & Variables ── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:        #050507;
      --bg2:       #07090F;
      --card:      #080D14;
      --border:    #0A2233;
      --cyan:      #00CFFF;
      --cyan-dim:  #005F80;
      --gold:      #FFB300;
      --green:     #00FF88;
      --red:       #FF4444;
      --text:      #7EC8DC;
      --text-dim:  #2E5A6A;
      --font:      'Courier New', 'Lucida Console', monospace;
    }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 12px;
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
    }}

    /* Scanlines */
    body::before {{
      content: '';
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background: repeating-linear-gradient(
        to bottom,
        transparent 0px, transparent 2px,
        rgba(0,207,255,0.016) 2px, rgba(0,207,255,0.016) 3px
      );
      pointer-events: none;
      z-index: 9999;
    }}

    /* Hex grid background — subtle structural lattice */
    body::after {{
      content: '';
      position: fixed; top: 0; left: 0; right: 0; bottom: 0;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='60' height='104'%3E%3Cpath d='M30 4L4 19v30l26 15 26-15V19L30 4zm0 5l21 12.1v24.2L30 57.4 9 45.3V21.1L30 9z' fill='none' stroke='%2300CFFF' stroke-opacity='0.035' stroke-width='1'/%3E%3C/svg%3E");
      pointer-events: none;
      z-index: 0;
    }}

    .layout {{
      position: relative; z-index: 1;
      padding: 20px 26px 32px;
      max-width: 1640px;
      margin: 0 auto;
    }}

    /* ── Topbar ── */
    .topbar {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      margin-bottom: 22px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }}
    .title-h1 {{
      font-size: 22px;
      letter-spacing: 10px;
      color: var(--cyan);
      text-shadow: 0 0 22px rgba(0,207,255,0.65), 0 0 55px rgba(0,207,255,0.22);
      font-weight: bold;
      line-height: 1;
    }}
    .title-sub {{
      color: var(--text-dim);
      font-size: 9px;
      letter-spacing: 3px;
      margin-top: 5px;
    }}

    /* ── Status chips ── */
    .chips {{ display: flex; gap: 7px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }}
    .chip {{
      display: inline-flex; align-items: center; gap: 5px;
      padding: 4px 10px;
      border-radius: 2px;
      font-size: 9px; letter-spacing: 2px; font-weight: bold;
      border: 1px solid currentColor;
    }}
    .chip::before {{ content: '●'; font-size: 5px; }}
    .chip-green {{ color: var(--green); border-color: rgba(0,255,136,0.35); background: rgba(0,255,136,0.05); text-shadow: 0 0 8px currentColor; }}
    .chip-cyan  {{ color: var(--cyan);  border-color: rgba(0,207,255,0.35); background: rgba(0,207,255,0.05); text-shadow: 0 0 8px currentColor; }}
    .chip-gold  {{ color: var(--gold);  border-color: rgba(255,179,0,0.35);  background: rgba(255,179,0,0.05);  text-shadow: 0 0 8px currentColor; }}
    .chip-dim   {{ color: var(--text-dim); border-color: var(--border); background: transparent; }}
    .ts {{ color: var(--text-dim); font-size: 9px; letter-spacing: 2px; margin-top: 7px; text-align: right; }}

    /* ── Orb ── */
    .orb-section {{ display: flex; justify-content: center; align-items: center; }}
    .orb-wrap {{ position: relative; width: 180px; height: 180px; flex-shrink: 0; }}
    .orb-ring {{
      position: absolute; border-radius: 50%;
      border: 1px solid rgba(0,207,255,0.22);
      top: 0; left: 0; right: 0; bottom: 0;
    }}
    .orb-r1 {{ top:8px; left:8px; right:8px; bottom:8px; animation: rspin 9s linear infinite; }}
    .orb-r2 {{ top:18px; left:18px; right:18px; bottom:18px; animation: rspin 15s linear infinite reverse; border-style: dashed; opacity:.5; }}
    .orb-r3 {{ top:28px; left:28px; right:28px; bottom:28px; animation: rspin 22s linear infinite; opacity:.28; }}
    @keyframes rspin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
    .orb-core {{
      position: absolute; top:38px; left:38px; right:38px; bottom:38px;
      border-radius: 50%;
      background: radial-gradient(circle at 38% 34%, rgba(0,207,255,0.55), rgba(0,70,130,0.65) 52%, rgba(2,14,42,0.85));
      box-shadow: 0 0 18px rgba(0,207,255,0.65), 0 0 48px rgba(0,207,255,0.3), inset 0 0 14px rgba(0,207,255,0.22);
      animation: opulse 3.2s ease-in-out infinite;
      display: flex; align-items: center; justify-content: center;
    }}
    @keyframes opulse {{
      0%,100% {{ box-shadow: 0 0 18px rgba(0,207,255,0.65), 0 0 48px rgba(0,207,255,0.30), inset 0 0 14px rgba(0,207,255,0.22); }}
      50%      {{ box-shadow: 0 0 28px rgba(0,207,255,0.85), 0 0 72px rgba(0,207,255,0.45), inset 0 0 22px rgba(0,207,255,0.35); }}
    }}
    .orb-text {{ color: var(--cyan); font-size: 9px; letter-spacing: 3px; text-shadow: 0 0 10px currentColor; }}

    /* ── Cards ── */
    .card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 3px;
      padding: 15px 17px;
      position: relative;
    }}
    /* Corner brackets */
    .card::before {{
      content: '';
      position: absolute; top: -1px; left: -1px;
      width: 10px; height: 10px;
      border-top: 2px solid var(--cyan);
      border-left: 2px solid var(--cyan);
    }}
    .card::after {{
      content: '';
      position: absolute; bottom: -1px; right: -1px;
      width: 10px; height: 10px;
      border-bottom: 2px solid var(--cyan);
      border-right: 2px solid var(--cyan);
    }}
    .card-label {{ color: var(--text-dim); font-size: 9px; letter-spacing: 2px; margin-bottom: 8px; }}
    .card-value {{ color: var(--cyan); font-size: 26px; font-weight: bold; text-shadow: 0 0 14px rgba(0,207,255,0.5); line-height: 1; }}
    .card-value.gold  {{ color: var(--gold);  text-shadow: 0 0 14px rgba(255,179,0,0.55); }}
    .card-value.green {{ color: var(--green); text-shadow: 0 0 14px rgba(0,255,136,0.55); }}
    .card-sub {{ color: var(--text-dim); font-size: 10px; margin-top: 5px; }}

    .section-title {{
      color: var(--cyan);
      font-size: 9px; letter-spacing: 3px;
      padding-bottom: 8px; margin-bottom: 13px;
      border-bottom: 1px solid rgba(0,207,255,0.12);
      text-shadow: 0 0 10px rgba(0,207,255,0.5);
      display: flex; align-items: center; gap: 8px;
    }}
    .section-title::before {{ content: '//'; color: var(--text-dim); font-size: 10px; }}

    /* ── Layout grids ── */
    .grid-4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 11px; margin-bottom: 16px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr;       gap: 11px; margin-bottom: 16px; }}

    /* ── Charts ── */
    .chart-wrap    {{ position: relative; height: 210px; }}
    .chart-wrap-lg {{ position: relative; height: 195px; }}

    /* ── Tables ── */
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      color: var(--text-dim); font-size: 9px; letter-spacing: 2px;
      text-align: left; padding: 7px 10px;
      border-bottom: 1px solid var(--border);
    }}
    td {{
      padding: 7px 10px;
      border-bottom: 1px solid rgba(10,34,51,0.6);
      font-size: 11px; color: var(--text);
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(0,207,255,0.03); }}

    /* ── Progress bars ── */
    .bar-track {{ background: rgba(10,34,51,0.9); border-radius: 2px; height: 5px; overflow: hidden; margin-top: 3px; }}
    .bar-fill  {{ height: 100%; border-radius: 2px; }}

    /* ── Footer ── */
    .footer {{
      color: var(--text-dim); font-size: 9px; text-align: center;
      margin-top: 18px; padding-top: 12px;
      border-top: 1px solid var(--border); letter-spacing: 2px;
    }}
  </style>
</head>
<body>
<div class="layout">

  <!-- ── Top bar ── -->
  <div class="topbar">
    <div>
      <div class="title-h1">J.A.R.V.I.S.</div>
      <div class="title-sub">JUST A RATHER VERY INTELLIGENT SYSTEM &nbsp;·&nbsp; TRAINING INTELLIGENCE FEED</div>
    </div>
    <div>
      <div class="chips">
        <span class="chip chip-green">ONLINE</span>
        <span class="chip chip-cyan">BENCHMARK {_pct(overall_score)}</span>
        <span class="chip chip-gold">MLX TRAINING ACTIVE</span>
        <span class="chip chip-dim">11PM NIGHTLY</span>
      </div>
      <div class="ts">AUTO-REFRESH 60s &nbsp;·&nbsp; SYNC {generated_at}</div>
    </div>
  </div>

  <!-- ── Orb + KPI cards ── -->
  <div style="display:grid;grid-template-columns:200px 1fr;gap:18px;align-items:center;margin-bottom:16px">
    <div class="orb-section">
      <div class="orb-wrap">
        <div class="orb-ring orb-r1"></div>
        <div class="orb-ring orb-r2"></div>
        <div class="orb-ring orb-r3"></div>
        <div class="orb-core"><span class="orb-text">JARVIS</span></div>
      </div>
    </div>
    <div class="grid-4" style="margin:0">
      <div class="card">
        <div class="card-label">TRAINING RUNS</div>
        <div class="card-value">{total_runs}</div>
        <div class="card-sub">{trained_runs} trained &middot; {promoted_runs} promoted</div>
      </div>
      <div class="card">
        <div class="card-label">OVERALL BENCHMARK</div>
        <div class="card-value" style="color:{_score_color};text-shadow:0 0 14px {_score_color}88">{_pct(overall_score)}</div>
        <div class="card-sub">latest benchmark run</div>
      </div>
      <div class="card">
        <div class="card-label">LATEST EVAL</div>
        <div class="card-value gold">{baseline_pct}</div>
        <div class="card-sub">{baseline_passed}/{baseline_total} tests passing</div>
      </div>
      <div class="card">
        <div class="card-label">LAST TRAINING</div>
        <div style="font-size:13px;color:var(--cyan);margin-top:5px;letter-spacing:1px;text-shadow:0 0 10px rgba(0,207,255,0.5)">{last_run_date or '—'}</div>
        <div class="card-sub">next: 23:00 tonight</div>
      </div>
    </div>
  </div>

  <!-- ── Score history + Category bars ── -->
  <div class="grid-2" style="margin-top:20px">
    <div class="card">
      <div class="section-title">EVAL SCORE — TRAINING HISTORY</div>
      <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
    </div>
    <div class="card">
      <div class="section-title">CATEGORY BENCHMARK SCORES</div>
      <div class="chart-wrap"><canvas id="catChart"></canvas></div>
    </div>
  </div>

  <!-- ── Category trend ── -->
  <div class="card" style="margin-bottom:16px">
    <div class="section-title">CATEGORY TRENDS — BENCHMARK HISTORY</div>
    <div class="chart-wrap-lg"><canvas id="trendChart"></canvas></div>
  </div>

  <!-- ── Category table + Run history ── -->
  <div class="grid-2">
    <div class="card">
      <div class="section-title">BENCHMARK BY CATEGORY</div>
      <table>
        <thead><tr><th>CATEGORY</th><th>SCORE</th><th>PASS/TOTAL</th><th>DELTA</th></tr></thead>
        <tbody>{cat_rows_html}</tbody>
      </table>
    </div>
    <div class="card">
      <div class="section-title">OVERNIGHT RUN HISTORY</div>
      <table>
        <thead><tr><th>DATE</th><th>SCORE</th><th>PASS/TOTAL</th><th>EXAMPLES</th><th>DUR</th><th>&#8593;</th></tr></thead>
        <tbody>{run_rows_html}</tbody>
      </table>
    </div>
  </div>

  <!-- ── Routing + Pack composition ── -->
  <div class="grid-2">
    <div class="card">
      <div class="section-title">MODEL ROUTING — ALL TIME</div>
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:10px">{routing_total:,} total queries</div>
      <div style="margin-bottom:14px">
        <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:4px">
          <span style="color:var(--text)">LOCAL USAGE RATE</span>
          <span style="color:{local_bar_color};font-weight:bold;text-shadow:0 0 8px {local_bar_color}">{routing_local_pct}%</span>
        </div>
        <div class="bar-track">
          <div class="bar-fill" style="background:{local_bar_color};width:{routing_local_pct}%;box-shadow:0 0 6px {local_bar_color}"></div>
        </div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:4px">&#8593; goal: maximize local · reduce cloud spend</div>
      </div>
      <table>
        <thead><tr><th>TIER</th><th>QUERIES</th><th>SHARE</th></tr></thead>
        <tbody>{routing_rows_html}</tbody>
      </table>
    </div>
    <div class="card">
      <div class="section-title">TRAINING PACK COMPOSITION</div>
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:10px">Ranked source quality for tonight's pack</div>
      <table>
        <thead><tr><th>SOURCE</th><th>TYPE</th><th>QUALITY</th></tr></thead>
        <tbody>
          <tr>
            <td style="color:var(--green);font-weight:bold">Teacher Examples</td>
            <td>Curated JSONL</td>
            <td style="color:var(--green)">&#9733;&#9733;&#9733;&#9733;&#9733;</td>
          </tr>
          <tr>
            <td style="color:var(--cyan);font-weight:bold">Verbatim (real)</td>
            <td>Live interactions</td>
            <td style="color:var(--cyan)">&#9733;&#9733;&#9733;&#9733;&#9734;</td>
          </tr>
          <tr>
            <td style="color:var(--gold);font-weight:bold">Synthetic</td>
            <td>Tool-use / voice / memory</td>
            <td style="color:var(--gold)">&#9733;&#9733;&#9733;&#9734;&#9734;</td>
          </tr>
          <tr>
            <td style="color:var(--text-dim);font-weight:bold">Legacy (fallback)</td>
            <td style="color:var(--text-dim)">Summary rephrasing</td>
            <td style="color:var(--text-dim)">&#9733;&#9733;&#9734;&#9734;&#9734;</td>
          </tr>
        </tbody>
      </table>
      <div style="margin-top:10px;font-size:9px;color:var(--text-dim)">Pack grows as real interactions accumulate in verbatim log.</div>
    </div>
  </div>

  <div class="card" style="margin-bottom:16px">
    <div class="section-title">GUARDED LOCAL IMPROVEMENT — APPROVAL STATUS</div>
    <table>
      <thead><tr><th>STAGE</th><th>DATASET</th><th>QUARANTINE</th><th>CANDIDATE / BASELINE</th><th>APPROVAL</th><th>ROLLBACK</th></tr></thead>
      <tbody><tr>
        <td style="color:var(--cyan)">{html_lib.escape(str(improvement['stage']))}</td>
        <td>{improvement_counts['train']} train / {improvement_counts['validation']} val / {improvement_counts['test']} held-out</td>
        <td>{improvement['quarantined_examples']}</td>
        <td>{html_lib.escape(str(improvement_candidate.get('tag', '—')))} / {html_lib.escape(str(improvement_baseline.get('tag', '—')))}</td>
        <td>{html_lib.escape(str(improvement['approval_state']))}</td>
        <td>{html_lib.escape(str(improvement.get('rollback_target') or '—'))}</td>
      </tr></tbody>
    </table>
    <div style="margin-top:10px;font-size:9px;color:var(--text-dim)">
      Local-only · immutable held-out test split · no raw-conversation training · no automatic promotion · candidate digest {html_lib.escape(str(improvement_candidate.get('digest', '—')))}
    </div>
  </div>

  <div class="footer">
    JARVIS LOCAL FINE-TUNING &nbsp;&middot;&nbsp; MLX APPLE SILICON &nbsp;&middot;&nbsp;
    23:00&ndash;07:00 TRAINING WINDOW &nbsp;&middot;&nbsp; benchmarks.jsonl + overnight_log.jsonl
  </div>
</div>

<script>
const DATA = {chart_json};

const C = {{ CYAN:'#00CFFF', GOLD:'#FFB300', GREEN:'#00FF88', DIM:'#2E5A6A', TEXT:'#7EC8DC' }};

const gridLine = 'rgba(10,34,51,0.7)';
const tickFont = {{ family: 'Courier New', size: 9 }};

const baseOpts = {{
  responsive: true,
  maintainAspectRatio: false,
  plugins: {{
    legend: {{ labels: {{ color: C.TEXT, font: tickFont, boxWidth: 10, padding: 12 }} }},
    tooltip: {{
      backgroundColor: '#07090F',
      borderColor: '#0A2233',
      borderWidth: 1,
      titleColor: C.CYAN,
      bodyColor: C.TEXT,
      titleFont: tickFont,
      bodyFont: tickFont,
    }},
  }},
  scales: {{
    x: {{ ticks: {{ color: C.DIM, font: tickFont }}, grid: {{ color: gridLine }} }},
    y: {{ ticks: {{ color: C.DIM, font: tickFont }}, grid: {{ color: gridLine }}, min: 0, max: 100 }},
  }},
}};

// Score history line chart
new Chart(document.getElementById('scoreChart'), {{
  type: 'line',
  data: {{
    labels: DATA.run_dates.length ? DATA.run_dates : ['No runs yet'],
    datasets: [{{
      label: 'Eval %',
      data: DATA.run_scores.length ? DATA.run_scores : [0],
      borderColor: C.CYAN,
      backgroundColor: 'rgba(0,207,255,0.07)',
      fill: true, tension: 0.42,
      pointRadius: 4, pointBackgroundColor: C.CYAN,
      borderWidth: 2,
    }}],
  }},
  options: {{ ...baseOpts, plugins: {{ ...baseOpts.plugins, legend: {{ display: false }} }} }},
}});

// Category bar chart
new Chart(document.getElementById('catChart'), {{
  type: 'bar',
  data: {{
    labels: DATA.cat_labels,
    datasets: [{{
      label: 'Score %',
      data: DATA.cat_scores,
      backgroundColor: DATA.cat_colors.map(c => c + '28'),
      borderColor: DATA.cat_colors,
      borderWidth: 1.5,
      borderRadius: 2,
    }}],
  }},
  options: {{
    ...baseOpts,
    plugins: {{ ...baseOpts.plugins, legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: C.DIM, font: tickFont }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ color: C.DIM, font: tickFont }}, grid: {{ color: gridLine }}, min: 0, max: 100 }},
    }},
  }},
}});

// Category trend lines
new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: DATA.bench_dates.length ? DATA.bench_dates : ['No data yet'],
    datasets: DATA.cat_trend_datasets.length ? DATA.cat_trend_datasets : [{{
      label: 'No benchmark data', data: [null], borderColor: C.DIM,
    }}],
  }},
  options: baseOpts,
}});

// Live clock update in chip
(function tick() {{
  const el = document.getElementById('live-ts');
  if (el) el.textContent = new Date().toLocaleTimeString('en-GB', {{hour:'2-digit',minute:'2-digit',second:'2-digit'}});
  setTimeout(tick, 1000);
}})();
</script>
</body>
</html>
"""

    OUTPUT.write_text(html, encoding="utf-8")
    print(f"[Dashboard] Written → {OUTPUT}")
    return OUTPUT


if __name__ == "__main__":
    path = generate()
    print(f"Open: open {path}")
