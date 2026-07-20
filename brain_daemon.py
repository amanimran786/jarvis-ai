"""
Jarvis brain daemon — background agents that continuously improve the vault.

Runs as daemon threads inside the Jarvis process. Each agent loop fires
independently on its own schedule. Starts automatically from main.py.

Agents:
  - MemoryAgent (every 30min): consolidates working memory, dedupes facts
  - VaultSynthAgent (every 2hrs): searches vault, updates brain indexes
  - KnowledgeFeedAgent (every 4hrs): refreshes knowledge feed topics
  - EvalAgent (every 8hrs): runs golden case evals, logs delta
  - TrainingPackAgent (every 24hrs at midnight): builds training packs from recent conversations
"""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from abc import ABC, abstractmethod

import runtime_state

logger = logging.getLogger("brain_daemon")


def _sync_repo_map_metadata(repo_map: Path, synced_at: datetime) -> None:
    """Update Repo Map metadata without damaging Obsidian frontmatter."""
    content = repo_map.read_text(encoding="utf-8")
    lines = content.splitlines()

    # Repair older daemon damage where the opening YAML delimiter was replaced
    # with "# Repo Map (synced ...)" but the closing delimiter remained.
    if lines and lines[0].startswith("# Repo Map (synced ") and "---" in lines[1:]:
        lines[0] = "---"
        content = "\n".join(lines)
        if repo_map.read_text(encoding="utf-8").endswith("\n"):
            content += "\n"

    if not content.startswith("---\n"):
        return

    parts = content.split("---\n", 2)
    if len(parts) != 3:
        return

    metadata = parts[1].splitlines()
    updated = synced_at.strftime("%Y-%m-%d")
    for idx, line in enumerate(metadata):
        if line.startswith("updated:"):
            metadata[idx] = f"updated: {updated}"
            break
    else:
        metadata.append(f"updated: {updated}")

    repo_map.write_text("---\n" + "\n".join(metadata) + "\n---\n" + parts[2], encoding="utf-8")


class BrainAgent(ABC):
    """Base class for background brain agents."""

    def __init__(self, name: str, interval_seconds: int):
        self.name = name
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._last_run = None
        self._run_count = 0
        self._thread = None

    @abstractmethod
    def run_once(self) -> dict:
        """
        Run one iteration of the agent.

        Returns:
            dict with keys: {"ok": bool, "summary": str, "duration_ms": int}
        """
        pass

    def start(self) -> None:
        """Start daemon thread running the agent loop."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning(f"Agent {self.name} is already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.debug(f"Started agent: {self.name}")

    def stop(self) -> None:
        """Stop the agent loop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.debug(f"Stopped agent: {self.name}")

    def _loop(self) -> None:
        """Main loop: sleep interval, run_once, handle exceptions."""
        while not self._stop_event.is_set():
            try:
                if self._stop_event.wait(self.interval_seconds):
                    break

                start_time = time.time()
                result = self.run_once()
                duration_ms = int((time.time() - start_time) * 1000)

                self._run_count += 1
                self._last_run = datetime.now()

                ok = result.get("ok", False)
                summary = result.get("summary", "no summary")
                level = logging.INFO if ok else logging.WARNING
                logger.log(
                    level,
                    f"[{self.name}] run #{self._run_count}: {summary} ({duration_ms}ms)",
                )
            except Exception as e:
                logger.exception(f"[{self.name}] unhandled exception in loop:")

    def status(self) -> dict:
        """Return current status of the agent."""
        next_run = None
        if self._last_run is not None:
            next_run = (
                self._last_run.timestamp() + self.interval_seconds
            )
        return {
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "last_run": self._last_run.isoformat() if self._last_run else None,
            "run_count": self._run_count,
            "next_run": next_run,
        }


