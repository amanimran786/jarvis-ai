import os
import sys
import traceback
import threading
import faulthandler

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

import evals
import jarvis_daemon
import runtime_state


CRASH_LOG = str(runtime_state.crash_log_path())
_CRASH_STREAM = None


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


def _run_headless():
    import briefing
    import google_services as gs
    import memory as mem
    import tools
    from router import route_stream, set_timer_callback
    from voice import speak, speak_stream, listen, wait_for_wake_word

    END_CONVERSATION = {"that's all", "that's it", "done", "thank you", "thanks", "stop listening"}
    QUIT_PHRASES = {"quit", "exit", "goodbye", "bye", "shut down"}

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
        speak("Yes?")
        misses = 0
        while True:
            user_input = listen()
            if not user_input:
                misses += 1
                if misses >= 2:
                    return True
                speak("Still here.")
                continue
            misses = 0
            lower = user_input.lower().strip()
            if lower in QUIT_PHRASES:
                speak("Goodbye.")
                return False
            if lower in END_CONVERSATION:
                speak("Alright.")
                return True
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
    speak("Online.")
    while True:
        wait_for_wake_word()
        if not conversation_loop():
            break


def _request_macos_permissions():
    import subprocess
    import os
    print("[System] Requesting macOS Accessibility and Automation permissions for Python and Terminal...")
    
    targets = [
        'tell application "System Events" to get name of current user',
        'tell application "Messages" to get name of first service',
        'tell application "Mail" to get name of first account',
        'tell application "Calendar" to get name of first calendar',
        'tell application "Contacts" to get name of first person',
        'tell application "Terminal" to get windows'
    ]
    
    # Trigger requests for the current python process
    for cmd in targets:
        subprocess.run(["osascript", "-e", cmd], capture_output=True)
        
    # Trigger requests for the Terminal app by having it run osascript
    term_script = " && ".join([f"osascript -e '{cmd}'" for cmd in targets])
    term_script += " && exit"
    apple_script = f'tell application "Terminal" to do script "{term_script}"'
    subprocess.run(["osascript", "-e", apple_script], capture_output=True)


def _run_deferred_startup_tasks() -> None:
    request_permissions = os.getenv("JARVIS_REQUEST_STARTUP_PERMISSIONS", "").lower() in {"1", "true", "yes", "on"}
    if request_permissions:
        try:
            _request_macos_permissions()
        except Exception:
            traceback.print_exc()

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

def _run():
    _install_crash_logging()
    api_host = os.getenv("JARVIS_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    api_port = _resolve_api_port()

    jarvis_daemon.start_daemon(host=api_host, port=api_port)
    _start_deferred_startup_tasks()

    if "--no-ui" in sys.argv:
        _run_headless()
        return

    from ui import run
    run()


if __name__ == "__main__":
    _run()
