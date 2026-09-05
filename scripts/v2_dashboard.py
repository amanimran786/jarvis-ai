#!/usr/bin/env python3
"""Live read-only dashboard for the Jarvis V2 agentic pipeline.

Shows one request travelling end to end: user task -> model request -> tool call
-> tool result -> back into the model -> final answer, with the loop's cycling
made visible rather than flattened into a log.

Two fidelities are surfaced, and they are always labelled, never blended:

  checkpoint  Every run writes `<run_id>.json` (atomically replaced) and an
              append-only `<run_id>.events.jsonl`. The checkpoint accumulates,
              so the newest one holds the whole trace. But it only lands once
              per step, so the file is silent for the entire duration of a model
              request. Measured on this machine: a step took 12.6 s with 11.3 s
              of prefill before the first token. Checkpoint fidelity cannot see
              inside that window.

  trace       `scripts/v2_trace.py` decorates the injected model client and tool
              plane, bracketing each request and each tool dispatch. That yields
              sub-step visibility and true in-flight state.

The dashboard is strictly an observer. It never writes into `.jarvis-v2/`, never
takes a run lease, and never probes a `.lock` file, because a non-blocking flock
probe would race a starting run and could steal its lease.

    ./venv/bin/python scripts/v2_dashboard.py
    open http://127.0.0.1:7878
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_v2.config import LocalConfigurationError, LocalModelConfig

HEX32_RE = re.compile(r"[0-9a-f]{32}")
DEFAULT_PORT = 7878  # 8080 is the MLX server; 7842 and 8765 are retired V1 surfaces.
DEFAULT_STATE_ROOT = Path(".jarvis-v2")

# A run whose status is still "running" but whose checkpoint has not moved for
# longer than this is reported as "stalled", not "live". Generous, because a
# single step legitimately blocks for ~12 s of prefill on this hardware.
# Longer than the shipped 300-second agent wall-clock budget. A healthy request
# can legitimately remain silent for the full local-model timeout.
STALL_SECONDS = 360.0
POLL_SECONDS = 0.4

# Mirrors jarvis_v2.agent.AgentLimits defaults. Used only to render guard gauges.
GUARD_DEFAULTS = {
    "max_steps": 8,
    "max_seconds": 300.0,
    "max_consecutive_errors": 2,
    "max_repeated_call": 2,
    "max_total_tokens": 32_000,
}

BASE_SOURCES = (
    ("run", "runs"),
    ("worker", "team-runs/workers"),
    ("synthesis", "team-runs/synthesis"),
)
# Concurrency benchmarks write full AgentState trees under benchmarks/<level>/,
# and those hold most of the checkpoints on disk. Excluding them would make any
# "everything on disk" total quietly wrong.
BENCH_ROLES = ("workers", "synthesis")
KIND_RE = re.compile(r"[A-Za-z0-9_:-]{1,64}")


def validate_hex32(value: str) -> str:
    if not isinstance(value, str) or HEX32_RE.fullmatch(value) is None:
        raise ValueError("identifier must be exactly 32 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True)
class Store:
    """Read-only view over the V2 state tree."""

    root: Path

    def _dir(self, relative: str) -> Path:
        resolved_root = self.root.resolve(strict=False)
        candidate = (self.root / relative).resolve(strict=False)
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError("state source escapes dashboard root") from exc
        return candidate

    def _contained(self, base: Path, candidate: Path) -> Path:
        """Reject any path that escapes its directory, belt and braces.

        The hex32 check already makes traversal impossible; this survives a
        future loosening of the identifier format.
        """
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base.resolve(strict=False))
        return resolved

    def sources(self) -> list[tuple[str, str]]:
        """Every directory holding AgentState checkpoints, discovered fresh.

        The benchmark levels are found rather than hardcoded, so a later `c8`
        run shows up without a code change. A `kind` is only ever used as a key
        into this mapping, never spliced into a path.
        """
        found = list(BASE_SOURCES)
        benchmarks = self._dir("benchmarks")
        if benchmarks.is_dir():
            for level in sorted(p for p in benchmarks.iterdir() if p.is_dir()):
                if KIND_RE.fullmatch(level.name) is None:
                    continue
                for role in BENCH_ROLES:
                    if (level / role).is_dir():
                        found.append((f"bench-{level.name}-{role[:6]}", f"benchmarks/{level.name}/{role}"))
        return found

    def checkpoint_path(self, kind: str, run_id: str) -> Path:
        validate_hex32(run_id)
        relative = dict(self.sources()).get(kind)
        if relative is None:
            raise ValueError("unknown run kind")
        base = self._dir(relative)
        return self._contained(base, base / f"{run_id}.json")

    def events_path(self, kind: str, run_id: str) -> Path:
        return self.checkpoint_path(kind, run_id).with_suffix(".events.jsonl")

    def trace_path(self, trace_id: str) -> Path:
        validate_hex32(trace_id)
        base = self._dir("traces")
        return self._contained(base, base / f"{trace_id}.jsonl")

    # ---- discovery -----------------------------------------------------

    def list_runs(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for kind, relative in self.sources():
            directory = self._dir(relative)
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                if HEX32_RE.fullmatch(path.stem) is None:
                    continue
                summary = self._summarize(kind, path)
                if summary is not None:
                    found.append(summary)
        found.sort(key=lambda item: item["modified_at"], reverse=True)
        return found

    def _summarize(self, kind: str, path: Path) -> dict[str, Any] | None:
        try:
            stat = path.stat()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status") or "unknown")
        age = time.time() - stat.st_mtime
        return {
            "kind": kind,
            "run_id": path.stem,
            "task": str(payload.get("task") or ""),
            "status": status,
            "liveness": self._liveness(status, age),
            "age_seconds": age,
            "step": payload.get("step", 0),
            "tool_calls_completed": payload.get("tool_calls_completed", 0),
            "prompt_tokens": payload.get("prompt_tokens", 0),
            "completion_tokens": payload.get("completion_tokens", 0),
            "modified_at": stat.st_mtime,
            "has_timings": bool(payload.get("model_timings")),
        }

    @staticmethod
    def _liveness(status: str, age: float) -> str:
        """Infer liveness read-only.

        `status` is whatever the last checkpoint recorded. A crashed process
        leaves "running" on disk forever, so freshness is what separates a live
        run from an abandoned one.
        """
        if status != "running":
            return "finished"
        return "live" if age <= STALL_SECONDS else "stalled"

    def list_traces(self) -> list[dict[str, Any]]:
        directory = self._dir("traces")
        if not directory.is_dir():
            return []
        traces = []
        for path in directory.glob("*.jsonl"):
            if HEX32_RE.fullmatch(path.stem) is None:
                continue
            try:
                stat = path.stat()
                records = self._read_jsonl(path)
            except OSError:
                continue
            if not records:
                continue
            started = next((r for r in records if r.get("kind") == "run_started"), {})
            finished = next((r for r in records if r.get("kind") == "run_finished"), None)
            age = time.time() - stat.st_mtime
            done = [r for r in records if r.get("kind") == "model_request_finished"]
            traces.append(
                {
                    "trace_id": path.stem,
                    "mode": started.get("mode", "unknown"),
                    "task": started.get("task", ""),
                    "record_count": len(records),
                    "actors": len({r.get("actor") for r in done}),
                    "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in done),
                    "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in done),
                    "status": (finished or {}).get("status", "running"),
                    "liveness": "finished" if finished else ("live" if age <= STALL_SECONDS else "stalled"),
                    "age_seconds": age,
                    "modified_at": stat.st_mtime,
                    "run_id": (finished or {}).get("run_id", ""),
                }
            )
        traces.sort(key=lambda item: item["modified_at"], reverse=True)
        return traces

    # ---- detail --------------------------------------------------------

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        records = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # A reader can legitimately catch a partially flushed
                        # final line. Skip it; the next poll will pick it up.
                        continue
                    if isinstance(item, dict):
                        records.append(item)
        except OSError:
            return []
        return records

    def load_run(self, kind: str, run_id: str, *, include_messages: bool) -> dict[str, Any]:
        path = self.checkpoint_path(kind, run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        stat = path.stat()
        events = self._read_jsonl(self.events_path(kind, run_id))
        messages = payload.get("messages") or []
        status = str(payload.get("status") or "unknown")
        detail = {
            "kind": kind,
            "run_id": run_id,
            "status": status,
            "liveness": self._liveness(status, time.time() - stat.st_mtime),
            "task": payload.get("task", ""),
            "step": payload.get("step", 0),
            "reason": payload.get("reason", ""),
            "final_answer": payload.get("final_answer", ""),
            "prompt_tokens": payload.get("prompt_tokens", 0),
            "completion_tokens": payload.get("completion_tokens", 0),
            "tool_calls_completed": payload.get("tool_calls_completed", 0),
            "consecutive_errors": payload.get("consecutive_errors", 0),
            "repeated_call_count": payload.get("repeated_call_count", 0),
            "model_timings": payload.get("model_timings") or [],
            "tool_evidence": payload.get("tool_evidence") or [],
            "events": events,
            "message_count": len(messages),
            "guards": payload.get("limits") or GUARD_DEFAULTS,
            "guards_source": (
                "checkpoint" if payload.get("limits") else "legacy_assumed_defaults"
            ),
            "modified_at": stat.st_mtime,
        }
        # The conversation contains full prompts and raw model output. It is the
        # most sensitive thing on disk, so it is opt-in per request rather than
        # shipped with every poll.
        detail["messages"] = messages if include_messages else []
        return detail

    def load_trace(self, trace_id: str) -> dict[str, Any]:
        records = self._read_jsonl(self.trace_path(trace_id))
        origin = next((r for r in records if r.get("kind") == "trace_started"), {})
        return {
            "trace_id": trace_id,
            "records": records,
            "monotonic_origin": origin.get("monotonic_origin"),
            "wall_clock_epoch": origin.get("wall_clock_epoch"),
        }

    # ---- change detection ----------------------------------------------

    def fingerprint(self) -> str:
        """Cheap digest of every watched file's (size, mtime).

        Detects both the append to a JSONL and the atomic replace of a
        checkpoint, without reading any file contents.
        """
        parts: list[str] = []
        for _, relative in self.sources():
            directory = self._dir(relative)
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix not in {".json", ".jsonl"}:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        traces = self._dir("traces")
        if traces.is_dir():
            for path in sorted(traces.glob("*.jsonl")):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                parts.append(f"t/{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
        return str(hash("|".join(parts)))


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_model_server(config: LocalModelConfig, timeout: float = 1.5) -> dict[str, Any]:
    """Ask the MLX server whether it is up. Loopback only, no proxy, no redirects."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
    )
    try:
        with opener.open(config.models_url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ids = [str(item.get("id", "")) for item in payload.get("data", [])]
        return {
            "up": config.model in ids,
            "model_ids": ids,
            "expected_model": config.model,
        }
    except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return {"up": False, "error": str(exc), "model_ids": []}


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisV2Dashboard/1.0"
    store: Store
    model_config: LocalModelConfig
    allowed_hosts: set[str]
    capability: str

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # keep the console clean; this is a viewer, not a service

    # ---- helpers -------------------------------------------------------

    def _host_ok(self) -> bool:
        """Reject DNS-rebinding: only literal loopback Host headers are served."""
        host = (self.headers.get("Host") or "").strip()
        return host in self.allowed_hosts

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        supplied = query.get("capability", [])
        return len(supplied) == 1 and hmac.compare_digest(
            supplied[0], self.capability
        )

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        # The page loads no third-party anything; say so explicitly.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(raw)

    # ---- routes --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send_json({"error": "host not allowed"}, status=403)
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == f"/{self.capability}":
                self._send_html(PAGE.replace("__CAPABILITY_TOKEN__", self.capability))
            elif path.startswith("/api/") and not self._authorized(query):
                self._send_json({"error": "capability required"}, status=403)
            elif path == "/api/overview":
                self._send_json(
                    {
                        "runs": self.store.list_runs(),
                        "traces": self.store.list_traces(),
                        "model_server": probe_model_server(self.model_config),
                        "endpoint": self.model_config.base_url,
                        "root": str(self.store.root),
                        "server_time": time.time(),
                    }
                )
            elif path.startswith("/api/run/"):
                _, _, _, kind, run_id = path.split("/", 4)
                include = query.get("messages") == ["1"]
                self._send_json(self.store.load_run(kind, run_id, include_messages=include))
            elif path.startswith("/api/trace/"):
                trace_id = path.rsplit("/", 1)[-1]
                self._send_json(self.store.load_trace(trace_id))
            elif path == "/api/stream":
                self._stream()
            else:
                self._send_json({"error": "not found"}, status=404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except FileNotFoundError:
            self._send_json({"error": "run not found"}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except OSError as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _stream(self) -> None:
        """Server-sent events: push a tick whenever any watched file changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        previous = None
        last_beat = 0.0
        try:
            while True:
                current = self.store.fingerprint()
                now = time.monotonic()
                if current != previous:
                    previous = current
                    self.wfile.write(b"event: changed\ndata: {}\n\n")
                    self.wfile.flush()
                    last_beat = now
                elif now - last_beat > 15:
                    # Comment frame keeps the connection from being reaped.
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_beat = now
                time.sleep(POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis V2 Pipeline</title>
<style>
  :root{
    --bg:#0b0d13; --panel:#12151f; --panel2:#171b28; --line:#242a3a;
    --slate:#2e313e; --slate2:#353845; --slate3:#474b5e; --muted:#767a8e;
    --text:#e6e8f0; --dim:#9aa0b4;
    --live:#3ddc97; --warn:#f0b429; --bad:#f0605d; --model:#5b8def; --tool:#c084fc;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"SF Pro Text","Helvetica Neue",Arial,sans-serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:13px/1.5 var(--sans);
       -webkit-font-smoothing:antialiased}
  a{color:var(--model);text-decoration:none}
  .app{display:grid;grid-template-columns:290px 1fr;height:100vh}
  /* ---- top bar ---- */
  header{grid-column:1/-1;display:flex;align-items:center;gap:14px;padding:0 16px;height:52px;
         background:linear-gradient(180deg,#141824,#0f121b);border-bottom:1px solid var(--line)}
  .brand{font-weight:640;letter-spacing:.14em;font-size:11px;color:var(--dim)}
  .brand b{color:var(--text);letter-spacing:.06em;font-size:13px;display:block;letter-spacing:.1em}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;
        background:var(--panel2);border:1px solid var(--line);font:11px/1.4 var(--mono);color:var(--dim)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--muted);flex:none}
  .dot.live{background:var(--live);box-shadow:0 0 0 0 rgba(61,220,151,.7);animation:pulse 1.8s infinite}
  .dot.bad{background:var(--bad)} .dot.warn{background:var(--warn)}
  @keyframes pulse{70%{box-shadow:0 0 0 7px rgba(61,220,151,0)}100%{box-shadow:0 0 0 0 rgba(61,220,151,0)}}
  .spacer{flex:1}
  /* ---- rail ---- */
  .rail{background:var(--panel);border-right:1px solid var(--line);overflow-y:auto}
  .railhead{padding:11px 14px 7px;font:10px/1 var(--mono);letter-spacing:.13em;color:var(--muted);
            text-transform:uppercase;position:sticky;top:0;background:var(--panel);z-index:2}
  .run{padding:9px 14px;border-bottom:1px solid #1a1e2b;cursor:pointer;display:block;width:100%;
       text-align:left;background:none;border-left:2px solid transparent;color:inherit;font:inherit}
  .run:hover{background:var(--panel2)}
  .run.sel{background:#1a1f2e;border-left-color:var(--model)}
  .run .top{display:flex;align-items:center;gap:7px;margin-bottom:3px}
  .run .task{color:var(--dim);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;
             display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .kindtag{font:9px/1 var(--mono);padding:2px 5px;border-radius:3px;background:var(--slate);
           color:#b9bed0;letter-spacing:.06em;text-transform:uppercase}
  .kindtag.trace{background:#3a2a52;color:#d9bcff}
  .meta{font:10px/1 var(--mono);color:var(--muted);margin-top:4px}
  /* ---- main ---- */
  main{overflow-y:auto;padding:18px 22px 60px}
  .empty{color:var(--muted);padding:60px 0;text-align:center}
  h2{font-size:12px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);
     margin:26px 0 11px;font-weight:600;display:flex;align-items:center;gap:9px}
  h2 .hint{text-transform:none;letter-spacing:0;font-size:11px;color:#5d6377;font-weight:400}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:14px 16px}
  .strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(112px,1fr));gap:1px;
         background:var(--line);border:1px solid var(--line);border-radius:9px;overflow:hidden}
  .stat{background:var(--panel);padding:11px 13px}
  .stat .k{font:9.5px/1 var(--mono);letter-spacing:.1em;color:var(--muted);text-transform:uppercase}
  .stat .v{font:17px/1.35 var(--mono);color:var(--text);margin-top:5px}
  .stat .v small{font-size:11px;color:var(--muted)}
  .taskline{background:var(--panel2);border:1px solid var(--line);border-left:2px solid var(--slate3);
            border-radius:7px;padding:11px 13px;margin-bottom:4px}
  .taskline .lbl{font:9.5px/1 var(--mono);letter-spacing:.1em;color:var(--muted);text-transform:uppercase;
                 margin-bottom:6px}
  /* ---- journey ---- */
  .cycle{position:relative;padding-left:26px}
  .cycle:before{content:"";position:absolute;left:7px;top:6px;bottom:6px;width:2px;
                background:linear-gradient(180deg,var(--slate3),var(--slate));border-radius:2px}
  .node{position:relative;margin-bottom:12px}
  .node:before{content:"";position:absolute;left:-23px;top:14px;width:10px;height:10px;border-radius:50%;
               background:var(--slate2);border:2px solid var(--bg);box-shadow:0 0 0 1px var(--slate3)}
  .node.model:before{background:var(--model);box-shadow:0 0 0 1px var(--model)}
  .node.tool:before{background:var(--tool);box-shadow:0 0 0 1px var(--tool)}
  .node.out:before{background:var(--live);box-shadow:0 0 0 1px var(--live)}
  .node.inflight:before{background:var(--warn);animation:pulse 1.4s infinite}
  .nodecard{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
  .node.inflight .nodecard{border-color:#4a3d1c;background:#16150f}
  .nodehead{display:flex;align-items:center;gap:9px;margin-bottom:9px}
  .nodetitle{font:11.5px/1 var(--mono);letter-spacing:.05em;color:var(--text)}
  .nodetitle b{color:var(--model)} .node.tool .nodetitle b{color:var(--tool)}
  .ms{font:10.5px/1 var(--mono);color:var(--muted);margin-left:auto}
  /* segmented request bar */
  .segbar{display:flex;height:22px;border-radius:5px;overflow:hidden;background:#0e1119;
          border:1px solid var(--line)}
  .seg{display:flex;align-items:center;justify-content:center;font:9.5px/1 var(--mono);
       color:#0b0d13;font-weight:700;min-width:0;overflow:hidden;white-space:nowrap}
  .seg.prefill{background:linear-gradient(90deg,#2c4a7a,#3f6db3);color:#cfe0ff}
  .seg.gen{background:linear-gradient(90deg,#3ddc97,#2bb87c);color:#062a1c}
  .seg.flush{background:var(--slate3);color:#cbd0e0}
  .seg.pending{background:repeating-linear-gradient(45deg,#3a3320,#3a3320 7px,#4a4028 7px,#4a4028 14px);
               color:#f0d9a0;flex:1;animation:shift 1s linear infinite}
  @keyframes shift{to{background-position:20px 0}}
  .seglegend{display:flex;gap:13px;margin-top:7px;font:9.5px/1 var(--mono);color:var(--muted);flex-wrap:wrap}
  .seglegend i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:4px;vertical-align:-1px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font:10.5px/1.6 var(--mono);
      color:var(--dim);margin-top:9px}
  .kv .k{color:var(--muted)}
  .pre{font:10.5px/1.55 var(--mono);color:#c3c8d8;background:#0e1119;border:1px solid var(--line);
       border-radius:6px;padding:9px 11px;margin-top:9px;white-space:pre-wrap;word-break:break-word;
       max-height:200px;overflow:auto}
  .retedge{font:10px/1 var(--mono);color:var(--muted);margin:-4px 0 12px 2px;display:flex;
           align-items:center;gap:6px}
  .retedge:before{content:"↻";color:var(--tool);font-size:13px}
  /* ---- waterfall ---- */
  .wf{position:relative;overflow-x:auto}
  .wfrow{display:grid;grid-template-columns:104px 1fr;gap:11px;align-items:center;margin-bottom:6px}
  .wflabel{font:10.5px/1 var(--mono);color:var(--dim);text-align:right;overflow:hidden;
           text-overflow:ellipsis;white-space:nowrap}
  .wftrack{position:relative;height:19px;background:#0e1119;border:1px solid var(--line);border-radius:4px}
  .wfbar{position:absolute;top:0;bottom:0;border-radius:3px;display:flex;overflow:hidden}
  .wfbar .p{background:#3f6db3;height:100%} .wfbar .g{background:#3ddc97;height:100%}
  .wfbar.toolbar{background:var(--tool)}
  .wfaxis{display:grid;grid-template-columns:104px 1fr;gap:11px;margin-top:7px}
  .wfticks{position:relative;height:14px;font:9.5px/1 var(--mono);color:var(--muted)}
  .wfticks span{position:absolute;transform:translateX(-50%)}
  /* ---- guards ---- */
  .guards{display:grid;grid-template-columns:repeat(auto-fit,minmax(178px,1fr));gap:10px}
  .guard{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .guard .gk{font:9.5px/1 var(--mono);letter-spacing:.09em;color:var(--muted);text-transform:uppercase}
  .guard .gv{font:13px/1 var(--mono);margin:7px 0 8px;color:var(--text)}
  .bar{height:5px;background:#0e1119;border-radius:3px;overflow:hidden}
  .bar i{display:block;height:100%;background:var(--live);border-radius:3px;transition:width .35s}
  .bar i.warn{background:var(--warn)} .bar i.bad{background:var(--bad)}
  /* ---- token bars ---- */
  .brow{display:grid;grid-template-columns:210px 1fr 118px;gap:11px;align-items:center;
        padding:5px 0;cursor:default}
  .card .brow:hover{background:#161a26}
  .blab{font:10.5px/1.5 var(--mono);color:var(--dim);overflow:hidden;text-overflow:ellipsis;
        white-space:nowrap}
  .btrack{display:flex;height:15px;background:#0e1119;border:1px solid var(--line);
          border-radius:4px;overflow:hidden}
  .bseg{height:100%} .bseg.p{background:#3f6db3} .bseg.c{background:#3ddc97}
  .bval{font:10.5px/1.5 var(--mono);color:var(--text);text-align:right;white-space:pre}
  .gk{font:9.5px/1 var(--mono);letter-spacing:.09em;color:var(--muted);text-transform:uppercase;
      margin-bottom:8px}
  /* ---- events ---- */
  table{width:100%;border-collapse:collapse;font:10.5px/1.6 var(--mono)}
  th{text-align:left;color:var(--muted);font-weight:500;padding:5px 9px;border-bottom:1px solid var(--line);
     letter-spacing:.06em;text-transform:uppercase;font-size:9.5px}
  td{padding:5px 9px;border-bottom:1px solid #171b26;color:var(--dim);vertical-align:top}
  td.hash{color:#5d6377;word-break:break-all}
  .badge{display:inline-block;padding:2px 7px;border-radius:4px;font:9.5px/1.5 var(--mono);
         background:var(--slate);color:#c7ccdd}
  .badge.ok{background:#12331f;color:var(--live)} .badge.bad{background:#3a1a1c;color:var(--bad)}
  .badge.warn{background:#3a2f13;color:var(--warn)}
  .fid{font:9.5px/1 var(--mono);letter-spacing:.06em;padding:2px 6px;border-radius:3px}
  .fid.trace{background:#2a1f3d;color:#d0b3ff;border:1px solid #3d2d59}
  .fid.ckpt{background:#1b2436;color:#8fb0e8;border:1px solid #27354d}
  .note{color:var(--muted);font-size:11px;margin:9px 0 0;line-height:1.6}
  .toggle{background:var(--panel2);border:1px solid var(--line);color:var(--dim);border-radius:6px;
          padding:5px 11px;font:10.5px/1 var(--mono);cursor:pointer}
  .toggle:hover{color:var(--text);border-color:var(--slate3)}
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="brand">JARVIS<b>V2 PIPELINE</b></div>
    <span class="chip" id="mlx"><span class="dot"></span>model server</span>
    <span class="chip" id="rootchip">.jarvis-v2</span>
    <div class="spacer"></div>
    <span class="chip" id="conn"><span class="dot"></span>connecting</span>
  </header>
  <nav class="rail">
    <div class="railhead">Traces <span style="color:#5d6377">sub-step</span></div>
    <div id="tracelist"></div>
    <div class="railhead">Runs <span style="color:#5d6377">per-step</span></div>
    <div id="runlist"></div>
  </nav>
  <main id="main"><div class="empty">Select a run or trace.</div></main>
</div>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => { const n = document.createElement(tag);
  if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const fx = (n, d) => (n === null || n === undefined || isNaN(n)) ? "-" : Number(n).toFixed(d === undefined ? 2 : d);
const num = (n) => (n === null || n === undefined) ? "-" : Number(n).toLocaleString();
const capability = "__CAPABILITY_TOKEN__";
const apiURL = (path) => { const u = new URL(path, window.location.origin);
  u.searchParams.set("capability", capability); return u.pathname + u.search; };

let selected = null;        // {type:'run'|'trace', kind, id}
let overview = null;
let showMessages = false;

/* ---------- data ---------- */
async function getJSON(url){ const r = await fetch(apiURL(url)); if(!r.ok) throw new Error((await r.json()).error||r.status); return r.json(); }

async function refresh(){
  overview = await getJSON("/api/overview");
  renderRail();
  $("rootchip").textContent = overview.root;
  const m = $("mlx"), up = overview.model_server.up;
  m.innerHTML = "";
  m.appendChild(el("span", "dot " + (up ? "live" : "bad")));
  m.appendChild(document.createTextNode(up ? "MLX 8080 up" : "MLX 8080 down"));
  if (selected) await renderDetail(); else renderFleet();
}

/* ---------- fleet: token burn across everything on disk ---------- */
function renderFleet(){
  const main = $("main"); main.innerHTML = "";
  const runs = overview.runs;
  main.appendChild(h2("Token burn", "every run on disk, grouped by role"));
  if(!runs.length){ main.appendChild(el("div","empty","No runs recorded yet.")); return; }

  const pin = runs.reduce((a,r)=>a+r.prompt_tokens,0);
  const pout = runs.reduce((a,r)=>a+r.completion_tokens,0);
  main.appendChild(statStrip([
    ["Runs", runs.length],
    ["Tokens burned", num(pin+pout)],
    ["Prompt in", num(pin), (pin+pout?Math.round(pin/(pin+pout)*100):0)+"%"],
    ["Completion out", num(pout), (pin+pout?Math.round(pout/(pin+pout)*100):0)+"%"],
    ["Avg / run", num(Math.round((pin+pout)/runs.length))],
    ["Traces", overview.traces.length],
  ]));

  const byKind = {};
  runs.forEach(r => { const k = byKind[r.kind] = byKind[r.kind] || {n:0,p:0,c:0,tools:0,steps:0};
    k.n++; k.p+=r.prompt_tokens; k.c+=r.completion_tokens; k.tools+=r.tool_calls_completed; k.steps+=r.step; });

  main.appendChild(h2("Per role", "run = solo agent, worker = team member, synthesis = the reducer"));
  const kc = el("div","card"); const kt = el("table");
  const kh = el("tr"); ["role","runs","steps","tools","prompt in","completion out","total","avg / run"]
    .forEach(x => kh.appendChild(el("th",null,x))); kt.appendChild(kh);
  Object.entries(byKind).sort((a,b)=>(b[1].p+b[1].c)-(a[1].p+a[1].c)).forEach(([k,v]) => {
    const tr = el("tr");
    const c0 = el("td"); c0.appendChild(el("span","kindtag",k)); tr.appendChild(c0);
    [v.n, v.steps, v.tools, num(v.p), num(v.c), num(v.p+v.c), num(Math.round((v.p+v.c)/v.n))]
      .forEach(x => tr.appendChild(el("td",null,String(x))));
    kt.appendChild(tr);
  });
  kc.appendChild(kt); main.appendChild(kc);

  main.appendChild(h2("Heaviest requests", "top 12 by tokens burned"));
  const max = Math.max(...runs.map(r => r.prompt_tokens + r.completion_tokens), 1);
  const lc = el("div","card");
  runs.slice().sort((a,b)=>(b.prompt_tokens+b.completion_tokens)-(a.prompt_tokens+a.completion_tokens))
      .slice(0,12).forEach(r => {
    const tot = r.prompt_tokens + r.completion_tokens;
    const row = el("div","brow");
    const lab = el("div","blab"); lab.appendChild(el("span","kindtag",r.kind));
    lab.appendChild(document.createTextNode(" "+(r.task||"(no task)").slice(0,58)));
    lab.title = r.task || "";
    row.appendChild(lab);
    const track = el("div","btrack");
    const p = el("div","bseg p"); p.style.width = (r.prompt_tokens/max*100)+"%";
    p.title = num(r.prompt_tokens)+" prompt tokens";
    const c = el("div","bseg c"); c.style.width = (r.completion_tokens/max*100)+"%";
    c.title = num(r.completion_tokens)+" completion tokens";
    track.appendChild(p); track.appendChild(c); row.appendChild(track);
    row.appendChild(el("div","bval", num(tot)));
    row.onclick = () => { selected={type:"run",kind:r.kind,id:r.run_id}; showMessages=false;
                          renderRail(); renderDetail(); };
    lc.appendChild(row);
  });
  const lg = el("div","seglegend");
  [["#3f6db3","prompt in"],["#3ddc97","completion out"]].forEach(([c,t]) => {
    const s = el("span"); const i = el("i"); i.style.background=c;
    s.appendChild(i); s.appendChild(document.createTextNode(t)); lg.appendChild(s); });
  lc.appendChild(lg);
  main.appendChild(lc);
  main.appendChild(el("p","note",
    "Prompt tokens dominate because the agent loop re-sends the whole conversation on every step. "+
    "Open a trace to see the per-request split and the re-send factor."));
}

/* ---------- rail ---------- */
function renderRail(){
  const tl = $("tracelist"); tl.innerHTML = "";
  if(!overview.traces.length){
    const n = el("div","meta","no traces yet - run scripts/v2_trace.py");
    n.style.padding = "4px 14px 12px"; tl.appendChild(n);
  }
  overview.traces.forEach(t => tl.appendChild(railItem({
    type:"trace", id:t.trace_id, kind:t.mode, task:t.task,
    status:t.status, liveness:t.liveness,
    meta:`${t.mode} · ${t.actors||1} agent${t.actors===1?"":"s"} · ${num(t.prompt_tokens+t.completion_tokens)} tok`
  })));
  const rl = $("runlist"); rl.innerHTML = "";
  overview.runs.forEach(r => rl.appendChild(railItem({
    type:"run", id:r.run_id, kind:r.kind, task:r.task,
    status:r.status, liveness:r.liveness,
    meta:`step ${r.step} · ${r.tool_calls_completed} tools · ${num(r.prompt_tokens+r.completion_tokens)} tok`
  })));
}

function railItem(o){
  const b = el("button","run");
  if(selected && selected.id===o.id) b.classList.add("sel");
  const top = el("div","top");
  top.appendChild(el("span","dot "+(o.liveness==="live"?"live":o.liveness==="stalled"?"warn":
                     o.status==="completed"?"":"bad")));
  top.appendChild(el("span","kindtag"+(o.type==="trace"?" trace":""), o.kind));
  top.appendChild(el("span","meta", o.id.slice(0,8)));
  b.appendChild(top);
  b.appendChild(el("div","task", o.task || "(no task recorded)"));
  b.appendChild(el("div","meta", o.meta));
  b.onclick = () => { selected = {type:o.type, kind:o.kind, id:o.id}; showMessages=false;
                      renderRail(); renderDetail(); };
  return b;
}

/* ---------- detail ---------- */
async function renderDetail(){
  const main = $("main");
  try {
    if(selected.type === "trace"){ renderTrace(await getJSON("/api/trace/"+selected.id)); }
    else {
      const q = showMessages ? "?messages=1" : "";
      renderRun(await getJSON("/api/run/"+selected.kind+"/"+selected.id+q));
    }
  } catch(e){ main.innerHTML=""; main.appendChild(el("div","empty","Could not load: "+e.message)); }
}

function statStrip(pairs){
  const s = el("div","strip");
  pairs.forEach(([k,v,sub]) => { const c = el("div","stat");
    c.appendChild(el("div","k",k)); const val = el("div","v",v);
    if(sub){ const sm = el("small"); sm.textContent = " "+sub; val.appendChild(sm); }
    c.appendChild(val); s.appendChild(c); });
  return s;
}

function h2(text, hint){ const h = el("h2",null,text);
  if(hint) h.appendChild(el("span","hint",hint)); return h; }

/* ---- checkpoint-fidelity run view ---- */
function renderRun(d){
  const main = $("main"); main.innerHTML = "";
  const head = el("div"); head.style.cssText="display:flex;align-items:center;gap:10px;margin-bottom:12px";
  head.appendChild(el("span","fid ckpt","CHECKPOINT FIDELITY · one sample per step"));
  head.appendChild(el("span","badge "+(d.status==="completed"?"ok":d.status==="running"?"warn":"bad"), d.status));
  if(d.liveness==="stalled") head.appendChild(el("span","badge warn","stalled · checkpoint not moving"));
  if(d.liveness==="live") head.appendChild(el("span","badge ok","live"));
  main.appendChild(head);

  const t = el("div","taskline"); t.appendChild(el("div","lbl","User task"));
  t.appendChild(el("div",null,d.task||"(none)")); main.appendChild(t);

  const tim = d.model_timings, tot = d.prompt_tokens + d.completion_tokens;
  const wall = tim.length ? (tim[tim.length-1].completed_at - tim[0].request_started_at) : 0;
  const gen = tim.reduce((a,x)=> a + ((x.first_delta_at&&x.terminal_at)? x.terminal_at-x.first_delta_at : 0), 0);
  main.appendChild(statStrip([
    ["Steps", d.step, "/ "+d.guards.max_steps],
    ["Model calls", tim.length],
    ["Tool calls", d.tool_calls_completed],
    ["Tokens", num(tot), "/ "+num(d.guards.max_total_tokens)],
    ["Run span", fx(wall)+"s"],
    ["Decode", gen>0 ? fx(d.completion_tokens/gen,1)+" tok/s" : "-"],
  ]));

  main.appendChild(h2("Token burn", "run totals; the per-step split is not written to the checkpoint"));
  const tk = el("div","card");
  const brow = el("div","brow");
  brow.appendChild(el("div","blab", d.kind+" · "+d.run_id.slice(0,8)));
  const track = el("div","btrack");
  const mx = Math.max(tot, 1);
  const pb = el("div","bseg p"); pb.style.width=(d.prompt_tokens/mx*100)+"%";
  pb.title = num(d.prompt_tokens)+" prompt tokens";
  const cb = el("div","bseg c"); cb.style.width=(d.completion_tokens/mx*100)+"%";
  cb.title = num(d.completion_tokens)+" completion tokens";
  track.appendChild(pb); track.appendChild(cb); brow.appendChild(track);
  brow.appendChild(el("div","bval", num(tot)));
  tk.appendChild(brow);
  const tl = el("div","seglegend");
  [["#3f6db3","prompt in "+num(d.prompt_tokens)],["#3ddc97","completion out "+num(d.completion_tokens)]]
    .forEach(([col,txt]) => { const s=el("span"); const i=el("i"); i.style.background=col;
      s.appendChild(i); s.appendChild(document.createTextNode(txt)); tl.appendChild(s); });
  tk.appendChild(tl);
  if(d.step > 0){
    tk.appendChild(el("div","note",
      "Averages "+num(Math.round(tot/d.step))+" tokens per step across "+d.step+" step"+
      (d.step===1?"":"s")+". The checkpoint stores only the running sum: `state.prompt_tokens += "+
      "turn.prompt_tokens` at jarvis_v2/agent.py:273. Run this task through scripts/v2_trace.py "+
      "for the exact per-request breakdown."));
  }
  main.appendChild(tk);

  main.appendChild(h2("Request journey", "user → model → tool → model → output"));
  main.appendChild(journeyFromCheckpoint(d));

  if(tim.length){
    main.appendChild(h2("Timeline", "normalized to the first request; monotonic clock"));
    main.appendChild(waterfallFromCheckpoint(d));
  }

  main.appendChild(h2("Termination guards", d.guards_source === "checkpoint"
    ? "recorded by this run" : "legacy checkpoint · assumed shipped defaults"));
  main.appendChild(guardPanel(d, wall));

  main.appendChild(h2("Checkpoint chain", d.events.length + " atomic transitions"));
  main.appendChild(eventTable(d.events));

  const bar = el("div"); bar.style.cssText="margin-top:22px;display:flex;gap:9px;align-items:center";
  const btn = el("button","toggle", showMessages ? "Hide conversation" : "Show conversation ("+d.message_count+" messages)");
  btn.onclick = () => { showMessages = !showMessages; renderDetail(); };
  bar.appendChild(btn);
  bar.appendChild(el("span","note","Contains full prompts and raw model output, so it is fetched only on request."));
  main.appendChild(bar);
  if(showMessages && d.messages.length){
    const c = el("div","card"); c.style.marginTop="11px";
    d.messages.forEach(m => { const r = el("div"); r.style.marginBottom="11px";
      r.appendChild(el("span","badge", m.role));
      r.appendChild(el("div","pre", typeof m.content==="string" ? m.content : JSON.stringify(m.content,null,1)));
      c.appendChild(r); });
    main.appendChild(c);
  }
}

/* Build the cycling loop from checkpoint data: model call N, then any tool call
   recorded at that step, then the return edge into the next model call. */
function journeyFromCheckpoint(d){
  const wrap = el("div","cycle");
  const toolsByStep = {};
  d.tool_evidence.forEach(t => { (toolsByStep[t.step] = toolsByStep[t.step] || []).push(t); });

  const start = el("div","node"); const sc = el("div","nodecard");
  const sh = el("div","nodehead"); sh.appendChild(el("span","nodetitle","USER TASK"));
  sc.appendChild(sh); sc.appendChild(el("div","pre", d.task||"(none)"));
  start.appendChild(sc); wrap.appendChild(start);

  d.model_timings.forEach((t, i) => {
    wrap.appendChild(modelNode(t, i, d.model_timings));
    const tools = toolsByStep[t.step] || [];
    const resends = d.model_timings.length - 1 - i;
    tools.forEach(tool => wrap.appendChild(toolNode(tool, resends)));
    if(tools.length && i < d.model_timings.length-1)
      wrap.appendChild(el("div","retedge","tool result appended to conversation, loop continues"));
  });

  if(d.final_answer){
    const n = el("div","node out"); const c = el("div","nodecard");
    const h = el("div","nodehead"); h.appendChild(el("span","nodetitle","FINAL ANSWER"));
    h.appendChild(el("span","ms", d.reason||""));
    c.appendChild(h); c.appendChild(el("div","pre", d.final_answer));
    n.appendChild(c); wrap.appendChild(n);
  }
  return wrap;
}

function modelNode(t, i){
  const n = el("div","node model"); const c = el("div","nodecard");
  const h = el("div","nodehead");
  const title = el("span","nodetitle"); title.appendChild(el("b","","MODEL REQUEST "));
  title.appendChild(document.createTextNode("· step "+t.step));
  h.appendChild(title);
  const total = t.completed_at - t.request_started_at;
  h.appendChild(el("span","ms", fx(total)+"s"));
  c.appendChild(h);
  c.appendChild(segbar(t));
  c.appendChild(segLegend(t));
  n.appendChild(c); return n;
}

/* The three phases of one streamed request. Prefill dominates on this hardware,
   which is invisible in any per-step view. */
function segbar(t){
  const bar = el("div","segbar");
  const total = Math.max(t.completed_at - t.request_started_at, 1e-6);
  const pre = (t.first_delta_at ? t.first_delta_at - t.request_started_at : total);
  const gen = (t.first_delta_at && t.terminal_at) ? t.terminal_at - t.first_delta_at : 0;
  const fl  = Math.max(t.completed_at - (t.terminal_at || t.completed_at), 0);
  [["prefill",pre],["gen",gen],["flush",fl]].forEach(([cls,v]) => {
    if(v <= 0) return;
    const s = el("div","seg "+cls); s.style.width = (v/total*100)+"%";
    if(v/total > 0.13) s.textContent = fx(v)+"s";
    s.title = cls+" "+fx(v,3)+"s";
    bar.appendChild(s);
  });
  return bar;
}
function segLegend(t){
  const pre = t.first_delta_at ? t.first_delta_at - t.request_started_at : null;
  const gen = (t.first_delta_at && t.terminal_at) ? t.terminal_at - t.first_delta_at : null;
  const l = el("div","seglegend");
  const add = (color,label) => { const s=el("span"); const i=el("i");
    i.style.background=color; s.appendChild(i); s.appendChild(document.createTextNode(label)); l.appendChild(s); };
  add("#3f6db3","prefill / queue "+fx(pre)+"s");
  add("#3ddc97","generation "+fx(gen)+"s");
  add("#474b5e","flush "+fx(Math.max(t.completed_at-(t.terminal_at||t.completed_at),0),3)+"s");
  return l;
}

function toolNode(t, resends){
  const n = el("div","node tool"); const c = el("div","nodecard");
  const h = el("div","nodehead");
  const title = el("span","nodetitle"); title.appendChild(el("b","","TOOL "));
  title.appendChild(document.createTextNode("· "+t.tool));
  h.appendChild(title);
  h.appendChild(el("span","ms", num(t.result_chars)+" chars returned"));
  c.appendChild(h);
  const kv = el("div","kv");
  const row = (k,v) => { kv.appendChild(el("div","k",k)); kv.appendChild(el("div",null,v)); };
  row("call id", t.call_id);
  row("arguments sha256", t.arguments_sha256);
  row("result sha256", t.result_sha256);
  c.appendChild(kv);
  const est = Math.round((t.result_chars||0)/4);
  if(est > 200 && resends > 0){
    const w = el("div","note");
    if(t.result_chars > 4000) w.style.color = "#f0b429";
    w.textContent = "~"+num(est)+" tokens, re-sent on "+resends+" later request"+
                    (resends===1?"":"s")+": ~"+num(est*resends)+" prompt tokens of downstream cost.";
    c.appendChild(w);
  }
  c.appendChild(el("div","note","This evidence row stores digests and lengths. The local checkpoint "+
                                "conversation may contain raw tool output; use the protected conversation toggle deliberately."));
  n.appendChild(c); return n;
}

function waterfallFromCheckpoint(d){
  const t = d.model_timings;
  const t0 = t[0].request_started_at, t1 = t[t.length-1].completed_at;
  const span = Math.max(t1-t0, 1e-6);
  const wf = el("div","wf");
  t.forEach((x,i) => {
    const row = el("div","wfrow");
    row.appendChild(el("div","wflabel","model · step "+x.step));
    const track = el("div","wftrack");
    const bar = el("div","wfbar");
    bar.style.left = ((x.request_started_at-t0)/span*100)+"%";
    bar.style.width = Math.max((x.completed_at-x.request_started_at)/span*100, 0.6)+"%";
    const pre = (x.first_delta_at||x.completed_at)-x.request_started_at;
    const gen = Math.max(x.completed_at-(x.first_delta_at||x.completed_at),0);
    const p = el("div","p"); p.style.width=(pre/(pre+gen)*100)+"%";
    const g = el("div","g"); g.style.width=(gen/(pre+gen)*100)+"%";
    bar.appendChild(p); bar.appendChild(g);
    bar.title = "step "+x.step+" · "+fx(x.completed_at-x.request_started_at)+"s";
    track.appendChild(bar); row.appendChild(track); wf.appendChild(row);
  });
  const axis = el("div","wfaxis"); axis.appendChild(el("div"));
  const ticks = el("div","wfticks");
  for(let i=0;i<=4;i++){ const s=el("span",null,fx(span*i/4,1)+"s");
    s.style.left=(i/4*100)+"%"; ticks.appendChild(s); }
  axis.appendChild(ticks); wf.appendChild(axis);
  return wf;
}

function guardPanel(d, wall){
  const g = d.guards, wrap = el("div","guards");
  const tot = d.prompt_tokens + d.completion_tokens;
  [["steps", d.step, g.max_steps],
   ["wall clock", wall, g.max_seconds, "s"],
   ["total tokens", tot, g.max_total_tokens],
   ["consecutive errors", d.consecutive_errors, g.max_consecutive_errors],
   ["repeated call", d.repeated_call_count, g.max_repeated_call],
  ].forEach(([k,v,max,unit]) => {
    const pct = Math.min(v/max*100, 100);
    const c = el("div","guard");
    c.appendChild(el("div","gk",k));
    c.appendChild(el("div","gv", (unit==="s"?fx(v,1):num(Math.round(v)))+" / "+(unit==="s"?fx(max,0):num(max))+(unit||"")));
    const bar = el("div","bar"); const i = el("i");
    i.style.width = pct+"%";
    if(pct >= 100) i.className="bad"; else if(pct >= 60) i.className="warn";
    bar.appendChild(i); c.appendChild(bar); wrap.appendChild(c);
  });
  return wrap;
}

function eventTable(events){
  const c = el("div","card");
  const tbl = el("table");
  const hr = el("tr");
  ["#","status","step","reason","checkpoint sha256"].forEach(h => hr.appendChild(el("th",null,h)));
  tbl.appendChild(hr);
  events.slice().reverse().forEach(e => {
    const r = el("tr");
    r.appendChild(el("td",null,String(e.sequence)));
    const st = el("td"); st.appendChild(el("span","badge "+(e.status==="completed"?"ok":e.status==="running"?"":"bad"), e.status));
    r.appendChild(st);
    r.appendChild(el("td",null,String(e.step)));
    r.appendChild(el("td",null,e.reason||""));
    r.appendChild(el("td","hash",(e.checkpoint_sha256||"").slice(0,32)+"…"));
    tbl.appendChild(r);
  });
  c.appendChild(tbl); return c;
}

/* ---- trace-fidelity view ---- */
function renderTrace(d){
  const main = $("main"); main.innerHTML = "";
  const recs = d.records;
  const started = recs.find(r => r.kind==="run_started") || {};
  const finished = recs.find(r => r.kind==="run_finished");
  const t0 = d.monotonic_origin ?? (recs[0] ? recs[0].t : 0);

  const head = el("div"); head.style.cssText="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap";
  head.appendChild(el("span","fid trace","TRACE FIDELITY · every request and tool bracketed"));
  head.appendChild(el("span","badge "+(finished? (finished.status==="completed"?"ok":"bad") : "warn"),
                      finished ? finished.status : "in flight"));
  main.appendChild(head);

  const t = el("div","taskline"); t.appendChild(el("div","lbl","User task"));
  t.appendChild(el("div",null, started.task || "(none)")); main.appendChild(t);

  // Pair started/finished by (actor, sequence) so an unmatched start is in flight.
  const starts = recs.filter(r => r.kind==="model_request_started");
  const fins   = recs.filter(r => r.kind==="model_request_finished");
  const key = (r) => r.actor+"#"+r.request_sequence;
  const finBy = {}; fins.forEach(f => finBy[key(f)] = f);
  const reqs = starts.map(s => ({s, f: finBy[key(s)] || null}));
  const inflight = reqs.filter(r => !r.f).length;

  const toolStarts = recs.filter(r => r.kind==="tool_started");
  const toolFins   = recs.filter(r => r.kind==="tool_finished");
  const actors = [...new Set(starts.map(s => s.actor))];
  const last = recs.length ? recs[recs.length-1].t : t0;

  main.appendChild(statStrip([
    ["Actors", actors.length],
    ["Model calls", reqs.length],
    ["In flight", inflight],
    ["Tool calls", toolStarts.length],
    ["Peak overlap", peakOverlap(reqs.filter(r=>r.f).map(r => [r.s.t, r.f.t]))],
    ["Trace span", fx(last-t0)+"s"],
  ]));

  main.appendChild(h2("Concurrency", actors.length>1
    ? "one lane per worker; overlap proves requests reached MLX-LM together"
    : "single agent"));
  main.appendChild(traceWaterfall(reqs, toolStarts, toolFins, t0, last));
  if(actors.length>1){
    main.appendChild(el("p","note",
      "Overlapping bars prove concurrent requests reached the MLX server. They do not prove "+
      "simultaneous hardware decoding, which this dashboard cannot observe."));
  }

  main.appendChild(h2("Token economics", "exact per-request accounting from the model's own usage block"));
  main.appendChild(tokenPanel(fins, actors));

  main.appendChild(h2("Request journey","live, including requests still in flight"));
  main.appendChild(traceJourney(recs, t0));
}

/* Per-request and per-agent token accounting.

   The loop re-sends the entire conversation on every step, so prompt tokens are
   paid again for text already paid for. `peak` is the largest single context,
   which is roughly the unique material; total prompt tokens divided by peak is
   how many times over that material was bought. */
function tokenPanel(fins, actors){
  const wrap = el("div");
  if(!fins.length){ wrap.appendChild(el("div","empty","No finished requests yet.")); return wrap; }
  const pin  = fins.reduce((a,r)=>a+(r.prompt_tokens||0),0);
  const pout = fins.reduce((a,r)=>a+(r.completion_tokens||0),0);
  const peak = Math.max(...fins.map(r=>r.prompt_tokens||0), 1);
  const genS = fins.reduce((a,r)=>a+Math.max((r.request_seconds||0)-(r.time_to_first_delta_seconds||0),0),0);
  const preS = fins.reduce((a,r)=>a+(r.time_to_first_delta_seconds||0),0);
  wrap.appendChild(statStrip([
    ["Burned", num(pin+pout), "tok"],
    ["Prompt in", num(pin)],
    ["Completion out", num(pout)],
    ["Peak context", num(peak), "tok"],
    ["Re-send factor", fx(pin/peak,1)+"x"],
    ["Decode", genS>0 ? fx(pout/genS,1)+" tok/s" : "-", "per lane"],
    ["Prefill", preS>0 ? num(Math.round(pin/preS))+" tok/s" : "-", "per lane"],
  ]));

  if(actors.length > 1){
    const by = {};
    fins.forEach(r => { const a = by[r.actor] = by[r.actor] || {n:0,p:0,c:0,s:0};
      a.n++; a.p += r.prompt_tokens||0; a.c += r.completion_tokens||0; a.s += r.request_seconds||0; });
    const rows = Object.entries(by).sort((x,y)=>(y[1].p+y[1].c)-(x[1].p+x[1].c));
    const max = Math.max(...rows.map(([,v])=>v.p+v.c), 1);
    const card = el("div","card"); card.style.marginTop="10px";
    card.appendChild(el("div","gk","Per agent"));
    rows.forEach(([actor,v]) => {
      const tot = v.p+v.c;
      const row = el("div","brow");
      const lab = el("div","blab", actor); row.appendChild(lab);
      const track = el("div","btrack");
      const p = el("div","bseg p"); p.style.width=(v.p/max*100)+"%"; p.title=num(v.p)+" prompt";
      const c = el("div","bseg c"); c.style.width=(v.c/max*100)+"%"; c.title=num(v.c)+" completion";
      track.appendChild(p); track.appendChild(c); row.appendChild(track);
      row.appendChild(el("div","bval", num(tot)+"  "+Math.round(tot/(pin+pout)*100)+"%"));
      card.appendChild(row);
    });
    wrap.appendChild(card);
  }

  const card = el("div","card"); card.style.marginTop="10px";
  const tbl = el("table"); const hr = el("tr");
  ["agent","req","prompt in","completion out","prefill","stream","out tok/s","running total"]
    .forEach(x => hr.appendChild(el("th",null,x)));
  tbl.appendChild(hr);
  let running = 0, suspect = false;
  fins.slice().sort((a,b)=>a.t-b.t).forEach(r => {
    running += (r.prompt_tokens||0)+(r.completion_tokens||0);
    const gen = Math.max((r.request_seconds||0)-(r.time_to_first_delta_seconds||0),0);
    const rate = gen > 0 ? (r.completion_tokens||0)/gen : 0;
    // A tool-call turn often arrives as one stream flush, so this interval is
    // delivery time, not decode time. Flag it rather than publish a fake rate.
    const flush = rate > 150;
    if(flush) suspect = true;
    const tr = el("tr");
    [r.actor, "#"+r.request_sequence, num(r.prompt_tokens), num(r.completion_tokens),
     fx(r.time_to_first_delta_seconds)+"s", fx(gen)+"s",
     gen>0 ? (flush ? fx(rate,0)+" †" : fx(rate,1)) : "-", num(running)]
      .forEach(x => tr.appendChild(el("td",null,String(x))));
    tbl.appendChild(tr);
  });
  card.appendChild(tbl); wrap.appendChild(card);
  if(suspect){
    wrap.appendChild(el("p","note",
      "† Not a decode rate. The stream column is first-delta to terminal, and a turn that "+
      "returns only a tool call usually arrives in a single flush, so the interval measures "+
      "delivery rather than generation."));
  }
  wrap.appendChild(el("p","note",
    "Prompt tokens are re-billed every step because the whole conversation is resent. A re-send "+
    "factor of "+fx(pin/peak,1)+"x means the context was paid for that many times over. Cutting a "+
    "large tool result early is worth more than shortening any answer."));
  return wrap;
}

function peakOverlap(intervals){
  const ev = [];
  intervals.forEach(([a,b]) => { if(b>a){ ev.push([a,1]); ev.push([b,-1]); } });
  ev.sort((x,y) => x[0]-y[0] || x[1]-y[1]);
  let cur=0, peak=0;
  ev.forEach(([,d]) => { cur+=d; if(cur>peak) peak=cur; });
  return peak;
}

function traceWaterfall(reqs, toolStarts, toolFins, t0, last){
  const span = Math.max(last-t0, 1e-6);
  const wf = el("div","wf");
  const lanes = {};
  reqs.forEach(r => { (lanes[r.s.actor] = lanes[r.s.actor] || []).push(r); });
  const toolBy = {};
  toolFins.forEach(f => toolBy[f.actor+"#"+f.tool_sequence] = f);

  Object.keys(lanes).forEach(actor => {
    const row = el("div","wfrow");
    row.appendChild(el("div","wflabel", actor));
    const track = el("div","wftrack");
    lanes[actor].forEach(r => {
      const end = r.f ? r.f.t : last;
      const bar = el("div","wfbar");
      bar.style.left = ((r.s.t-t0)/span*100)+"%";
      bar.style.width = Math.max((end-r.s.t)/span*100, 0.6)+"%";
      if(r.f){
        const pre = r.f.time_to_first_delta_seconds || 0;
        const tot = Math.max(r.f.request_seconds || (end-r.s.t), 1e-6);
        const p = el("div","p"); p.style.width = Math.min(pre/tot*100,100)+"%";
        const g = el("div","g"); g.style.width = Math.max(100-pre/tot*100,0)+"%";
        bar.appendChild(p); bar.appendChild(g);
        bar.title = actor+" req "+r.s.request_sequence+" · "+fx(tot)+"s (prefill "+fx(pre)+"s)";
      } else {
        bar.style.background = "repeating-linear-gradient(45deg,#4a4028,#4a4028 6px,#5c5033 6px,#5c5033 12px)";
        bar.title = actor+" req "+r.s.request_sequence+" · in flight";
      }
      track.appendChild(bar);
    });
    row.appendChild(track); wf.appendChild(row);
  });

  if(toolStarts.length){
    const row = el("div","wfrow");
    row.appendChild(el("div","wflabel","tools"));
    const track = el("div","wftrack");
    toolStarts.forEach(s => {
      const f = toolBy[s.actor+"#"+s.tool_sequence];
      const end = f ? s.t + (f.duration_seconds||0) : last;
      const bar = el("div","wfbar toolbar");
      bar.style.left = ((s.t-t0)/span*100)+"%";
      bar.style.width = Math.max((end-s.t)/span*100, 0.6)+"%";
      bar.title = s.tool+" · "+fx(end-s.t,3)+"s";
      track.appendChild(bar);
    });
    row.appendChild(track); wf.appendChild(row);
  }

  const axis = el("div","wfaxis"); axis.appendChild(el("div"));
  const ticks = el("div","wfticks");
  for(let i=0;i<=4;i++){ const s=el("span",null,fx(span*i/4,1)+"s"); s.style.left=(i/4*100)+"%"; ticks.appendChild(s); }
  axis.appendChild(ticks); wf.appendChild(axis);
  return wf;
}

function traceJourney(recs, t0){
  const wrap = el("div","cycle");
  const pendingTool = {};
  recs.forEach((r, idx) => {
    if(r.kind === "run_started"){
      const n = el("div","node"); const c = el("div","nodecard");
      const h = el("div","nodehead"); h.appendChild(el("span","nodetitle","USER TASK"));
      h.appendChild(el("span","ms","+"+fx(r.t-t0)+"s"));
      c.appendChild(h); c.appendChild(el("div","pre", r.task||"(none)"));
      n.appendChild(c); wrap.appendChild(n);
    }
    if(r.kind === "model_request_started"){
      pendingTool["m"+r.actor+"#"+r.request_sequence] = r;
    }
    if(r.kind === "model_request_finished"){
      const s = pendingTool["m"+r.actor+"#"+r.request_sequence];
      delete pendingTool["m"+r.actor+"#"+r.request_sequence];
      const n = el("div","node model"); const c = el("div","nodecard");
      const h = el("div","nodehead");
      const ti = el("span","nodetitle"); ti.appendChild(el("b","","MODEL REQUEST "));
      ti.appendChild(document.createTextNode("· "+r.actor+" #"+r.request_sequence));
      h.appendChild(ti);
      h.appendChild(el("span","ms","+"+fx((s?s.t:r.t)-t0)+"s · "+fx(r.request_seconds)+"s"));
      c.appendChild(h);
      c.appendChild(segbar({
        request_started_at: r.request_started_at, first_delta_at: r.first_delta_at,
        terminal_at: r.terminal_at, completed_at: r.completed_at }));
      c.appendChild(segLegend({
        request_started_at: r.request_started_at, first_delta_at: r.first_delta_at,
        terminal_at: r.terminal_at, completed_at: r.completed_at }));
      const kv = el("div","kv");
      const row = (k,v) => { kv.appendChild(el("div","k",k)); kv.appendChild(el("div",null,v)); };
      row("finish reason", r.finish_reason);
      row("message content", num(s ? s.message_content_chars : "-")+" chars");
      row("tokens", num(r.prompt_tokens)+" in / "+num(r.completion_tokens)+" out");
      if(r.tool_calls && r.tool_calls.length)
        row("requested tools", r.tool_calls.map(c2 => c2.name+" "+c2.arguments).join(", "));
      c.appendChild(kv);
      if(r.content_preview) c.appendChild(el("div","pre", r.content_preview));
      n.appendChild(c); wrap.appendChild(n);
    }
    if(r.kind === "tool_started") pendingTool[r.actor+"#"+r.tool_sequence] = r;
    if(r.kind === "tool_finished"){
      const s = pendingTool[r.actor+"#"+r.tool_sequence] || {};
      const n = el("div","node tool"); const c = el("div","nodecard");
      const h = el("div","nodehead");
      const ti = el("span","nodetitle"); ti.appendChild(el("b","","TOOL "));
      ti.appendChild(document.createTextNode("· "+r.tool));
      h.appendChild(ti);
      h.appendChild(el("span","ms", fx(r.duration_seconds,3)+"s · "+num(r.result_chars)+" chars"));
      c.appendChild(h);
      const kv = el("div","kv");
      kv.appendChild(el("div","k","arguments"));
      kv.appendChild(el("div",null, JSON.stringify(s.arguments || {})));
      c.appendChild(kv);
      if(r.result_preview) c.appendChild(el("div","pre", r.result_preview));
      // What this result costs downstream: it is appended to the conversation and
      // re-sent verbatim on every remaining request in this run.
      const est = Math.round((r.result_chars||0)/4);
      const resends = recs.slice(idx).filter(x => x.kind==="model_request_started"
                                              && x.actor === r.actor).length;
      if(est > 200 && resends > 0){
        const w = el("div","note");
        if(r.result_chars > 4000) w.style.color = "#f0b429";
        w.textContent = "~"+num(est)+" tokens, re-sent on "+resends+" later request"+
                        (resends===1?"":"s")+" by this agent: ~"+num(est*resends)+
                        " prompt tokens of downstream cost.";
        c.appendChild(w);
      }
      n.appendChild(c); wrap.appendChild(n);
      wrap.appendChild(el("div","retedge","result appended to conversation, loop continues"));
    }
    if(r.kind === "model_request_failed" || r.kind === "tool_failed"){
      const n = el("div","node"); const c = el("div","nodecard");
      c.style.borderColor = "#4a2225";
      const h = el("div","nodehead"); h.appendChild(el("span","nodetitle","FAILED · "+r.kind));
      c.appendChild(h); c.appendChild(el("div","pre", (r.error_type||"")+": "+(r.error||"")));
      n.appendChild(c); wrap.appendChild(n);
    }
    if(r.kind === "run_finished"){
      const n = el("div","node out"); const c = el("div","nodecard");
      const h = el("div","nodehead"); h.appendChild(el("span","nodetitle","RUN FINISHED"));
      h.appendChild(el("span","ms","+"+fx(r.t-t0)+"s"));
      c.appendChild(h);
      const kv = el("div","kv");
      const row = (k,v) => { kv.appendChild(el("div","k",k)); kv.appendChild(el("div",null,String(v))); };
      row("status", r.status); if(r.run_id) row("run id", r.run_id);
      if(r.reason) row("reason", r.reason);
      row("tokens", num(r.prompt_tokens)+" in / "+num(r.completion_tokens)+" out");
      c.appendChild(kv); n.appendChild(c); wrap.appendChild(n);
    }
  });
  // Anything still unmatched is genuinely in flight right now.
  Object.keys(pendingTool).filter(k => k.startsWith("m")).forEach(k => {
    const s = pendingTool[k];
    const n = el("div","node model inflight"); const c = el("div","nodecard");
    const h = el("div","nodehead");
    const ti = el("span","nodetitle"); ti.appendChild(el("b","","MODEL REQUEST "));
    ti.appendChild(document.createTextNode("· "+s.actor+" #"+s.request_sequence));
    h.appendChild(ti); h.appendChild(el("span","ms","in flight"));
    c.appendChild(h);
    const bar = el("div","segbar"); bar.appendChild(el("div","seg pending","waiting for first delta"));
    c.appendChild(bar);
    const kv = el("div","kv");
    kv.appendChild(el("div","k","prompt sent")); kv.appendChild(el("div",null,num(s.prompt_chars)+" chars"));
    c.appendChild(kv);
    n.appendChild(c); wrap.appendChild(n);
  });
  return wrap;
}

/* ---------- live ---------- */
function connect(){
  const es = new EventSource(apiURL("/api/stream"));
  const c = $("conn");
  es.onopen = () => { c.innerHTML=""; c.appendChild(el("span","dot live"));
                      c.appendChild(document.createTextNode("live")); };
  es.addEventListener("changed", () => refresh());
  es.onerror = () => { c.innerHTML=""; c.appendChild(el("span","dot bad"));
                       c.appendChild(document.createTextNode("reconnecting")); };
}
refresh().then(connect).catch(e => {
  $("main").innerHTML=""; $("main").appendChild(el("div","empty","Failed to load: "+e.message));
});
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = parser.parse_args()

    try:
        model_config = LocalModelConfig(base_url=args.endpoint)
    except LocalConfigurationError as exc:
        print(f"invalid local model endpoint: {exc}", file=sys.stderr)
        return 2

    root = args.root.expanduser().resolve(strict=False)
    if not root.is_dir():
        print(f"state root does not exist: {root}", file=sys.stderr)
        return 2

    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "store": Store(root),
            "model_config": model_config,
            "capability": secrets.token_urlsafe(24),
            "allowed_hosts": {
                f"127.0.0.1:{args.port}",
                f"localhost:{args.port}",
                f"[::1]:{args.port}",
            },
        },
    )

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        address_family = socket.AF_INET

    # Loopback only. Never 0.0.0.0: this exposes prompts and model output.
    server = Server(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/{handler.capability}"
    print(f"Jarvis V2 pipeline dashboard: {url}")
    print(f"  watching  {root}")
    print(f"  model api {model_config.base_url}")
    print("  read-only: no run is ever written, locked, or resumed from here")
    if args.open:
        import webbrowser

        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
