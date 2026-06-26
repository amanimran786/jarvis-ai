import atexit
import logging
import multiprocessing
import os
import signal
import sys
import traceback
import threading
import faulthandler
import shlex
import subprocess

# ── Clean shutdown — reap all multiprocessing children before exit ────────────
_SHUTDOWN_LOCK = threading.RLock()
_SHUTDOWN_DONE = False


def _reap_children() -> None:
    """Terminate any live multiprocessing child processes (resource_tracker, etc.)."""
    children = multiprocessing.active_children()
    for child in children:
        try:
            child.terminate()
        except Exception:
            logging.debug("[Main] child.terminate failed for %s", child.pid, exc_info=True)
    for child in children:
        try:
            child.join(timeout=1.5)
        except Exception:
            logging.debug("[Main] child.join failed for %s", child.pid, exc_info=True)
    # Hard-kill any that ignored SIGTERM
    for child in children:
        try:
            if child.is_alive():
                child.kill()
        except Exception:
            logging.debug("[Main] child.kill failed for %s", child.pid, exc_info=True)


def _shutdown_runtime(reason: str = "") -> None:
    """Stop Jarvis background services and child processes exactly once."""
    global _SHUTDOWN_DONE
    with _SHUTDOWN_LOCK:
        if _SHUTDOWN_DONE:
            return
        _SHUTDOWN_DONE = True

        for stop_call in (
            lambda: jarvis_watcher.stop(),
            lambda: brain_daemon.stop(),
            lambda: __import__("tunnel_manager").stop_tunnel(),
            lambda: __import__("brains.brain_ollama", fromlist=["stop_keepalive"]).stop_keepalive(),
        ):
            try:
                stop_call()
            except Exception:
                logging.debug("[Main] shutdown stop_call failed", exc_info=True)

        _reap_children()

        try:
            runtime_state.mark_stopped(reason)
        except Exception:
            logging.debug("[Main] runtime_state.mark_stopped failed", exc_info=True)


def _signal_shutdown(signum, frame) -> None:
    _shutdown_runtime(f"signal:{signum}")
    sys.exit(0)


atexit.register(_shutdown_runtime)
signal.signal(signal.SIGTERM, _signal_shutdown)
signal.signal(signal.SIGINT, _signal_shutdown)


# Fix Qt cocoa plugin path before any PyQt6 import.
# Conda sometimes writes a corrupted Qt path registry ("plug3ins" typo).
# Setting QT_PLUGIN_PATH explicitly bypasses it.
_pyqt6_plugins = os.path.join(
    os.path.dirname(sys.executable),
    "..", "lib",
    f"python{sys.version_info.major}.{sys.version_info.minor}",
    "site-packages", "PyQt6", "Qt6", "plugins",
)
_pyqt6_plugins = os.path.normpath(_pyqt6_plugins)
if os.path.isdir(_pyqt6_plugins):
    os.environ.setdefault("QT_PLUGIN_PATH", _pyqt6_plugins)
    _pyqt6_platforms = os.path.join(_pyqt6_plugins, "platforms")
    _qpa_path = os.getenv("QT_QPA_PLATFORM_PLUGIN_PATH", "")
    # Some environments persist a broken conda path ("plug3ins"), which makes
    # Qt abort before UI startup. Force a valid platforms directory here.
    if _qpa_path:
        normalized_qpa = os.path.normpath(_qpa_path)
        if "plug3ins" in normalized_qpa.lower() or not os.path.isdir(normalized_qpa):
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _pyqt6_platforms
    else:
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", _pyqt6_platforms)

import evals
import jarvis_daemon
import jarvis_watcher
import runtime_state
import brain_daemon


CRASH_LOG = str(runtime_state.crash_log_path())
_CRASH_STREAM = None


def _is_conda_python() -> bool:
    exe = (sys.executable or "").lower()
    return bool(os.getenv("CONDA_PREFIX")) or "anaconda" in exe or "miniconda" in exe or "/conda/" in exe


def _project_venv_python() -> str:
    return os.path.join(os.path.dirname(__file__), "venv", "bin", "python")


