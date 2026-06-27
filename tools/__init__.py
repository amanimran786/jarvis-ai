import logging
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime
from ddgs import DDGS
from desktop.screen_capture import capture_screenshot


# ── Web search helpers ────────────────────────────────────────────────────────

_PAGE_FETCH_TIMEOUT = 8  # seconds for HTTP fetch
_PAGE_MAX_CHARS = 6000   # cap raw page text before summarisation


def _summarise_for_voice(raw: str, query: str) -> str:
    try:
        from brains.brain_ollama import ask_local, get_best_available
        from config import LOCAL_DEFAULT
        model = get_best_available(LOCAL_DEFAULT)
        prompt = f"Search results for: {query}\n\n{raw[:1500]}"
        system = (
            "You are Jarvis. Summarise these search results in 2-3 natural spoken sentences. "
            "No markdown. No bullet points. Lead with the key finding."
        )
        result = ask_local(prompt, model=model, system_extra=system)
        return result.strip() if result and len(result) > 20 else raw
    except Exception:
        return raw


def fetch_page(url: str, max_chars: int = _PAGE_MAX_CHARS) -> str:
    """Fetch a URL and return stripped plain text, capped at max_chars."""
    import html
    import re
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=_PAGE_FETCH_TIMEOUT) as resp:
            raw_bytes = resp.read(max_chars * 10)
        text = raw_bytes.decode("utf-8", errors="replace")
        # Strip scripts/styles then tags
        text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:max_chars]
    except Exception as exc:
        return f"Could not fetch page: {exc}"


def web_search_with_fetch(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo, then fetch and summarise the top result page locally."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "I couldn't find anything on that."
    except Exception as exc:
        return f"Search failed: {exc}"

    # Build snippet list (with URLs)
    snippets = "\n".join(
        f"[{i+1}] {r['title']} ({r['href']})\n    {r['body']}"
        for i, r in enumerate(results)
    )

    # Fetch top result for full-page context
    top_url = results[0].get("href", "")
    page_text = ""
    if top_url:
        page_text = fetch_page(top_url)

    combined = f"Search snippets:\n{snippets}"
    if page_text and not page_text.startswith("Could not fetch"):
        combined += f"\n\nFull text of top result ({top_url}):\n{page_text[:2000]}"

    return _summarise_for_voice(combined, query) or snippets


# ── Weather ───────────────────────────────────────────────────────────────────

def get_weather(location: str = "") -> str:
    """Get current weather using wttr.in. Defaults to IP auto-detect when no location is given."""
    try:
        place = (location or "").strip()
        encoded_place = urllib.parse.quote(place, safe="")
        url = f"https://wttr.in/{encoded_place}?format=j1" if encoded_place else "https://wttr.in/?format=j1"
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

def web_search(query: str, max_results: int = 5, summarise: bool = True) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return "I couldn't find anything on that."
        raw = "\n".join(
            f"- {r['title']} ({r.get('href', '')}): {r['body']}"
            for r in results
        )
    except Exception as e:
        return f"Search failed: {e}"

    if summarise and len(raw) > 300:
        result_holder: list[str] = []

        def _run():
            result_holder.append(_summarise_for_voice(raw, query))

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=12)
        return result_holder[0] if result_holder else raw

    return raw


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


# ── Battery ───────────────────────────────────────────────────────────────────

def get_battery() -> str:
    """Return battery percentage and charging status."""
    try:
        out = subprocess.check_output(["pmset", "-g", "batt"], text=True, timeout=3)
        import re
        pct_match = re.search(r"(\d+)%", out)
        charging = "charging" in out.lower() or "ac power" in out.lower()
        if pct_match:
            pct = int(pct_match.group(1))
            status = "charging" if charging else "on battery"
            return f"Battery at {pct}%, {status}."
        return "Battery status unavailable."
    except Exception as e:
        return f"Battery check failed: {e}"


def mute() -> str:
    subprocess.run(["osascript", "-e", "set volume with output muted"], check=True)
    return "Muted."


def unmute() -> str:
    subprocess.run(["osascript", "-e", "set volume without output muted"], check=True)
    return "Unmuted."


def set_brightness(level: int) -> str:
    """Set screen brightness 0–100 using Quartz key events (Apple Silicon safe).

    Falls back to the 'brightness' CLI for older Intel Macs.
    Requires Accessibility permission for the app/terminal running Jarvis.
    """
    level = max(0, min(100, level))

    # Primary: Quartz CGEventPost (works on Apple Silicon M-series, all displays)
    try:
        import Quartz
        import time as _t
        STEPS = 16  # macOS has 16 brightness steps
        target = round(level / 100 * STEPS)

        # Drive to minimum first (press F1 / brightness-down STEPS+2 times)
        for _ in range(STEPS + 2):
            e = Quartz.CGEventCreateKeyboardEvent(None, 145, True)   # key down
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
            e = Quartz.CGEventCreateKeyboardEvent(None, 145, False)  # key up
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        _t.sleep(0.05)

        # Step up to target level (press F2 / brightness-up)
        for _ in range(target):
            e = Quartz.CGEventCreateKeyboardEvent(None, 144, True)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
            e = Quartz.CGEventCreateKeyboardEvent(None, 144, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)

        return f"Brightness set to {level}%."
    except Exception:
        logging.debug("[Tools] silent failure in set_brightness", exc_info=True)

    # Fallback: 'brightness' CLI (Intel Macs / external DDC displays)
    try:
        fraction = level / 100
        result = subprocess.run(["brightness", str(fraction)], capture_output=True, timeout=3)
        if result.returncode == 0:
            return f"Brightness set to {level}%."
    except Exception:
        logging.debug("[Tools] silent failure in set_brightness", exc_info=True)

    return (
        "Brightness control requires Accessibility permission. "
        "Go to System Settings → Privacy & Security → Accessibility → "
        "add Terminal (or Jarvis.app) and enable it."
    )


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


# ── Math eval ─────────────────────────────────────────────────────────────────

def eval_math(expr: str) -> str:
    """Safely evaluate a numeric expression. Only allows digits and math operators."""
    import re
    clean = expr.strip()
    if not re.fullmatch(r"[\d\s\+\-\*\/\.\(\)\%\^]+", clean):
        return ""
    try:
        clean = clean.replace("^", "**")
        result = eval(clean, {"__builtins__": {}})  # noqa: S307
        if isinstance(result, float) and result == int(result):
            return str(int(result))
        return str(round(result, 6)).rstrip("0").rstrip(".")
    except Exception:
        return ""


def get_current_time() -> str:
    """Return the current local time in a mobile-friendly human format."""
    from datetime import datetime
    return datetime.now().strftime("%-I:%M %p, %A %B %-d")


# ── Backend Engineer Workspace Tools ──────────────────────────────────────────

from .fs_tools import read_file, write_file
from .shell_tools import run_tests