class MemoryAgent(BrainAgent):
    """Consolidates working memory and dedupes facts every 30min."""

    def __init__(self):
        super().__init__("MemoryAgent", 1800)  # 30 minutes

    def run_once(self) -> dict:
        try:
            import memory

            # Consolidate memory (builds working_memory and long_term_profile)
            consolidate_result = memory.consolidate_memory()

            # Load, dedupe facts, and save if changed
            data = memory.load()
            original_facts = data.get("facts", [])
            deduped_facts = memory._dedupe_keep_order(original_facts)

            facts_deduped = len(original_facts) - len(deduped_facts)
            if facts_deduped > 0:
                data["facts"] = deduped_facts
                memory.save(data)

            summary = (
                f"Consolidated {len(original_facts)} facts, deduped {facts_deduped} duplicates"
            )
            return {"ok": True, "summary": summary, "duration_ms": 0}
        except Exception as e:
            return {
                "ok": False,
                "summary": f"Memory consolidation failed: {str(e)[:80]}",
                "duration_ms": 0,
            }


class VaultSynthAgent(BrainAgent):
    """Searches vault, updates brain indexes every 2 hours."""

    def __init__(self):
        super().__init__("VaultSynthAgent", 7200)  # 2 hours

    def run_once(self) -> dict:
        try:
            import vault

            vault_root = vault.VAULT_ROOT
            brain_dir = vault_root / "wiki" / "brain"

            if not brain_dir.exists():
                return {
                    "ok": False,
                    "summary": "Vault brain directory not found",
                    "duration_ms": 0,
                }

            # Search for decision/learned/insight in brain files
            cmd = [
                "rg",
                "-n",
                "--hidden",
                "-g",
                "!.git",
                "decision|learned|insight",
                str(brain_dir),
                "--count-matches",
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )

            # Count total matches
            match_count = 0
            for line in result.stdout.split("\n"):
                if ":" in line:
                    try:
                        count = int(line.split(":")[-1])
                        match_count += count
                    except (ValueError, IndexError):
                        pass

            # Count brain files
            brain_files = list(brain_dir.glob("*.md"))
            file_count = len(brain_files)

            # Check if brain files were modified in last 2 hours
            now = datetime.now().timestamp()
            recently_modified = False
            for f in brain_files:
                mtime = f.stat().st_mtime
                if now - mtime < 7200:  # 2 hours
                    recently_modified = True
                    break

            # Update Repo Map header with timestamp if brain changed recently
            repo_map = vault_root / "indexes" / "Repo Map.md"
            if recently_modified and repo_map.exists():
                _sync_repo_map_metadata(repo_map, datetime.now())

            summary = f"Brain indexed: {file_count} files, {match_count} key matches"
            return {"ok": True, "summary": summary, "duration_ms": 0}

        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "summary": "Vault search timeout",
                "duration_ms": 0,
            }
        except Exception as e:
            return {
                "ok": False,
                "summary": f"Vault synthesis failed: {str(e)[:80]}",
                "duration_ms": 0,
            }


class KnowledgeFeedAgent(BrainAgent):
    """Refreshes knowledge feed topics every 4 hours."""

    def __init__(self):
        super().__init__("KnowledgeFeedAgent", 14400)  # 4 hours

    def run_once(self) -> dict:
        try:
            from learner import _refresh_knowledge_feed

            _refresh_knowledge_feed()
            return {
                "ok": True,
                "summary": "Knowledge feed refreshed",
                "duration_ms": 0,
            }
        except Exception as e:
            error_msg = str(e)[:80]
            # Silently skip on network/import errors (learner may fail without internet)
            return {
                "ok": False,
                "summary": f"Knowledge feed skipped: {error_msg}",
                "duration_ms": 0,
            }


