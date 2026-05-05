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
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def generate() -> Path:
    overnight_runs = _load_jsonl(OVERNIGHT_LOG)
    benchmarks = _load_jsonl(BENCHMARK_LOG)
    state = _load_json(STATE_FILE, {})

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
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="60">
  <title>Jarvis Training Dashboard</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #020A10;
      color: #A8E6FF;
      font-family: 'Courier New', monospace;
      font-size: 13px;
      padding: 24px;
      min-height: 100vh;
    }}
    h1 {{
      color: #00D4FF;
      font-size: 22px;
      letter-spacing: 4px;
      font-weight: bold;
      margin-bottom: 4px;
    }}
    .subtitle {{ color: #4A8FA8; font-size: 11px; letter-spacing: 2px; margin-bottom: 28px; }}
    .grid-4 {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 24px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 24px; }}
    .card {{
      background: rgba(3, 18, 28, 0.85);
      border: 1px solid #0D4F70;
      border-radius: 10px;
      padding: 18px 20px;
    }}
    .card-label {{ color: #4A8FA8; font-size: 10px; letter-spacing: 2px; margin-bottom: 8px; }}
    .card-value {{ color: #00D4FF; font-size: 28px; font-weight: bold; }}
    .card-sub {{ color: #A8E6FF; font-size: 11px; margin-top: 4px; }}
    .section-title {{
      color: #00D4FF;
      font-size: 11px;
      letter-spacing: 3px;
      margin-bottom: 14px;
      border-bottom: 1px solid #0D4F70;
      padding-bottom: 8px;
    }}
    .chart-card {{ padding: 20px; }}
    .chart-wrap {{ position: relative; height: 240px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      color: #4A8FA8;
      font-size: 10px;
      letter-spacing: 1px;
      text-align: left;
      padding: 8px 12px;
      border-bottom: 1px solid #0D4F70;
    }}
    td {{
      padding: 9px 12px;
      border-bottom: 1px solid rgba(13, 79, 112, 0.3);
      font-size: 12px;
    }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(0, 212, 255, 0.04); }}
    .footer {{
      color: #4A8FA8;
      font-size: 10px;
      text-align: center;
      margin-top: 24px;
      letter-spacing: 1px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 10px;
      font-weight: bold;
      letter-spacing: 1px;
    }}
    .badge-green {{ background: rgba(0,255,136,0.12); color: #00FF88; border: 1px solid #00FF88; }}
    .badge-cyan  {{ background: rgba(0,212,255,0.12); color: #00D4FF; border: 1px solid #00D4FF; }}
  </style>
</head>
<body>

<h1>J.A.R.V.I.S &mdash; TRAINING INTELLIGENCE</h1>
<div class="subtitle">FINE-TUNING DASHBOARD &nbsp;·&nbsp; AUTO-REFRESH 60s &nbsp;·&nbsp; GENERATED {generated_at}</div>

<!-- Summary cards -->
<div class="grid-4">
  <div class="card">
    <div class="card-label">TOTAL RUNS</div>
    <div class="card-value">{total_runs}</div>
    <div class="card-sub">{trained_runs} trained · {promoted_runs} promoted</div>
  </div>
  <div class="card">
    <div class="card-label">OVERALL BENCHMARK</div>
    <div class="card-value" style="color:{_color_for_score(overall_score)}">{_pct(overall_score)}</div>
    <div class="card-sub">latest benchmark run</div>
  </div>
  <div class="card">
    <div class="card-label">LATEST EVAL</div>
    <div class="card-value">{baseline_pct}</div>
    <div class="card-sub">{baseline_passed}/{baseline_total} tests passing</div>
  </div>
  <div class="card">
    <div class="card-label">LAST TRAINING</div>
    <div class="card-value" style="font-size:18px;padding-top:5px">{last_run_date or '—'}</div>
    <div class="card-sub">next run: 11:00 pm tonight</div>
  </div>
</div>

<!-- Charts row -->
<div class="grid-2">
  <div class="card chart-card">
    <div class="section-title">OVERALL EVAL SCORE — TRAINING HISTORY</div>
    <div class="chart-wrap"><canvas id="scoreChart"></canvas></div>
  </div>
  <div class="card chart-card">
    <div class="section-title">CATEGORY BENCHMARK SCORES</div>
    <div class="chart-wrap"><canvas id="catChart"></canvas></div>
  </div>
</div>

<!-- Category trend -->
<div class="card chart-card" style="margin-bottom:24px">
  <div class="section-title">CATEGORY TRENDS OVER TIME</div>
  <div style="position:relative;height:220px"><canvas id="trendChart"></canvas></div>
</div>

<!-- Category table + Run history -->
<div class="grid-2">
  <div class="card">
    <div class="section-title">BENCHMARK BY CATEGORY</div>
    <table>
      <thead><tr>
        <th>CATEGORY</th><th>SCORE</th><th>PASS/TOTAL</th><th>DELTA</th>
      </tr></thead>
      <tbody>{cat_rows_html}</tbody>
    </table>
  </div>
  <div class="card">
    <div class="section-title">OVERNIGHT RUN HISTORY</div>
    <table>
      <thead><tr>
        <th>DATE</th><th>SCORE</th><th>PASS/TOTAL</th><th>EXAMPLES</th><th>DURATION</th><th>PROMOTED</th>
      </tr></thead>
      <tbody>{run_rows_html}</tbody>
    </table>
  </div>
</div>

<div class="footer">
  JARVIS LOCAL FINE-TUNING &nbsp;·&nbsp; MLX APPLE SILICON &nbsp;·&nbsp;
  11PM–7AM TRAINING WINDOW &nbsp;·&nbsp; DATA: training/benchmarks.jsonl + training/overnight_log.jsonl
</div>

<script>
const DATA = {chart_json};

const baseOpts = {{
  responsive: true,
  maintainAspectRatio: false,
  plugins: {{ legend: {{ labels: {{ color: '#A8E6FF', font: {{ family: 'Courier New', size: 10 }} }} }} }},
  scales: {{
    x: {{ ticks: {{ color: '#4A8FA8', font: {{ family: 'Courier New', size: 9 }} }}, grid: {{ color: 'rgba(13,79,112,0.3)' }} }},
    y: {{ ticks: {{ color: '#4A8FA8', font: {{ family: 'Courier New', size: 9 }} }}, grid: {{ color: 'rgba(13,79,112,0.3)' }}, min: 0, max: 100 }},
  }},
}};

// Score history line chart
new Chart(document.getElementById('scoreChart'), {{
  type: 'line',
  data: {{
    labels: DATA.run_dates.length ? DATA.run_dates : ['No runs yet'],
    datasets: [{{
      label: 'Eval Score %',
      data: DATA.run_scores.length ? DATA.run_scores : [0],
      borderColor: '#00D4FF',
      backgroundColor: 'rgba(0,212,255,0.08)',
      fill: true,
      tension: 0.4,
      pointRadius: 5,
      pointBackgroundColor: '#00D4FF',
    }}]
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
      backgroundColor: DATA.cat_colors.map(c => c + '33'),
      borderColor: DATA.cat_colors,
      borderWidth: 2,
      borderRadius: 4,
    }}]
  }},
  options: {{
    ...baseOpts,
    plugins: {{ ...baseOpts.plugins, legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ color: '#4A8FA8', font: {{ family: 'Courier New', size: 9 }} }}, grid: {{ display: false }} }},
      y: {{ ticks: {{ color: '#4A8FA8', font: {{ family: 'Courier New', size: 9 }} }}, grid: {{ color: 'rgba(13,79,112,0.3)' }}, min: 0, max: 100 }},
    }},
  }},
}});

// Category trend lines
new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: DATA.bench_dates.length ? DATA.bench_dates : ['No data yet'],
    datasets: DATA.cat_trend_datasets.length ? DATA.cat_trend_datasets : [{{
      label: 'No benchmark data',
      data: [null],
      borderColor: '#4A8FA8',
    }}],
  }},
  options: baseOpts,
}});
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
