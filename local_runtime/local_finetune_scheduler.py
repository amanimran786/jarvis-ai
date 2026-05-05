"""
Overnight fine-tuning scheduler for Jarvis on Apple Silicon.

Runs 11pm → 7am daily. Builds training packs from conversation history,
runs MLX QLoRA SFT, evaluates results, promotes if better.

Called by: brain_daemon.py (scheduled thread) AND launchd plist directly.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config
from local_runtime import local_mlx_training

REPO_ROOT = Path(__file__).resolve().parent.parent
TRAINING_ROOT = REPO_ROOT / "training"
STATE_FILE = TRAINING_ROOT / "overnight_state.json"
LOG_FILE = TRAINING_ROOT / "overnight_log.jsonl"
PACKS_DIR = TRAINING_ROOT / "packs"
MEMORY_FILE = REPO_ROOT / "memory.json"

# Ensure logging is configured
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _timestamp() -> str:
    """Return ISO timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_date() -> str:
    """Return YYYY-MM-DD for today."""
    return datetime.now().strftime("%Y-%m-%d")


class OvernightTrainer:
    """Orchestrate overnight fine-tuning cycle."""

    def __init__(self) -> None:
        """Initialize paths and load state."""
        TRAINING_ROOT.mkdir(parents=True, exist_ok=True)
        PACKS_DIR.mkdir(parents=True, exist_ok=True)

        self.logger = logger
        self.state: dict = self._load_state()
        if self._repair_state_from_log():
            self._save_state()

    def _load_state(self) -> dict:
        """Load state from overnight_state.json, or return fresh state."""
        if not STATE_FILE.exists():
            return {
                "last_run_date": None,
                "last_session": None,
                "baseline_eval_passed": 0,
                "baseline_eval_total": 0,
            }

        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self.logger.warning(f"Failed to load state: {e}, using defaults")
            return {
                "last_run_date": None,
                "last_session": None,
                "baseline_eval_passed": 0,
                "baseline_eval_total": 0,
            }

    def _read_log_sessions(self) -> list[dict]:
        """Return valid sessions from the append-only overnight log."""
        if not LOG_FILE.exists():
            return []
        sessions: list[dict] = []
        try:
            with LOG_FILE.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        session = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(session, dict):
                        sessions.append(session)
        except Exception as e:
            self.logger.warning(f"Failed to read overnight log: {e}")
        return sessions

    @staticmethod
    def _session_eval_counts(session: dict) -> tuple[int, int]:
        stages = session.get("stages") if isinstance(session.get("stages"), dict) else {}
        eval_stage = stages.get("eval") if isinstance(stages.get("eval"), dict) else {}
        passed = int(session.get("eval_passed") or eval_stage.get("passed") or 0)
        total = int(session.get("eval_total") or eval_stage.get("total") or 0)
        return passed, total

    @staticmethod
    def _repair_session_summary(session: dict) -> dict:
        """Backfill top-level summary fields used by status/dashboard consumers."""
        repaired = dict(session)
        stages = repaired.get("stages") if isinstance(repaired.get("stages"), dict) else {}
        build = stages.get("build") if isinstance(stages.get("build"), dict) else {}
        training = stages.get("training") if isinstance(stages.get("training"), dict) else {}
        passed, total = OvernightTrainer._session_eval_counts(repaired)
        repaired["eval_passed"] = passed
        repaired["eval_total"] = total
        if not repaired.get("examples_count"):
            examples_count = int(training.get("examples_count") or 0)
            pack_path = build.get("pack_path")
            if not examples_count and pack_path:
                try:
                    examples_count = sum(
                        1 for line in Path(pack_path).read_text(encoding="utf-8").splitlines() if line.strip()
                    )
                except Exception:
                    examples_count = 0
            repaired["examples_count"] = examples_count
        if not repaired.get("duration_seconds"):
            repaired["duration_seconds"] = training.get("duration_seconds", training.get("duration_sec", 0)) or 0
        return repaired

    def _repair_state_from_log(self) -> bool:
        """Repair stale state from the append-only training log."""
        sessions = self._read_log_sessions()
        if not sessions:
            return False

        latest = self._repair_session_summary(sessions[-1])
        latest_passed, latest_total = self._session_eval_counts(latest)
        changed = False

        current_session = self.state.get("last_session") if isinstance(self.state.get("last_session"), dict) else {}
        if (
            not current_session
            or current_session.get("timestamp") != latest.get("timestamp")
            or (not current_session.get("examples_count") and latest.get("examples_count"))
            or (not current_session.get("duration_seconds") and latest.get("duration_seconds"))
        ):
            self.state["last_session"] = latest
            changed = True
        if not self.state.get("last_run_date") and latest.get("date"):
            self.state["last_run_date"] = latest.get("date")
            changed = True

        promoted_sessions = [
            self._repair_session_summary(session)
            for session in sessions
            if session.get("promoted") or (session.get("stages") or {}).get("promotion", {}).get("promoted")
        ]
        best = max(
            promoted_sessions,
            key=lambda session: self._session_eval_counts(session)[0],
            default=latest if latest_total else {},
        )
        best_passed, best_total = self._session_eval_counts(best)
        if best_total and (
            best_passed > int(self.state.get("baseline_eval_passed") or 0)
            or best_total > int(self.state.get("baseline_eval_total") or 0)
        ):
            self.state["baseline_eval_passed"] = best_passed
            self.state["baseline_eval_total"] = best_total
            changed = True

        return changed

    def _save_state(self) -> None:
        """Persist state to overnight_state.json."""
        try:
            with STATE_FILE.open("w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")

    def is_training_window(self) -> bool:
        """Return True if current time is 23:00–07:00 (11pm-7am)."""
        now = datetime.now()
        hour = now.hour

        # Training window: 23:00 (11pm) through 06:59 (6:59am)
        # i.e., hour >= 23 OR hour < 7
        return hour >= 23 or hour < 7

    def should_run_tonight(self) -> bool:
        """Return True if not already run today."""
        last_run = self.state.get("last_run_date")
        today = _today_date()
        return last_run != today

    # ------------------------------------------------------------------
    # Training pack builders
    # ------------------------------------------------------------------

    def build_training_pack(self) -> Optional[Path]:
        """
        Build overnight training pack from multiple ranked sources:

          1. Teacher examples (training/teacher_examples/*.jsonl)
             Highest-quality curated pairs — always included in full.
          2. Real verbatim conversations (memory/conversations/verbatim.jsonl)
             Actual Jarvis interactions: tool-use, voice commands, memory recall.
          3. Synthetic typed examples for tool-use / voice / memory patterns.
          4. Legacy memory.json summaries as a low-quality fallback.

        Output: messages format {"messages": [{"role": ..., "content": ...}, ...]}
        required by mlx_lm ≥0.31.x.

        Returns path to overnight_{date}.jsonl or None if <10 examples.
        """
        examples: list[dict] = []

        # Source 1 — teacher examples (already in messages format)
        teacher_examples = self._collect_teacher_examples()
        examples.extend(teacher_examples)
        self.logger.info(f"Teacher examples: {len(teacher_examples)}")

        # Source 2 — verbatim real conversations
        verbatim_examples = self._collect_verbatim_examples(limit=80)
        examples.extend(verbatim_examples)
        self.logger.info(f"Verbatim examples: {len(verbatim_examples)}")

        # Source 3 — synthetic typed examples for underrepresented patterns
        synthetic = self._build_synthetic_examples()
        examples.extend(synthetic)
        self.logger.info(f"Synthetic examples: {len(synthetic)}")

        # Source 4 — legacy memory.json summaries (fallback, low quality)
        if len(examples) < 30:
            legacy = self._collect_legacy_summary_examples()
            examples.extend(legacy)
            self.logger.info(f"Legacy fallback examples: {len(legacy)}")

        if len(examples) < 10:
            self.logger.info(f"Only {len(examples)} examples; need >=10 — aborting pack")
            return None

        # Deduplicate by user message content
        seen: set[str] = set()
        deduped: list[dict] = []
        for ex in examples:
            msgs = ex.get("messages", [])
            user_content = next(
                (m["content"] for m in msgs if m.get("role") == "user"), ""
            )
            key = user_content[:120]
            if key and key not in seen:
                seen.add(key)
                deduped.append(ex)

        self.logger.info(f"Pack size after dedup: {len(deduped)} examples")

        today = _today_date()
        pack_path = PACKS_DIR / f"overnight_{today}.jsonl"
        try:
            with pack_path.open("w", encoding="utf-8") as f:
                for ex in deduped:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            self.logger.info(f"Built training pack: {pack_path} ({len(deduped)} examples)")
            return pack_path
        except Exception as e:
            self.logger.error(f"Failed to save pack: {e}")
            return None

    def _collect_teacher_examples(self) -> list[dict]:
        """
        Load all JSONL files from training/teacher_examples/.

        Each file is expected to contain one JSON object per line in messages format.
        Returns examples as-is (already correctly formatted).
        """
        teacher_dir = TRAINING_ROOT / "teacher_examples"
        if not teacher_dir.exists():
            return []

        examples = []
        for jsonl_path in sorted(teacher_dir.glob("*.jsonl")):
            try:
                for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    # Accept records that already have messages list
                    if isinstance(record.get("messages"), list) and len(record["messages"]) >= 2:
                        # Strip heavy meta fields but keep messages
                        examples.append({"messages": record["messages"]})
            except Exception as e:
                self.logger.warning(f"Skipping teacher file {jsonl_path.name}: {e}")

        return examples

    def _collect_verbatim_examples(self, limit: int = 80) -> list[dict]:
        """
        Read real Jarvis interactions from memory/conversations/verbatim.jsonl.

        Format in file: {"user": "...", "assistant": "...", ...}
        Converts to messages format. Skips trivial pairs (assistant < 20 chars).
        Samples the most recent `limit` interactions.
        """
        verbatim_path = REPO_ROOT / "memory" / "conversations" / "verbatim.jsonl"
        if not verbatim_path.exists():
            return []

        try:
            import config as _config
            system_prompt = getattr(_config, "SYSTEM_PROMPT", "You are Jarvis.")
        except Exception:
            system_prompt = "You are Jarvis."

        records = []
        try:
            for line in verbatim_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                user = (r.get("user") or "").strip()
                asst = (r.get("assistant") or "").strip()
                # Skip trivial or diagnostic ping-pong pairs
                if not user or len(asst) < 20:
                    continue
                if user.startswith("USER: Reply with LOCAL_OK"):
                    continue
                records.append((user, asst))
        except Exception as e:
            self.logger.warning(f"Failed to read verbatim log: {e}")
            return []

        # Take the most recent `limit` interactions
        recent = records[-limit:]

        examples = []
        for user, asst in recent:
            examples.append({
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": asst},
                ]
            })
        return examples

    def _build_synthetic_examples(self) -> list[dict]:
        """
        Return handcrafted synthetic examples for tool-use, voice, and memory patterns.

        These cover categories that may be sparse in real logs:
          - Calendar event creation
          - Terminal command execution
          - Web search + answer
          - Voice command → spoken response
          - Memory recall (preferences, projects)
          - iMessage / contact lookup
        """
        try:
            import config as _config
            sp = getattr(_config, "SYSTEM_PROMPT", "You are Jarvis.")
        except Exception:
            sp = "You are Jarvis."

        def ex(user: str, asst: str) -> dict:
            return {
                "messages": [
                    {"role": "system", "content": sp},
                    {"role": "user", "content": user},
                    {"role": "assistant", "content": asst},
                ]
            }

        return [
            # Calendar
            ex(
                "Schedule a meeting with Sarah tomorrow at 2pm called Engineering Sync.",
                "Done. Engineering Sync is on your calendar for tomorrow at 2 PM. "
                "I'll send Sarah an invite.",
            ),
            ex(
                "What's on my calendar this week?",
                "You have three things: a dentist appointment Tuesday at 10 AM, "
                "team standup Wednesday at 9, and dinner with dad Friday at 7.",
            ),
            ex(
                "Cancel my 3pm today.",
                "Cancelled. Your 3 PM slot is clear.",
            ),
            # Terminal / code
            ex(
                "Run the test suite.",
                "Running pytest now.",
            ),
            ex(
                "What's my git status?",
                "You're on main, 2 files modified, nothing staged. "
                "Changes are in router.py and config.py.",
            ),
            ex(
                "Open the jarvis-ai directory in VS Code.",
                "Opening VS Code in the jarvis-ai directory.",
            ),
            # Search / knowledge
            ex(
                "What's the latest on WWDC 2026?",
                "Apple hasn't announced dates yet as of my last update. "
                "I'll search for current news.",
            ),
            ex(
                "What's 15% of 340?",
                "Fifty-one dollars.",
            ),
            # Voice / TTS commands
            ex(
                "Set a timer for 10 minutes.",
                "Timer set for 10 minutes.",
            ),
            ex(
                "Turn the volume down.",
                "Volume reduced.",
            ),
            ex(
                "Read my latest email.",
                "From Sarah, subject: Budget Review — she's asking for the Q2 numbers "
                "by end of week.",
            ),
            # iMessage / contacts
            ex(
                "Message dad and tell him I'll be home by 8.",
                "Draft ready for Dad: I'll be home by 8. Say confirm to send.",
            ),
            ex(
                "What's mom's number?",
                "Your mom's number is saved as +1 510 555 0101.",
            ),
            # Memory / preferences
            ex(
                "What coding language do I prefer?",
                "Python is your primary language. "
                "You also work in TypeScript for frontend and shell scripting for automation.",
            ),
            ex(
                "What projects am I working on?",
                "Jarvis AI is the main one. "
                "You also have the portfolio site and a research project on local model training.",
            ),
            ex(
                "Remember that I prefer dark mode in all apps.",
                "Saved. Dark mode preference noted across all apps.",
            ),
            # System control
            ex(
                "Take a screenshot.",
                "Screenshot captured and saved to your Desktop.",
            ),
            ex(
                "What's my battery level?",
                "Battery is at 73%, plugged in and charging.",
            ),
            ex(
                "Lock the screen.",
                "Screen locked.",
            ),
            # Conversational / identity
            ex(
                "What can you do?",
                "I can run terminal commands, manage your calendar and email, send iMessages, "
                "search the web, control your Mac, write and debug code, recall anything you've "
                "told me, and learn from every conversation. What do you need?",
            ),
            ex(
                "Are you actually Jarvis or just a chatbot?",
                "I'm Jarvis — a fully integrated runtime on your Mac, not a generic chatbot. "
                "I have direct access to your terminal, calendar, contacts, and system controls.",
            ),
        ]

    def _collect_legacy_summary_examples(self) -> list[dict]:
        """
        Low-quality fallback: build examples from memory.json conversation summaries.

        Used only when other sources produce fewer than 30 examples.
        """
        if not MEMORY_FILE.exists():
            return []

        try:
            with MEMORY_FILE.open("r", encoding="utf-8") as f:
                memory = json.load(f)
        except Exception as e:
            self.logger.warning(f"Failed to load memory.json: {e}")
            return []

        try:
            import config as _config
            sp = getattr(_config, "SYSTEM_PROMPT", "You are Jarvis.")
        except Exception:
            sp = "You are Jarvis."

        convos = memory.get("conversation_history", [])
        examples = []
        for convo in convos:
            summary = (convo.get("summary") or "").strip()
            if len(summary) > 30:
                examples.append({
                    "messages": [
                        {"role": "system", "content": sp},
                        {"role": "user", "content": f"What happened in our recent conversation?"},
                        {"role": "assistant", "content": summary},
                    ]
                })
        return examples

    def run_training(self, pack_path: Path) -> dict:
        """
        Run MLX SFT fine-tuning.

        Returns dict with success bool, adapter_path, duration_seconds.
        """
        self.logger.info(f"Starting training with pack: {pack_path}")

        # Use default model from config (e.g., "qwen2.5-coder:7b")
        model_tag = config.LOCAL_CODER

        result = local_mlx_training.run_sft(
            model_tag,
            train_jsonl=pack_path,
            num_iters=2,  # Lightweight for overnight
            dry_run=False,
        )

        self.logger.info(f"Training result: ok={result.get('ok')}, error={result.get('error')}")
        try:
            result["examples_count"] = sum(
                1 for line in pack_path.read_text(encoding="utf-8").splitlines() if line.strip()
            )
        except Exception:
            result["examples_count"] = 0
        return result

    def run_eval(self) -> dict:
        """
        Run evaluation suite via pytest.

        Returns {"passed": N, "failed": N, "total": N}.
        """
        self.logger.info("Running evaluation suite")

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest",
                 str(REPO_ROOT / "tests" / "test_jarvis_golden_cases.py"),
                 "-q", "--tb=no"],
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(REPO_ROOT),
            )

            # Parse output: "X passed, Y failed" or similar
            output = result.stdout + result.stderr
            passed = 0
            failed = 0

            # Simple parsing: look for "N passed" and "N failed" patterns
            import re
            passed_match = re.search(r"(\d+) passed", output)
            failed_match = re.search(r"(\d+) failed", output)

            if passed_match:
                passed = int(passed_match.group(1))
            if failed_match:
                failed = int(failed_match.group(1))

            total = passed + failed
            if total == 0:
                benchmark = self._run_benchmark_eval()
                if benchmark.get("total", 0) > 0:
                    return benchmark

            self.logger.info(f"Eval result: {passed} passed, {failed} failed (total {total})")

            return {"passed": passed, "failed": failed, "total": total}

        except subprocess.TimeoutExpired:
            self.logger.error("Evaluation timed out")
            return {"passed": 0, "failed": 0, "total": 0}
        except Exception as e:
            self.logger.error(f"Evaluation failed: {e}")
            return {"passed": 0, "failed": 0, "total": 0}

    def _run_benchmark_eval(self) -> dict:
        """Fallback eval gate when live golden cases are skipped."""
        try:
            sys.path.insert(0, str(TRAINING_ROOT))
            from benchmark_tracker import run_full_benchmark

            record = run_full_benchmark(model_version=f"jarvis-{_today_date()}")
            passed = int(record.get("total_passed") or 0)
            total = int(record.get("total_tests") or 0)
            failed = max(0, total - passed)

            if total > 0:
                self.logger.info(
                    f"Benchmark eval fallback: {passed} passed, {failed} failed (total {total})"
                )
                return {
                    "passed": passed,
                    "failed": failed,
                    "total": total,
                    "source": "benchmark_tracker",
                    "overall": record.get("overall"),
                    "categories": record.get("categories", {}),
                }
        except Exception as e:
            self.logger.warning(f"Benchmark eval fallback failed: {e}")

        return {"passed": 0, "failed": 0, "total": 0}

    def promote_if_better(self, training_result: dict, eval_result: dict) -> bool:
        """
        Compare eval results to baseline. If improved, fuse adapter and update state.

        Returns True if promoted.
        """
        if not training_result.get("ok"):
            self.logger.info("Training failed; skipping promotion")
            return False

        current_passed = eval_result.get("passed", 0)
        baseline_passed = self.state.get("baseline_eval_passed", 0)

        self.logger.info(
            f"Promotion check: current {current_passed} vs baseline {baseline_passed}"
        )

        if current_passed >= baseline_passed:
            # Good! Fuse and promote.
            adapter_path = training_result.get("adapter_path")
            if adapter_path:
                self.logger.info(f"Promoting adapter: {adapter_path}")
                model_hf_id = "Qwen/Qwen2.5-Coder-7B-Instruct"
                fused_dir = TRAINING_ROOT / "exports" / "overnight_fused"

                fuse_result = local_mlx_training.fuse_adapter(
                    adapter_path=adapter_path,
                    base_model_hf_id=model_hf_id,
                    output_dir=fused_dir,
                )

                if fuse_result.get("ok"):
                    self.logger.info(f"Fused adapter saved to {fused_dir}")
                    self.state["baseline_eval_passed"] = current_passed
                    self.state["baseline_eval_total"] = eval_result.get("total", 0)
                    self._save_state()
                    return True
                else:
                    self.logger.error(f"Fusion failed: {fuse_result.get('error')}")

        return False

    def log_session(self, session: dict) -> None:
        """Append session dict to overnight_log.jsonl."""
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        try:
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(session, ensure_ascii=False) + "\n")
        except Exception as e:
            self.logger.error(f"Failed to log session: {e}")

    def run_full_cycle(self) -> dict:
        """
        Orchestrate build → train → eval → promote.

        Returns full session dict.
        """
        self.logger.info("Starting overnight training cycle")

        session = {
            "timestamp": _timestamp(),
            "date": _today_date(),
            "stages": {},
        }

        # Stage 1: Build training pack
        pack_path = self.build_training_pack()
        if not pack_path:
            session["stages"]["build"] = {"ok": False, "reason": "insufficient_examples"}
            self.log_session(session)
            self.logger.info("Build failed; aborting cycle")
            return session

        session["stages"]["build"] = {"ok": True, "pack_path": str(pack_path)}

        # Stage 2: Run training
        training_result = self.run_training(pack_path)
        session["stages"]["training"] = training_result

        if not training_result.get("ok"):
            self.log_session(session)
            self.logger.warning("Training failed; aborting promotion")
            return session

        # Stage 3: Run evaluation
        eval_result = self.run_eval()
        session["stages"]["eval"] = eval_result

        # Stage 4: Promote if better
        promoted = self.promote_if_better(training_result, eval_result)
        session["stages"]["promotion"] = {"promoted": promoted}
        session["promoted"] = promoted

        # Carry top-level eval fields for dashboard
        session["eval_passed"] = eval_result.get("passed", 0)
        session["eval_total"] = eval_result.get("total", 0)
        session["examples_count"] = training_result.get("examples_count", 0)
        session["duration_seconds"] = training_result.get(
            "duration_seconds",
            training_result.get("duration_sec", 0),
        )

        # Update state
        self.state["last_run_date"] = _today_date()
        self.state["last_session"] = session
        self._save_state()

        # Log session
        self.log_session(session)

        # Stage 5: Run per-category benchmark and regenerate dashboard
        self._run_post_training_benchmark(training_result, promoted)

        self.logger.info(f"Cycle complete: promoted={promoted}")
        return session

    def _run_post_training_benchmark(self, training_result: dict, promoted: bool) -> None:
        """Run category benchmarks and regenerate the HTML dashboard."""
        try:
            import sys, os
            sys.path.insert(0, str(TRAINING_ROOT))
            from benchmark_tracker import run_full_benchmark, log_benchmark, get_latest
            from dashboard_generator import generate as generate_dashboard

            baseline = get_latest()
            adapter_path = training_result.get("adapter_path", "")
            record = run_full_benchmark(
                model_version=f"jarvis-{_today_date()}",
                adapter_path=str(adapter_path),
                baseline=baseline,
            )
            record["promoted"] = promoted
            log_benchmark(record)
            self.logger.info(f"Benchmark: overall={record.get('overall')} delta={record.get('delta_vs_baseline')}")
        except Exception as e:
            self.logger.warning(f"Benchmark run failed (non-fatal): {e}")

        try:
            from dashboard_generator import generate as generate_dashboard
            generate_dashboard()
        except Exception as e:
            self.logger.warning(f"Dashboard generation failed (non-fatal): {e}")

        # Stage 6: Auto-commit training artifacts to git
        self._auto_commit_artifacts(promoted)

        # Stage 7: macOS notification
        self._notify_training_complete(promoted)

    def _auto_commit_artifacts(self, promoted: bool) -> None:
        """
        Commit overnight training artifacts to git so history accumulates.

        Files committed:
          - training/overnight_log.jsonl   (session log)
          - training/benchmarks.jsonl      (benchmark history)
          - training/dashboard.html        (rendered dashboard)
          - training/overnight_state.json  (run state)
        """
        artifacts = [
            "training/overnight_log.jsonl",
            "training/benchmarks.jsonl",
            "training/dashboard.html",
            "training/overnight_state.json",
        ]
        try:
            # Only stage files that actually exist
            to_stage = [a for a in artifacts if (REPO_ROOT / a).exists()]
            if not to_stage:
                self.logger.info("Auto-commit: no artifact files found, skipping")
                return

            subprocess.run(
                ["git", "add", *to_stage],
                cwd=str(REPO_ROOT),
                check=True,
                capture_output=True,
                text=True,
            )

            promoted_tag = "promoted" if promoted else "no-promote"
            date_tag = _today_date()
            msg = f"chore(training): overnight artifacts {date_tag} [{promoted_tag}]"

            # Use a pathspec so Claude/Codex/user-staged work cannot be swept
            # into the scheduled artifact commit.
            result = subprocess.run(
                ["git", "commit", "-m", msg, "--", *to_stage],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self.logger.info(f"Auto-commit: artifacts committed — {msg}")
            elif "nothing to commit" in result.stdout + result.stderr:
                self.logger.info("Auto-commit: nothing new to commit, skipping")
            else:
                self.logger.warning(f"Auto-commit: git commit returned {result.returncode}: {result.stderr.strip()}")

        except Exception as e:
            self.logger.warning(f"Auto-commit failed (non-fatal): {e}")

    def _notify_training_complete(self, promoted: bool) -> None:
        """Send a macOS notification when the overnight training cycle finishes."""
        try:
            status_word = "✅ Adapter promoted" if promoted else "⚠️ Not promoted (no improvement)"
            script = (
                f'display notification "{status_word}" '
                f'with title "Jarvis Training Complete" '
                f'sound name "Glass"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.logger.info(f"macOS notification sent: promoted={promoted}")
        except Exception as e:
            self.logger.warning(f"macOS notification failed (non-fatal): {e}")


def run_if_scheduled() -> Optional[dict]:
    """
    Check if training should run. If yes, run it.

    Returns session dict or None.
    """
    trainer = OvernightTrainer()

    if not trainer.is_training_window():
        logger.debug("Not in training window")
        return None

    if not trainer.should_run_tonight():
        logger.debug("Already ran today")
        return None

    logger.info("Running overnight training")
    return trainer.run_full_cycle()


def status() -> dict:
    """Return status dict: last session, is_training_window, next scheduled time."""
    trainer = OvernightTrainer()
    now = datetime.now()

    # Calculate next scheduled run (11pm today or tomorrow)
    if now.hour < 23:
        next_run = now.replace(hour=23, minute=0, second=0, microsecond=0)
    else:
        next_run = (now + timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)

    return {
        "is_training_window": trainer.is_training_window(),
        "should_run_tonight": trainer.should_run_tonight(),
        "last_run_date": trainer.state.get("last_run_date"),
        "last_session": trainer.state.get("last_session"),
        "baseline_eval_passed": trainer.state.get("baseline_eval_passed"),
        "baseline_eval_total": trainer.state.get("baseline_eval_total"),
        "next_scheduled_run": next_run.isoformat(),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=2))
    else:
        session = run_if_scheduled()
        if session:
            print(json.dumps(session, indent=2))
        else:
            print(json.dumps(status(), indent=2))