class EvalAgent(BrainAgent):
    """
    Runs full benchmark suite every 8 hours and logs results.

    Uses benchmark_tracker.run_full_benchmark() for consistent per-category
    results rather than the golden-cases file (which requires a live model).
    Results are appended to training/eval_history.jsonl.
    """

    def __init__(self):
        super().__init__("EvalAgent", 28800)  # 8 hours

    def run_once(self) -> dict:
        try:
            import sys as _sys
            repo = Path(__file__).parent
            training_dir = runtime_state.writable_data_path(
                "training",
                seed_from=repo / "training",
            )
            training_path = str(training_dir)
            inserted_path = False
            if training_path not in _sys.path:
                _sys.path.insert(0, training_path)
                inserted_path = True
            try:
                from benchmark_tracker import run_full_benchmark, get_latest

                baseline = get_latest()
                record = run_full_benchmark(
                    model_version=f"daemon-eval-{datetime.now().strftime('%Y%m%d-%H')}",
                    adapter_path="",
                    baseline=baseline,
                )
            finally:
                if inserted_path:
                    try:
                        _sys.path.remove(training_path)
                    except ValueError:
                        pass

            passed = record.get("total_passed", 0)
            total = record.get("total_tests", 0)
            overall = record.get("overall")
            skipped = [c for c, d in record.get("categories", {}).items() if d.get("skipped")]

            # Log to eval_history.jsonl
            eval_log_path = training_dir / "eval_history.jsonl"
            eval_log_path.parent.mkdir(parents=True, exist_ok=True)

            eval_entry = {
                "timestamp": datetime.now().isoformat(),
                "passed": passed,
                "failed": max(0, total - passed),
                "total": total,
                "overall": overall,
                "skipped_categories": skipped,
                "delta": record.get("delta_vs_baseline"),
            }

            with open(eval_log_path, "a") as f:
                f.write(json.dumps(eval_entry) + "\n")

            pct = f"{overall * 100:.1f}%" if overall is not None else "?"
            summary = f"EvalAgent: {passed}/{total} ({pct})"
            if skipped:
                summary += f" [{len(skipped)} skipped]"

            return {"ok": total > 0, "summary": summary, "duration_ms": 0}

        except Exception as e:
            return {
                "ok": False,
                "summary": f"EvalAgent failed: {str(e)[:80]}",
                "duration_ms": 0,
            }


class TrainingPackAgent(BrainAgent):
    """Builds training packs from recent conversations every 24 hours (0-6am)."""

    def __init__(self):
        super().__init__("TrainingPackAgent", 86400)  # 24 hours

    def run_once(self) -> dict:
        # Only run between midnight and 6am
        hour = datetime.now().hour
        if not (0 <= hour < 6):
            return {
                "ok": True,
                "summary": f"Training pack skipped (hour={hour}, need 0-6)",
                "duration_ms": 0,
            }

        try:
            from local_runtime.local_finetune_scheduler import OvernightTrainer

            trainer = OvernightTrainer()
            pack_path = trainer.build_training_pack()

            summary = f"Training pack built: {pack_path}"
            return {"ok": True, "summary": summary, "duration_ms": 0}

        except ModuleNotFoundError:
            return {
                "ok": False,
                "summary": "TrainingPackAgent: local_finetune_scheduler not available",
                "duration_ms": 0,
            }
        except Exception as e:
            return {
                "ok": False,
                "summary": f"Training pack failed: {str(e)[:80]}",
                "duration_ms": 0,
            }


# ── Proactive notification helpers ────────────────────────────────────────────

_speak_callback = None  # set by main.py via set_speak_callback(speak)


def set_speak_callback(fn) -> None:
    """Wire TTS so CalendarAlert and EmailAlert can speak aloud."""
    global _speak_callback
    _speak_callback = fn


def _speak(text: str) -> None:
    """Speak via TTS if wired, else log."""
    if _speak_callback:
        try:
            _speak_callback(text)
        except Exception as exc:
            logger.warning("[BrainDaemon] TTS error: %s", exc)
    else:
        logger.info("[BrainDaemon] alert (no TTS): %s", text)


