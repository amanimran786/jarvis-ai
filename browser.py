"""
Basic browser control for Jarvis on macOS.

Supports Safari and Google Chrome via AppleScript for:
- opening URLs
- web search
- navigation (back/forward/reload)
- reading page text
- summarizing the current page
- clicking links/buttons by visible text
"""

import json
import subprocess
import time
import urllib.parse
import urllib.request
import re
from html import unescape

from model_router import format_with_mini

_SUPPORTED_BROWSERS = ("Safari", "Google Chrome")
_MAX_PAGE_TEXT = 12000


def _run_applescript(script: str) -> tuple[str, str]:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15
    )
    return result.stdout.strip(), result.stderr.strip()


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _app_exists(app_name: str) -> bool:
    result = subprocess.run(["open", "-Ra", app_name], capture_output=True, text=True)
    return result.returncode == 0


def _choose_browser(preferred: str | None = None) -> str:
    if preferred in _SUPPORTED_BROWSERS and _app_exists(preferred):
        return preferred
    for app in _SUPPORTED_BROWSERS:
        if _app_exists(app):
            return app
    return "Safari"


def _normalize_url(target: str) -> str:
    target = target.strip()
    if not target:
        return "https://www.google.com"
    if "://" in target:
        return target
    if "." in target and " " not in target:
        return "https://" + target.lstrip("/")
    return "https://www.google.com/search?q=" + urllib.parse.quote(target)


def open_url(target: str, browser: str | None = None) -> str:
    app = _choose_browser(browser)
    url = _normalize_url(target)
    safe_url = _escape_applescript(url)

    if app == "Safari":
        script = f'''
        tell application "{app}"
            activate
            if (count of windows) = 0 then make new document
            tell front document to set URL to "{safe_url}"
        end tell
        '''
    else:
        script = f'''
        tell application "{app}"
            activate
            if (count of windows) = 0 then make new window
            set URL of active tab of front window to "{safe_url}"
        end tell
        '''

    out, err = _run_applescript(script)
    if err:
        return f"Couldn't open {url} in {app}: {err}"
    return f"Opened {url} in {app}."


def search_web(query: str, browser: str | None = None) -> str:
    return open_url(query, browser=browser)


def open_then_summarize(target: str, request: str = "", browser: str | None = None, wait_seconds: float = 2.0) -> str:
    """Open a page and summarize it after a short load delay."""
    opened = open_url(target, browser=browser)
    time.sleep(wait_seconds)
    summary = summarize_current_page(request or f"Summarize {target}.", browser=browser)
    return f"{opened} {summary}"


def _front_tab_data(browser: str | None = None) -> dict:
    app = _choose_browser(browser)
    if app == "Safari":
        script = f'''
        tell application "{app}"
            if (count of windows) = 0 then error "No browser window is open."
            set pageTitle to name of front document
            set pageURL to URL of front document
            return pageTitle & linefeed & pageURL
        end tell
        '''
    else:
        script = f'''
        tell application "{app}"
            if (count of windows) = 0 then error "No browser window is open."
            set pageTitle to title of active tab of front window
            set pageURL to URL of active tab of front window
            return pageTitle & linefeed & pageURL
        end tell
        '''

    out, err = _run_applescript(script)
    if err:
        return {"ok": False, "browser": app, "error": err}

    parts = out.splitlines()
    title = parts[0] if parts else ""
    url = parts[1] if len(parts) > 1 else ""
    return {
        "ok": True,
        "browser": app,
        "title": title,
        "url": url,
    }


def _extract_page_text(browser: str, url: str) -> str:
    js = _escape_applescript(
        f"(document.body && document.body.innerText ? document.body.innerText : '').slice(0, {_MAX_PAGE_TEXT})"
    )

    if browser == "Safari":
        script = f'''
        tell application "{browser}"
            if (count of windows) = 0 then error "No browser window is open."
            return do JavaScript "{js}" in front document
        end tell
        '''
    else:
        script = f'''
        tell application "{browser}"
            if (count of windows) = 0 then error "No browser window is open."
            return execute active tab of front window javascript "{js}"
        end tell
        '''

    out, err = _run_applescript(script)
    if not err and out.strip():
        return out.strip()
    if url:
        fetched = _fetch_url_text(url)
        if fetched:
            return fetched
    return ""


