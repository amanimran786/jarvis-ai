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

    def build_training_pack(self) -> Optional[Path]:
        """
        Read conversation history from memory.json, format as JSONL.

        Returns path to overnight_{date}.jsonl or None if <10 examples.
        Format: {"prompt": "User: <msg>\n\nJarvis:", "completion": " <response>"}
        """
        if not MEMORY_FILE.exists():
            self.logger.warning(f"Memory file not found: {MEMORY_FILE}")
            return None

        try:
            with MEMORY_FILE.open("r", encoding="utf-8") as f:
                memory = json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load memory: {e}")
            return None

        # Extract conversation history (list of dicts with 'date' and 'summary' fields)
        convos = memory.get("conversation_history", [])
        if not convos:
            self.logger.info("No conversation history found")
            return None

        # For now, we use summaries as synthetic data.
        # In production, integrate with usage_log.jsonl for real prompt/completion pairs.
        examples = []
        for convo in convos:
            summary = (convo.get("summary") or "").strip()
            if summary:
                # Synthetic example: user asked something, Jarvis responded with summary
                examples.append({
                    "prompt": f"User: Tell me about {summary}\n\nJarvis:",
                    "completion": f" {summary}"
                })

        if len(examples) < 10:
            self.logger.info(f"Only {len(examples)} examples; need >=10")
            return None

        # Save to overnight_{date}.jsonl
        today = _today_date()
        pack_path = PACKS_DIR / f"overnight_{today}.jsonl"

        try:
            with pack_path.open("w", encoding="utf-8") as f:
                for ex in examples:
                    f.write(json.dumps(ex, ensure_ascii=False) + "\n")
            self.logger.info(f"Built training pack: {pack_path} ({len(examples)} examples)")
            return pack_path
        except Exception as e:
            self.logger.error(f"Failed to save pack: {e}")
            return None

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

            result = subprocess.run(
                ["git", "commit", "-m", msg],
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