def _ensure_supported_gui_runtime() -> None:
    """Avoid hard Qt aborts when the GUI is launched from a conda interpreter.

    If the repo venv exists, transparently re-exec into it. Otherwise exit with a
    clear message instead of letting Qt abort on plugin initialization.
    """
    if getattr(sys, "frozen", False):
        return
    if "--no-ui" in sys.argv:
        return
    if "--console" in sys.argv:
        return
    if not _is_conda_python():
        return

    # Prevent infinite re-execution loops if venv/bin/python is also conda python
    _attempted_reexec = os.getenv("_JARVIS_GUI_REEXEC_ATTEMPTED", "").lower() in {"1", "true"}
    if _attempted_reexec:
        print("[Startup] Detected re-execution attempt with conda. Proceeding with current Python.")
        return

    target = _project_venv_python()
    current = os.path.realpath(sys.executable)
    target_real = os.path.realpath(target) if os.path.exists(target) else ""
    if target_real and current != target_real:
        print("[Startup] GUI launch requested from conda Python. Re-launching Jarvis with the project venv to avoid Qt plugin crashes...")
        env = os.environ.copy()
        env["_JARVIS_GUI_REEXEC_ATTEMPTED"] = "1"
        os.execve(target_real, [target_real] + sys.argv, env)

    if not target_real:
        raise SystemExit(
            "[Startup] Jarvis GUI should not be launched from conda on this machine. "
            "Use ./venv/bin/python main.py, python main.py --no-ui, or the packaged Jarvis.app."
        )


def _append_crash_log(label: str, exc_type, exc_value, exc_traceback) -> None:
    timestamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stack = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    message = f"[{timestamp}] {label}\n{stack}\n"
    try:
        with open(CRASH_LOG, "a", encoding="utf-8") as f:
            f.write(message)
    except OSError:
        pass
    try:
        evals.log_failure(
            issue=f"{label}: {exc_value}",
            response=stack[:1200],
            model="Process",
            source="runtime_crash",
        )
    except Exception:
        logging.debug("[Main] evals crash log write failed", exc_info=True)


def _install_crash_logging() -> None:
    global _CRASH_STREAM
    if _CRASH_STREAM is not None:
        return

    _CRASH_STREAM = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
    faulthandler.enable(_CRASH_STREAM, all_threads=True)

    def _handle_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        _append_crash_log("Unhandled main-thread exception", exc_type, exc_value, exc_traceback)
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def _handle_thread_exception(args):
        _append_crash_log(
            f"Unhandled thread exception in {getattr(args.thread, 'name', 'unknown')}",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
        )

    sys.excepthook = _handle_exception
    threading.excepthook = _handle_thread_exception


def _resolve_api_port() -> int:
    raw = os.getenv("JARVIS_API_PORT", "8765")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8765