def _fetch_url_text(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_PAGE_TEXT]


def get_current_page(browser: str | None = None) -> str:
    page = _front_tab_data(browser)
    if not page["ok"]:
        return f"Couldn't read the current page: {page['error']}"
    title = page["title"] or "Untitled page"
    url = page["url"] or "unknown URL"
    return f"Current page in {page['browser']}: {title}. URL: {url}."


def summarize_current_page(request: str = "", browser: str | None = None) -> str:
    page = _front_tab_data(browser)
    if not page["ok"]:
        return f"Couldn't summarize the current page: {page['error']}"
    page_text = _extract_page_text(page["browser"], page["url"])
    if not page_text:
        return f"I opened {page['title']} but couldn't extract readable page text."

    prompt = (
        f"Browser: {page['browser']}\n"
        f"Title: {page['title']}\n"
        f"URL: {page['url']}\n"
        f"User request: {request or 'Summarize the current page.'}\n\n"
        f"Page text:\n{page_text}\n\n"
        "Summarize this for Aman in Jarvis voice. Keep it concise and spoken naturally."
    )
    return "".join(format_with_mini(prompt))


def _browser_command(action: str, browser: str | None = None) -> str:
    app = _choose_browser(browser)
    if app == "Safari":
        command = {
            "back": "tell front document to go back",
            "forward": "tell front document to go forward",
            "reload": "tell front document to do JavaScript \"location.reload()\"",
        }[action]
    else:
        command = {
            "back": "tell active tab of front window to go back",
            "forward": "tell active tab of front window to go forward",
            "reload": "tell active tab of front window to reload",
        }[action]

    script = f'''
    tell application "{app}"
        activate
        if (count of windows) = 0 then error "No browser window is open."
        {command}
    end tell
    '''
    out, err = _run_applescript(script)
    if err:
        return f"Couldn't {action} in {app}: {err}"
    return f"{action.capitalize()} in {app}."


def go_back(browser: str | None = None) -> str:
    return _browser_command("back", browser)


def go_forward(browser: str | None = None) -> str:
    return _browser_command("forward", browser)


def reload_page(browser: str | None = None) -> str:
    return _browser_command("reload", browser)


def click_text(target_text: str, browser: str | None = None) -> str:
    app = _choose_browser(browser)
    safe_target = json.dumps(target_text)
    js = _escape_applescript(
        f"""
        (() => {{
          const target = {safe_target}.toLowerCase().trim();
          const nodes = Array.from(document.querySelectorAll('a, button, input[type="button"], input[type="submit"]'));
          const match = nodes.find((node) => {{
            const text = (node.innerText || node.textContent || node.value || '').toLowerCase().trim();
            return text === target || text.includes(target);
          }});
          if (!match) return 'NOT_FOUND';
          match.click();
          return 'CLICKED';
        }})()
        """.strip()
    )

    if app == "Safari":
        script = f'''
        tell application "{app}"
            if (count of windows) = 0 then error "No browser window is open."
            set clickResult to do JavaScript "{js}" in front document
            return clickResult
        end tell
        '''
    else:
        script = f'''
        tell application "{app}"
            if (count of windows) = 0 then error "No browser window is open."
            set clickResult to execute active tab of front window javascript "{js}"
            return clickResult
        end tell
        '''

    out, err = _run_applescript(script)
    if err:
        return f"Couldn't click '{target_text}' in {app}: {err}"
    if out == "NOT_FOUND":
        return f"I couldn't find a visible link or button matching '{target_text}' on the current page."
    return f"Clicked '{target_text}' in {app}."
