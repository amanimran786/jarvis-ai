import os
import subprocess
import threading
import time
import urllib.request
import json
from datetime import datetime
from ddgs import DDGS
from screen_capture import capture_screenshot


# ── Weather ───────────────────────────────────────────────────────────────────

def get_weather() -> str:
    """Get current weather using wttr.in — auto-detects location by IP, no API key needed."""
    try:
        url = "https://wttr.in/?format=j1"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        current = data["current_condition"][0]
        area = data["nearest_area"][0]
        city = area["areaName"][0]["value"]
        temp_f = current["temp_F"]
        desc = current["weatherDesc"][0]["value"]
        feels_f = current["FeelsLikeF"]
        return f"{desc}, {temp_f}°F, feels like {feels_f}°F in {city}."
    except Exception as e:
        return f"Couldn't get weather: {e}"


# ── Web search ────────────────────────────────────────────────────────────────

def web_search(query: str, max_results: int = 3) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "I couldn't find anything on that."
        return "\n".join(f"- {r['title']}: {r['body']}" for r in results)
    except Exception as e:
        return f"Search failed: {e}"


# ── App launcher ──────────────────────────────────────────────────────────────

def open_app(app_name: str) -> str:
    try:
        subprocess.Popen(["open", "-a", app_name])
        return f"Opening {app_name}."
    except Exception as e:
        return f"Couldn't open {app_name}: {e}"


# ── Timer ─────────────────────────────────────────────────────────────────────

def set_timer(seconds: int, label: str, on_done) -> None:
    def _run():
        time.sleep(seconds)
        on_done(label)
    threading.Thread(target=_run, daemon=True).start()


# ── System control ────────────────────────────────────────────────────────────

def set_volume(level: int) -> str:
    """Set system volume 0–100."""
    level = max(0, min(100, int(level)))  # int() prevents float/injection via f-string
    subprocess.run(["osascript", "-e", f"set volume output volume {level}"], check=True)
    return f"Volume set to {level}."


def mute() -> str:
    subprocess.run(["osascript", "-e", "set volume with output muted"], check=True)
    return "Muted."


def unmute() -> str:
    subprocess.run(["osascript", "-e", "set volume without output muted"], check=True)
    return "Unmuted."


def set_brightness(level: int) -> str:
    """Set screen brightness 0–100. Requires: brew install brightness"""
    level = max(0, min(100, level))
    fraction = level / 100
    result = subprocess.run(["brightness", str(fraction)], capture_output=True)
    if result.returncode != 0:
        return "Brightness control requires 'brew install brightness'. I couldn't change it."
    return f"Brightness set to {level}%."


def take_screenshot() -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.expanduser(f"~/Desktop/screenshot_{timestamp}.png")
    capture_screenshot(path, image_format="png")
    return f"Screenshot saved to your Desktop as screenshot_{timestamp}.png."


def lock_screen() -> str:
    subprocess.run([
        "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession",
        "-suspend"
    ], check=True)
    return "Locking screen."
