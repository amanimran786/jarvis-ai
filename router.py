"""
Jarvis Router — intent detection and tool dispatch.

Architecture:
  1. Fast-path: unambiguous commands (timer, volume, etc.) skip the LLM entirely
  2. Hardware: registered device commands checked before orchestration
  3. Orchestrator: Haiku classifies intent in ~300ms → dispatches right tool
  4. Fallback: smart_stream (model_router) for pure conversation

The orchestrator replaces the old regex wall. New tools need only an entry
in orchestrator.TOOLS — no regex patterns to write.
"""

import re
import os
import sys
import threading
import tools
import terminal
import notes
import google_services as gs
import camera
import memory as mem
import self_improve as si
import hardware as hw
import messages as msg
from model_router import smart_stream, format_with_mini
from config import OPUS, SONNET
from brain_claude import ask_claude_stream

_on_timer_done = None


def set_timer_callback(fn):
    global _on_timer_done
    _on_timer_done = fn


def _s(text: str):
    return iter([text])


def _parse_timer(text: str):
    match = re.search(r"(\d+)\s*(second|minute|hour)s?", text, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
    return seconds, f"{amount} {unit}{'s' if amount > 1 else ''}"


def _parse_app(text: str):
    match = re.search(r"\b(?:open|launch|start)\b\s+(?:up\s+)?(?:the\s+)?(?:my\s+)?(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_volume(text: str):
    match = re.search(r"(\d+)(?:\s*%)?", text)
    return int(match.group(1)) if match else None


# ── Main entry ────────────────────────────────────────────────────────────────

def route_stream(user_input: str) -> tuple:
    lower = user_input.lower().strip()
    mem.track_topic(lower)

    # ── 1. Fast-path: zero-latency unambiguous commands ───────────────────────

    # Timer
    if any(p in lower for p in ("set a timer", "timer for", "remind me in")):
        parsed = _parse_timer(lower)
        if parsed:
            seconds, label = parsed
            if _on_timer_done:
                tools.set_timer(seconds, label, _on_timer_done)
            return _s(f"Timer set for {label}."), "Timer"
        return _s("I didn't catch the duration."), "Timer"

    # Volume / mute
    if "mute" in lower and "unmute" not in lower:
        return _s(tools.mute()), "System"
    if "unmute" in lower:
        return _s(tools.unmute()), "System"
    if any(p in lower for p in ("set volume", "volume to", "turn volume", "volume up", "volume down")):
        level = _parse_volume(lower)
        return _s(tools.set_volume(level if level is not None else (80 if "up" in lower else 30))), "System"

    # Brightness
    if any(p in lower for p in ("brightness", "dim the screen", "dim screen", "brighten")):
        level = _parse_volume(lower)
        if level is None:
            level = 30 if any(w in lower for w in ("dim", "low", "dark")) else 80
        return _s(tools.set_brightness(level)), "System"

    # Screenshot
    if any(p in lower for p in ("take a screenshot", "screenshot", "capture screen")):
        return _s(tools.take_screenshot()), "System"

    # Lock
    if any(p in lower for p in ("lock screen", "lock my screen")):
        return _s(tools.lock_screen()), "System"

    # Clipboard
    if any(p in lower for p in ("what's in my clipboard", "read my clipboard", "what did i copy")):
        return _s(terminal.get_clipboard()), "Clipboard"

    # Self-improve fast paths
    if any(p in lower for p in ("restore backup", "undo last change", "revert")):
        backups = si.list_backups()
        if not backups:
            return _s("No backups found."), "Self-Improve"
        result = si.restore_backup(backups[0])
        return _s(result), "Self-Improve"

    if any(p in lower for p in ("restart yourself", "restart jarvis", "reload yourself",
                                "apply changes", "hit your restart", "do a restart")):
        def _do_restart():
            import time
            time.sleep(0.8)  # let the TTS finish speaking
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()
        return _s("Restarting now."), "Self-Improve"

    # ── 2. Hardware fast-path ─────────────────────────────────────────────────
    hw_result = _route_hardware(lower, user_input)
    if hw_result:
        return hw_result

    # ── 3. Orchestrator dispatch ──────────────────────────────────────────────
    return _orchestrate(user_input, lower)


# ── Orchestrator dispatch ─────────────────────────────────────────────────────

def _orchestrate(user_input: str, lower: str) -> tuple:
    """Use the orchestrator to classify intent and dispatch the right tool."""
    from orchestrator import classify

    decision = classify(user_input)
    tool      = decision.tool
    params    = decision.params

    # ── Search ────────────────────────────────────────────────────────────────
    if tool == "search":
        query = params.get("query", user_input)
        raw = tools.web_search(query)
        return format_with_mini(
            f"Summarize these search results concisely in Jarvis voice:\n{raw}"
        ), "Search"

    # ── Deep research ─────────────────────────────────────────────────────────
    if tool == "deep_research":
        from research import deep_research, format_for_voice
        query = params.get("query", user_input)

        def _stream_research():
            yield "Initiating deep research. This will take a moment, sir."

        # Run research in background, return immediately
        def _do_research():
            result = deep_research(query, depth=2)
            # Save to notes automatically
            notes.add_note(f"# Research: {query}\n\n{result['report']}\n\n"
                           f"Sources: {len(result['sources'])}")
            return result

        # Return a stream that blocks until done
        def _blocking_stream():
            yield f"Researching '{query}'. Reading sources now..."
            result = _do_research()
            voice_summary = format_for_voice(result)
            yield " " + voice_summary
            yield f" Full report saved to notes. {len(result['sources'])} sources cited."

        return _blocking_stream(), "Deep Research"

    # ── Operative agent ───────────────────────────────────────────────────────
    if tool == "operative":
        from operative import run_task
        task = params.get("task", user_input)

        def _operative_stream():
            yield f"Understood. Running the task autonomously now, sir."
            steps_done = []

            def _progress(step_desc, detail):
                steps_done.append(step_desc)

            result = run_task(task, on_progress=_progress)
            yield " " + result["summary"]
            if not result["ok"]:
                failed = [s.description for s in result["steps"] if not s.ok]
                yield f" Note: {len(failed)} step{'s' if len(failed)>1 else ''} encountered issues."

        return _operative_stream(), "Operative"

    # ── Messages / iMessage ───────────────────────────────────────────────────
    if tool == "message":
        recipient = params.get("recipient", params.get("to", ""))
        body      = params.get("message",   params.get("body", params.get("text", "")))
        # Try to pull recipient from raw input if orchestrator missed it
        if not recipient:
            m = re.search(r"(?:text|message|send to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", user_input)
            if m:
                recipient = m.group(1)
        if recipient and body:
            return _s(msg.send_imessage(recipient, body)), "Messages"
        if recipient and not body:
            return _s(f"What would you like to say to {recipient}?"), "Messages"
        return _s("Who would you like to message?"), "Messages"

    # ── Calendar ──────────────────────────────────────────────────────────────
    if tool == "calendar":
        action = params.get("action", "read")
        if action == "read":
            return _s(gs.get_todays_events()), "Calendar"
        # create — fall through to chat for now
        return smart_stream(user_input)

    # ── Email ─────────────────────────────────────────────────────────────────
    if tool == "email":
        return _s(gs.get_unread_emails()), "Gmail"

    # ── Weather ───────────────────────────────────────────────────────────────
    if tool == "weather":
        return _s(tools.get_weather()), "Weather"

    # ── Notes ─────────────────────────────────────────────────────────────────
    if tool == "notes":
        action = params.get("action", "")
        if "read" in action or "show" in action or "get" in action:
            return _s(notes.get_notes()), "Notes"
        if "search" in action:
            kw = params.get("keyword", "")
            return _s(notes.search_notes(kw)), "Notes"
        # save
        content = params.get("content", "")
        if content:
            return _s(notes.add_note(content)), "Notes"
        return _s(notes.get_notes()), "Notes"

    # ── Terminal ─────────────────────────────────────────────────────────────
    if tool == "terminal":
        cmd = params.get("command", params.get("cmd", ""))
        if cmd:
            output = terminal.run_command(cmd)
            return format_with_mini(
                f"The user ran: '{cmd}'. Output:\n{output}\nSummarize concisely in Jarvis voice."
            ), "Terminal"
        path = params.get("path", "")
        if path:
            content = terminal.read_file(path)
            return format_with_mini(f"Summarize this file concisely:\n{content}"), "File"
        return smart_stream(user_input)

    # ── App ──────────────────────────────────────────────────────────────────
    if tool == "app":
        app_name = params.get("app", _parse_app(user_input) or "")
        if app_name:
            return _s(tools.open_app(app_name)), "App"
        return smart_stream(user_input)

    # ── Camera / Vision ───────────────────────────────────────────────────────
    if tool == "camera":
        action = params.get("action", "webcam")
        if "screen" in action or "screenshot" in action:
            return _s(camera.screenshot_and_describe(user_input)), "Screen"
        return _s(camera.see(user_input)), "Camera"

    # ── Memory ────────────────────────────────────────────────────────────────
    if tool == "memory":
        action = params.get("action", "")
        if action == "save" or lower.startswith("remember "):
            fact = re.sub(r"^remember\s+", "", user_input, flags=re.IGNORECASE).strip()
            mem.add_fact(fact)
            return _s(f"Got it. I'll remember that {fact}."), "Memory"
        if action == "forget" or lower.startswith("forget "):
            keyword = re.sub(r"^forget\s+", "", user_input, flags=re.IGNORECASE).strip()
            removed = mem.forget(keyword)
            return _s("Forgotten." if removed else f"Nothing saved about {keyword}."), "Memory"
        # briefing
        if any(p in lower for p in ("briefing", "catch me up", "what did i miss")):
            from briefing import build_briefing
            return _s(build_briefing(mem.list_facts())), "Memory"
        return smart_stream(user_input)

    # ── Self-improve ──────────────────────────────────────────────────────────
    if tool == "self_improve":
        action = params.get("action", "improve")
        if action == "restart":
            return _s("Restarting now to apply the latest changes."), "Self-Improve"
        if action == "analyze":
            area = params.get("area", None)
            analysis = si.analyze_weakness(area)
            return _s(analysis), "Self-Improve"
        # improve
        target = params.get("target", params.get("area", ""))
        return _s("Analyzing my own code and generating improvements. This will take a moment..."), "Self-Improve"

    # ── Meeting ───────────────────────────────────────────────────────────────
    if tool == "meeting":
        import meeting_listener as ml
        return _s(ml.auto_configure_blackhole()), "Meeting"

    # ── Chat fallback ─────────────────────────────────────────────────────────
    return smart_stream(user_input)


# ── Hardware routing ──────────────────────────────────────────────────────────

_HW_ROUTES = [
    (["fire web shooter", "fire shooter", "activate shooter", "web shooter", "shoot"],
     "web_shooter", "fire", {}),
    (["reload", "reload shooter"], "web_shooter", "reload", {}),
    (["arm retract", "retract arm", "pull back"], "arm", "retract", {}),
    (["arm extend", "extend arm", "reach out"],   "arm", "extend",  {}),
    (["activate relay", "turn on relay", "relay on"],    "relay_1", "on",  {}),
    (["deactivate relay", "turn off relay", "relay off"], "relay_1", "off", {}),
]


def _route_hardware(lower: str, user_input: str):
    devices = hw.list_devices()

    if any(p in lower for p in ["hardware status", "device status", "check devices", "show hardware"]):
        s = hw.status()
        if not s or "No hardware" in s:
            return _s("No hardware devices registered, sir."), "Hardware"
        return format_with_mini(f"Report this hardware status in Jarvis voice: {s}"), "Hardware"

    if any(p in lower for p in ["scan ports", "find devices", "detect hardware"]):
        ports = hw.scan_serial_ports()
        return _s(f"Found {len(ports)} port{'s' if len(ports)>1 else ''}: {', '.join(ports)}." if ports
                  else "No serial ports detected."), "Hardware"

    if any(p in lower for p in ["emergency stop", "abort all", "halt all", "stop all devices"]):
        results = hw.command_all("stop")
        ok = sum(1 for r in results.values() if r.ok)
        return _s(f"Emergency stop sent to {len(results)} devices. {ok} confirmed."), "Hardware"

    for device in devices:
        dname = device.name.lower().replace("_", " ")
        cmd_match = re.search(
            rf"(?:fire|activate|trigger|run|execute|use|engage)\s+{re.escape(dname)}(?:\s+(\w+))?|"
            rf"{re.escape(dname)}\s+(\w+)|"
            rf"(?:turn\s+(?:on|off))\s+{re.escape(dname)}",
            lower
        )
        if cmd_match:
            cmd = (cmd_match.group(1) or cmd_match.group(2) or "trigger").strip()
            if "turn off" in lower or "deactivate" in lower:
                cmd = "off"
            elif "turn on" in lower or "activate" in lower and not cmd_match.group(1):
                cmd = "on"
            result = hw.command(device.name, cmd)
            msg = str(result)
            return format_with_mini(f"Report this in Jarvis voice (1 sentence): {msg}"), "Hardware"

    for triggers, device_name, cmd, extra_params in _HW_ROUTES:
        if any(t in lower for t in triggers):
            dur = re.search(r"(\d+)\s*ms", lower)
            p = dict(extra_params, duration=int(dur.group(1))) if dur else extra_params
            result = hw.command(device_name, cmd, **p)
            return format_with_mini(f"Report in Jarvis voice (1 sentence): {result}"), "Hardware"

    return None