def _notify(title: str, message: str) -> None:
    """Send a macOS notification banner as a non-blocking fire-and-forget."""
    try:
        safe_title = json.dumps(str(title))
        safe_message = json.dumps(str(message))
        subprocess.Popen(
            [
                "osascript",
                "-e",
                f"display notification {safe_message} with title {safe_title} sound name \"Glass\"",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        logger.debug("[BrainDaemon] osascript notify failed: %s", exc)


class CalendarAlertAgent(BrainAgent):
    """Every 5 minutes: alert once when a calendar event is ≤15 min away."""

    def __init__(self):
        super().__init__("CalendarAlert", 300)   # 5 minutes
        self._alerted_ids: set[str] = set()

    def run_once(self) -> dict:
        try:
            import google_services as gs
            from datetime import timezone, timedelta
            events_text = gs.get_todays_events()
            if not events_text or "unavailable" in events_text.lower():
                return {"ok": True, "summary": "calendar unavailable, skipped"}

            # Parse events — get_todays_events() returns plain text; use the
            # raw calendar API directly for structured data when available.
            try:
                service = gs._get_calendar_service()
                now_utc = datetime.now(timezone.utc)
                window_end = now_utc + timedelta(minutes=15)
                result = service.events().list(
                    calendarId="primary",
                    timeMin=now_utc.isoformat(),
                    timeMax=window_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=5,
                ).execute()
                items = result.get("items", [])
            except Exception:
                items = []

            alerted = 0
            for event in items:
                event_id = event.get("id", "")
                summary = event.get("summary", "Untitled event")
                start = event.get("start", {})
                # Skip all-day events (they have "date" not "dateTime")
                if "dateTime" not in start:
                    continue
                if event_id in self._alerted_ids:
                    continue
                from datetime import datetime as _dt
                start_dt = _dt.fromisoformat(start["dateTime"])
                if start_dt.tzinfo is None:
                    from datetime import timezone as _tz
                    start_dt = start_dt.replace(tzinfo=_tz.utc)
                mins_away = int((start_dt - now_utc).total_seconds() / 60)
                if mins_away < 0:
                    continue
                label = f"in {mins_away} minute{'s' if mins_away != 1 else ''}" if mins_away > 0 else "now"
                msg = f"Heads up — {summary} starts {label}."
                _speak(msg)
                _notify("Jarvis — Calendar", msg)
                self._alerted_ids.add(event_id)
                alerted += 1

            return {"ok": True, "summary": f"alerted {alerted} events"}
        except Exception as exc:
            return {"ok": False, "summary": f"error: {exc}"}


class EmailAlertAgent(BrainAgent):
    """Every 30 minutes: speak new unread emails since last check."""

    def __init__(self):
        super().__init__("EmailAlert", 1800)   # 30 minutes
        self._seen_ids: set[str] = set()
        self._first_run: bool = True   # don't alert on the very first poll (startup)

    def run_once(self) -> dict:
        try:
            import google_services as gs
            emails = gs.get_unread_emails(max_results=10)
            if not emails or (isinstance(emails, str) and "unavailable" in emails.lower()):
                return {"ok": True, "summary": "email unavailable, skipped"}

            # get_unread_emails returns formatted text; use raw API for IDs
            try:
                service = gs._get_gmail_service()
                resp = service.users().messages().list(
                    userId="me", labelIds=["INBOX", "UNREAD"], maxResults=10
                ).execute()
                messages = resp.get("messages", [])
            except Exception:
                messages = []

            if self._first_run:
                # Seed seen IDs without alerting
                self._seen_ids = {m["id"] for m in messages}
                self._first_run = False
                return {"ok": True, "summary": f"seeded {len(self._seen_ids)} message IDs"}

            new_msgs = [m for m in messages if m["id"] not in self._seen_ids]
            if not new_msgs:
                return {"ok": True, "summary": "no new emails"}

            # Fetch sender names for new messages
            senders = []
            for m in new_msgs[:3]:
                try:
                    detail = service.users().messages().get(
                        userId="me", id=m["id"], format="metadata",
                        metadataHeaders=["From"]
                    ).execute()
                    headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
                    from_raw = headers.get("From", "Unknown")
                    # Extract just the name part: "Alice <alice@x.com>" → "Alice"
                    import re
                    name_match = re.match(r'^([^<"]+)', from_raw)
                    senders.append(name_match.group(1).strip() if name_match else from_raw.split("@")[0])
                except Exception:
                    logging.debug("[BrainDaemon] silent failure in run_once", exc_info=True)

            count = len(new_msgs)
            if senders:
                names = ", ".join(senders[:3])
                suffix = " and others" if count > 3 else ""
                msg = f"You have {count} new email{'s' if count != 1 else ''} from {names}{suffix}."
            else:
                msg = f"You have {count} new email{'s' if count != 1 else ''}."

            _speak(msg)
            _notify("Jarvis — Email", msg)
            self._seen_ids.update(m["id"] for m in new_msgs)
            return {"ok": True, "summary": f"alerted {count} new emails"}
        except Exception as exc:
            return {"ok": False, "summary": f"error: {exc}"}


class BrainDaemon:
    """Manages all brain agents."""

    def __init__(self):
        self.agents = [
            MemoryAgent(),
            VaultSynthAgent(),
            KnowledgeFeedAgent(),
            EvalAgent(),
            TrainingPackAgent(),
            CalendarAlertAgent(),
            EmailAlertAgent(),
        ]
        self._running = False

    def start(self) -> None:
        """Start all agents."""
        if self._running:
            logger.warning("BrainDaemon is already running")
            return

        for agent in self.agents:
            agent.start()

        self._running = True
        logger.info("BrainDaemon started with %s agents", len(self.agents))

    def stop(self) -> None:
        """Stop all agents."""
        for agent in self.agents:
            agent.stop()

        self._running = False
        logger.info("BrainDaemon stopped")

    def status(self) -> dict:
        """Return status of all agents."""
        return {
            "running": self._running,
            "agents": [agent.status() for agent in self.agents],
        }

    def is_running(self) -> bool:
        """Check if daemon is active."""
        return self._running


# Module-level daemon instance
_daemon: BrainDaemon | None = None


def start() -> BrainDaemon:
    """Create and start the brain daemon."""
    global _daemon
    if _daemon is not None:
        logger.warning("brain_daemon.start() called but daemon already exists")
        if not _daemon.is_running():
            _daemon.start()
        return _daemon

    _daemon = BrainDaemon()
    _daemon.start()
    return _daemon


def stop() -> None:
    """Stop the brain daemon."""
    global _daemon
    if _daemon is not None:
        _daemon.stop()


def status() -> dict:
    """Get daemon status."""
    if _daemon is None:
        return {"running": False}
    return _daemon.status()


def get_activity_summary() -> str:
    """
    Human-readable summary for UI status bar.

    Example: "Brain: memory consolidated 5min ago · eval 2hrs ago · vault synced 47min ago"
    """
    if _daemon is None or not _daemon.is_running():
        return "Brain: offline"

    summaries = []
    now = datetime.now()

    for agent in _daemon.agents:
        st = agent.status()
        last_run = st.get("last_run")
        if not last_run:
            continue

        last_run_dt = datetime.fromisoformat(last_run)
        delta = now - last_run_dt
        total_seconds = int(delta.total_seconds())

        if total_seconds < 60:
            time_str = f"{total_seconds}s ago"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            time_str = f"{minutes}m ago"
        else:
            hours = total_seconds // 3600
            time_str = f"{hours}h ago"

        # Short name
        name_short = agent.name.replace("Agent", "").lower()
        summaries.append(f"{name_short} {time_str}")

    if not summaries:
        return "Brain: waiting for agents"

    return "Brain: " + " · ".join(summaries)


# TO INTEGRATE: Add to main.py startup:
#   import brain_daemon
#   brain_daemon.start()
# Add to shutdown:
#   brain_daemon.stop()