def _resolve_api_host() -> str:
    # JARVIS_ALLOW_LAN=1 → listen on all interfaces so phone on same WiFi can reach the approval page
    if os.getenv("JARVIS_ALLOW_LAN", "").lower() in {"1", "true", "yes", "on"}:
        return "0.0.0.0"
    return os.getenv("JARVIS_API_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _run_headless():
    import briefing
    import google_services as gs
    import memory as mem
    import tools
    from local_runtime import local_stt
    from router import route_stream, set_timer_callback, record_turn as _record_turn
    from voice import speak, speak_stream, listen, wait_for_wake_word
    from desktop.cli_ui import CLISession, ThinkingIndicator
    jarvis_watcher.set_speak_callback(speak)
    brain_daemon.set_speak_callback(speak)   # wire TTS for calendar/email alerts

    cli = CLISession(use_rich=True)
    cli.print_header()

    # Show brain agent activity in status bar
    try:
        activity = brain_daemon.get_activity_summary()
        cli.agents_active = len([a for a in brain_daemon.status().get("agents", []) if a.get("status") == "active"])
    except Exception:
        activity = ""

    END_CONVERSATION = {"that's all", "that's it", "done", "thank you", "thanks", "stop listening"}
    QUIT_PHRASES = {"quit", "exit", "goodbye", "bye", "shut down"}
    WAKE_ACK = "I'm here. Go ahead."

    def on_timer_done(label):
        speak(f"Time's up. Your {label} timer is done.")

    def run_briefing(facts):
        try:
            speak(briefing.build_briefing(facts))
            speak(f"Weather: {tools.get_weather()}")
            speak(gs.get_todays_events())
            speak(gs.get_unread_emails(max_results=3))
        except Exception as e:
            cli.print_jarvis_message(f"Briefing error: {e}")

    def handle_memory(user_input):
        lower = user_input.lower().strip()
        if lower.startswith("remember "):
            fact = user_input[9:].strip()
            mem.add_fact(fact)
            speak(f"Got it. I'll remember that {fact}.")
            return True
        if lower.startswith("forget "):
            keyword = user_input[7:].strip()
            speak("Forgotten." if mem.forget(keyword) else f"Nothing saved about {keyword}.")
            return True
        if any(p in lower for p in ("give me a briefing", "catch me up", "what did i miss")):
            run_briefing(mem.list_facts())
            return True
        return False

    def conversation_loop():
        speak(WAKE_ACK)
        misses = 0
        while True:
            user_input = listen()
            if not user_input:
                misses += 1
                if misses >= 2:
                    return True
                continue
            misses = 0
            lower = user_input.lower().strip()
            if lower in QUIT_PHRASES:
                speak("Goodbye.")
                return False
            if lower in END_CONVERSATION:
                speak("Alright.")
                return True
            if len(lower.split()) == 1 and lower in {"um", "uh", "hm", "hmm", "ah", "oh", "er"}:
                continue
            if handle_memory(user_input):
                continue
            try:
                cli.print_user_message(user_input)
                with ThinkingIndicator(cli):
                    stream, model = route_stream(user_input)
                cli.current_model = model
                reply = cli.stream_jarvis_response(stream)
                # Speak the reply aloud too
                speak(reply)
                _record_turn(user_input, reply)
                # Refresh brain activity count in status bar
                try:
                    cli.agents_active = len([
                        a for a in brain_daemon.status().get("agents", [])
                        if a.get("status") == "active"
                    ])
                    cli.print_status_bar()
                except Exception:
                    logging.debug("[Main] status bar update failed", exc_info=True)
            except Exception:
                traceback.print_exc()
                speak("Sorry, something went wrong.")

    set_timer_callback(on_timer_done)
    stt_status = local_stt.status()
    if stt_status.get("active_engine") == "unavailable":
        reason = stt_status.get("import_error") or "No speech-to-text backend is available."
        cli.print_jarvis_message(f"Voice input unavailable: {reason}")
        cli.print_jarvis_message("Tip: Launch with ./venv/bin/python main.py for GUI mode.")
        return

    if "--no-mic" in sys.argv or os.getenv("JARVIS_NO_MIC") == "1":
        import time
        cli.print_jarvis_message("Microphone voice loop is disabled (API-only mode active).")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return

    speak("Online.")
    cli.print_status_bar()
    while True:
        wait_for_wake_word()
        if not conversation_loop():
            break


def _request_macos_permissions():
    print("[System] Startup app-permission probing is disabled to avoid opening user apps on launch.")
    print("[System] Grant permissions only when a feature first needs them: microphone, camera, screen recording, Contacts, Messages, or browser automation.")


def _run_deferred_startup_tasks() -> None:
    # Pre-warm faster-whisper so the first voice query has zero cold-start latency
    try:
        from local_runtime import local_stt
        local_stt.preload()
    except Exception:
        logging.debug("[Main] STT preload failed", exc_info=True)

    # Pin the default local model in Ollama RAM — eliminates 20-40s cold-load latency
    try:
        from brains.brain_ollama import start_keepalive, get_best_available
        from config import LOCAL_DEFAULT
        model = get_best_available(LOCAL_DEFAULT)
        start_keepalive(model)
    except Exception:
        logging.debug("[Main] Ollama keepalive startup failed", exc_info=True)

    # Pre-render common TTS phrases so acknowledgements play instantly
    try:
        try:
            from local_runtime.local_kokoro_subprocess_tts import prewarm_phrase_cache
        except Exception:
            from local_runtime.local_kokoro_tts import prewarm_phrase_cache
        prewarm_phrase_cache()
    except Exception:
        logging.debug("[Main] TTS phrase cache prewarm failed", exc_info=True)

    request_permissions = os.getenv("JARVIS_REQUEST_STARTUP_PERMISSIONS", "").lower() in {"1", "true", "yes", "on"}
    if request_permissions:
        try:
            _request_macos_permissions()
        except Exception:
            traceback.print_exc()

    request_admin = os.getenv("JARVIS_REQUEST_STARTUP_ADMIN", "").lower() in {"1", "true", "yes", "on"}
    if request_admin:
        try:
            import terminal

            print("[System] Requesting administrative access for this session...")
            terminal.run_admin_command("echo 'Jarvis Administrator Privileges Granted'")
        except Exception:
            traceback.print_exc()


def _start_deferred_startup_tasks() -> None:
    threading.Thread(
        target=_run_deferred_startup_tasks,
        daemon=True,
        name="JarvisStartupSetup",
    ).start()


def _start_harness_heartbeat() -> None:
    """Beat ORCHESTRATOR_STATUS.json every 30 s via harness/loop.py."""
    import time

    def _beat() -> None:
        from harness.loop import heartbeat
        while True:
            try:
                heartbeat("Jarvis runtime active")
            except Exception:
                logging.debug("[Main] harness heartbeat error", exc_info=True)
            time.sleep(30)

    threading.Thread(target=_beat, daemon=True, name="JarvisHarnessHeartbeat").start()


def _interactive_console_command() -> str:
    api_base = ""
    api_token = ""
    try:
        discovered = runtime_state.read_api_endpoint() or runtime_state.discover_api_endpoint() or {}
        api_base = str(discovered.get("base_url") or "").strip()
        api_token = str(discovered.get("token") or os.getenv("JARVIS_API_TOKEN", "")).strip()
    except Exception:
        logging.debug("[Main] API endpoint discovery failed", exc_info=True)

    exports = []
    if api_base:
        exports.append(f"export JARVIS_API_BASE_URL={shlex.quote(api_base)}")
    if api_token:
        exports.append(f"export JARVIS_API_TOKEN={shlex.quote(api_token)}")
    exports.append("export JARVIS_CONSOLE_ATTACHED=1")

    if getattr(sys, "frozen", False):
        runner = f"{shlex.quote(sys.executable)} --console"
    else:
        runner = f"{shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))} --console"
    prefix = " && ".join(exports)
    return f"{prefix} && {runner}" if prefix else runner


def _interactive_console_already_running() -> bool:
    try:
        session = runtime_state.read_console_session() or {}
        if session.get("alive"):
            return True
    except Exception:
        logging.debug("[Main] console session read failed", exc_info=True)
    try:
        result = subprocess.run(
            ["pgrep", "-fal", "--", "--console"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        logging.debug("[Main] pgrep --console check failed", exc_info=True)
    try:
        result = subprocess.run(
            ["pgrep", "-fal", "jarvis_cli.py --interactive"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    except Exception:
        logging.debug("[Main] pgrep jarvis_cli check failed", exc_info=True)
    return False


def _ensure_terminal_console_connected() -> None:
    if "--no-ui" in sys.argv or "--console" in sys.argv:
        return
    if sys.platform != "darwin":
        return
    if os.getenv("JARVIS_DISABLE_AUTO_CONSOLE", "").lower() in {"1", "true", "yes", "on"}:
        return
    if _interactive_console_already_running():
        return
    try:
        import terminal
        terminal.run_command_in_terminal_app(_interactive_console_command(), cwd=os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        traceback.print_exc()

def _run():
    _ensure_supported_gui_runtime()
    _install_crash_logging()
    api_host = _resolve_api_host()
    api_port = _resolve_api_port()

    # Enable teacher capture by default so cloud responses are automatically
    # recorded as training examples for the local model fine-tuning pipeline.
    os.environ.setdefault("JARVIS_TEACHER_CAPTURE", "1")

    jarvis_daemon.start_daemon(host=api_host, port=api_port)
    jarvis_watcher.start()
    brain_daemon.start()

    import task_runtime as _task_runtime
    _task_runtime.bootstrap()
    _start_harness_heartbeat()

    _start_deferred_startup_tasks()

    if "--console" in sys.argv:
        from jarvis_cli import run_interactive_console
        sys.exit(run_interactive_console())

    if "--no-ui" in sys.argv:
        _run_headless()
        return

    _ensure_terminal_console_connected()

    from ui import run
    run()


if __name__ == "__main__":
    # Required for frozen app builds (PyInstaller) so multiprocessing child
    # processes do not re-enter full application startup.
    multiprocessing.freeze_support()
    try:
        _run()
    except KeyboardInterrupt:
        _shutdown_runtime("keyboard_interrupt")
        raise SystemExit(0)
