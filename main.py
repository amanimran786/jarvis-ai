import atexit
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
def _reap_children() -> None:
    """Terminate any live multiprocessing child processes (resource_tracker, etc.)."""
    children = multiprocessing.active_children()
    for child in children:
        try:
            child.terminate()
        except Exception:
            pass
    for child in children:
        try:
            child.join(timeout=1.5)
        except Exception:
            pass
    # Hard-kill any that ignored SIGTERM
    for child in children:
        try:
            if child.is_alive():
                child.kill()
        except Exception:
            pass


def _signal_shutdown(signum, frame) -> None:
    _reap_children()
    sys.exit(0)


atexit.register(_reap_children)
signal.signal(signal.SIGTERM, _signal_shutdown)
# SIGINT already raises KeyboardInterrupt which unwinds normally through atexit.


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
import runtime_state


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
        pass


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
    from router import route_stream, set_timer_callback
    from voice import speak, speak_stream, listen, wait_for_wake_word

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
            print(f"[Briefing Error] {e}")

    def handle_memory(user_input):
        lower = user_input.lower().strip()
        if lower.startswith("remember "):
            fact = user_input[9:].strip()
            mem.add_fact(fact)
            speak(f"Got it. I'll remember that {fact}.")
            return True
        if lower.startswith("forget "):
            keyword = user_input[7:].strip()
            speak(f"Forgotten." if mem.forget(keyword) else f"Nothing saved about {keyword}.")
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
                # Don't speak on first miss — just wait silently so ambient noise
                # doesn't cause Jarvis to constantly interject "Still here."
                continue
            misses = 0
            lower = user_input.lower().strip()
            if lower in QUIT_PHRASES:
                speak("Goodbye.")
                return False
            if lower in END_CONVERSATION:
                speak("Alright.")
                return True
            # Ignore single-word noise captures ("um", "uh", "hm") without responding
            if len(lower.split()) == 1 and lower in {"um", "uh", "hm", "hmm", "ah", "oh", "er"}:
                continue
            if handle_memory(user_input):
                continue
            try:
                stream, model = route_stream(user_input)
                print(f"[{model}]")
                speak_stream(stream)
            except Exception:
                traceback.print_exc()
                speak("Sorry, something went wrong.")

    set_timer_callback(on_timer_done)
    stt_status = local_stt.status()
    if stt_status.get("active_engine") == "unavailable":
        reason = stt_status.get("import_error") or "No speech-to-text backend is available."
        print(f"[Voice] Voice input unavailable: {reason} Launch with ./venv/bin/python main.py")
        return
    speak("Online.")
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
        pass

    # Pin the default local model in Ollama RAM — eliminates 20-40s cold-load latency
    try:
        from brains.brain_ollama import start_keepalive, get_best_available
        from config import LOCAL_DEFAULT
        model = get_best_available(LOCAL_DEFAULT)
        start_keepalive(model)
    except Exception:
        pass

    # Pre-render common TTS phrases so acknowledgements play instantly
    try:
        try:
            from local_runtime.local_kokoro_subprocess_tts import prewarm_phrase_cache
        except Exception:
            from local_runtime.local_kokoro_tts import prewarm_phrase_cache
        prewarm_phrase_cache()
    except Exception:
        pass

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


def _interactive_console_command() -> str:
    api_base = ""
    api_token = ""
    try:
        discovered = runtime_state.read_api_endpoint() or runtime_state.discover_api_endpoint() or {}
        api_base = str(discovered.get("base_url") or "").strip()
        api_token = str(discovered.get("token") or os.getenv("JARVIS_API_TOKEN", "")).strip()
    except Exception:
        pass

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
        pass
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
        pass
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
        pass
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

    jarvis_daemon.start_daemon(host=api_host, port=api_port)
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
    _run()
