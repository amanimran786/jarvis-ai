"""
Basic browser control for Jarvis on macOS.

Supports Safari and Chromium-style browsers via AppleScript for:
- opening URLs
- web search
- navigation (back/forward/reload)
- reading page text
- summarizing the current page
- clicking links/buttons by visible text
- reading live meeting captions from browser tabs
"""

import json
import subprocess
import time
import urllib.parse
import urllib.request
import re
from html import unescape

from model_router import format_with_mini

_SUPPORTED_BROWSERS = ("Google Chrome", "ChatGPT Atlas", "Brave Browser", "Safari")
_MAX_PAGE_TEXT = 12000
_MEETING_URL_PATTERNS = {
    "meet.google.com": "MEET",
    "teams.microsoft.com": "TEAMS",
    "app.zoom.us": "ZOOM",
    "zoom.us/wc": "ZOOM",
    "zoom.us/j/": "ZOOM",
    "webex.com": "WEBEX",
}


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


def _frontmost_app_name() -> str:
    out, _err = _run_applescript(
        'tell application "System Events" to get name of first application process whose frontmost is true'
    )
    return out.strip()


def _choose_browser(preferred: str | None = None) -> str:
    if preferred in _SUPPORTED_BROWSERS and _app_exists(preferred):
        return preferred
    frontmost = _frontmost_app_name()
    if frontmost in _SUPPORTED_BROWSERS and _app_exists(frontmost):
        return frontmost
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


def open_then_click(target: str, click_target: str, browser: str | None = None, wait_seconds: float = 2.0) -> str:
    """Open a page, click a visible label, and report the result."""
    opened = open_url(target, browser=browser)
    time.sleep(wait_seconds)
    clicked = click_text(click_target, browser=browser)
    return f"{opened} {clicked}"


def open_click_then_summarize(
    target: str,
    click_target: str,
    request: str = "",
    browser: str | None = None,
    wait_seconds: float = 2.0,
) -> str:
    """Open a page, click a visible label, then summarize the resulting page."""
    opened = open_url(target, browser=browser)
    time.sleep(wait_seconds)
    clicked = click_text(click_target, browser=browser)
    time.sleep(wait_seconds)
    summary = summarize_current_page(request or f"Summarize the page after clicking {click_target} on {target}.", browser=browser)
    return f"{opened} {clicked} {summary}"


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


def _meeting_label_for_url(url: str) -> str | None:
    low = (url or "").lower()
    for pattern, label in _MEETING_URL_PATTERNS.items():
        if pattern in low:
            return label
    return None


def _tab_permission_hint(err: str, browser: str) -> str:
    text = (err or "").lower()
    if "executing javascript through applescript is turned off" in text:
        return (
            f" {browser} is blocking tab scripting. "
            "In Chrome, open View > Developer > Allow JavaScript from Apple Events, then try again."
        )
    if "access not allowed" in text or "-1723" in text or "not authorized" in text:
        return (
            f" macOS is blocking browser automation for {browser}. "
            "Allow your terminal or Python app under System Settings > Privacy & Security > Automation."
        )
    return ""


def _clean_browser_js_error(err: str, browser: str) -> str:
    raw = (err or "").strip()
    if not raw:
        return "Unknown browser automation error."

    hint = _tab_permission_hint(raw, browser)
    if hint:
        return f"Browser automation is blocked for {browser}.{hint}"

    cleaned = re.sub(r'Can[’\']t get ".*?" in tab \d+ of window \d+\.\s*', "", raw, flags=re.DOTALL)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        return cleaned
    return raw


def _list_browser_tabs(browser: str | None = None) -> list[dict]:
    app = _choose_browser(browser)
    if app == "Safari":
        page = _front_tab_data(app)
        if not page.get("ok"):
            return []
        return [{
            "browser": app,
            "window_index": 1,
            "tab_index": 1,
            "title": page.get("title", ""),
            "url": page.get("url", ""),
            "meeting": _meeting_label_for_url(page.get("url", "")),
            "active": True,
        }]

    script = f'''
    tell application "{app}"
        set outText to ""
        repeat with w from 1 to count of windows
            set activeIdx to active tab index of window w
            repeat with t from 1 to count of tabs of window w
                set theTitle to ""
                set theURL to ""
                try
                    set theTitle to title of tab t of window w
                end try
                try
                    set theURL to URL of tab t of window w
                end try
                set isActive to "0"
                if t is activeIdx then set isActive to "1"
                set outText to outText & w & "||" & t & "||" & isActive & "||" & theTitle & "||" & theURL & linefeed
            end repeat
        end repeat
        return outText
    end tell
    '''
    out, err = _run_applescript(script)
    if err:
        return []

    tabs = []
    for line in out.splitlines():
        parts = line.split("||", 4)
        if len(parts) != 5:
            continue
        window_index, tab_index, is_active, title, url = parts
        tabs.append({
            "browser": app,
            "window_index": int(window_index),
            "tab_index": int(tab_index),
            "title": title,
            "url": url,
            "meeting": _meeting_label_for_url(url),
            "active": is_active == "1",
        })
    return tabs


def _find_meeting_tabs(browser: str | None = None) -> list[dict]:
    browsers: list[str]
    if browser:
        browsers = [_choose_browser(browser)]
    else:
        frontmost = _frontmost_app_name()
        ordered = []
        if frontmost in _SUPPORTED_BROWSERS and _app_exists(frontmost):
            ordered.append(frontmost)
        for app in _SUPPORTED_BROWSERS:
            if app not in ordered and _app_exists(app):
                ordered.append(app)
        browsers = ordered

    tabs = []
    for app in browsers:
        for tab in _list_browser_tabs(app):
            if tab.get("meeting"):
                tabs.append(tab)
    tabs.sort(key=lambda item: (not item.get("active", False), item["browser"], item["window_index"], item["tab_index"]))
    return tabs


def _refresh_meeting_target(target: dict) -> dict:
    tabs = _find_meeting_tabs(target.get("browser"))
    if not tabs:
        return target

    target_url = target.get("url", "")
    target_title = target.get("title", "")

    exact = next(
        (
            tab for tab in tabs
            if tab.get("url") == target_url and tab.get("title") == target_title
        ),
        None,
    )
    if exact:
        return exact

    same_url = next((tab for tab in tabs if tab.get("url") == target_url), None)
    if same_url:
        return same_url

    return tabs[0]


def _execute_tab_js_target(target: dict, js: str) -> tuple[str, str]:
    target = _refresh_meeting_target(target)
    browser = target["browser"]
    safe_js = _escape_applescript(js)
    if browser == "Safari":
        script = f'''
        tell application "{browser}"
            if (count of windows) = 0 then error "No browser window is open."
            return do JavaScript "{safe_js}" in front document
        end tell
        '''
    else:
        window_index = int(target["window_index"])
        tab_index = int(target["tab_index"])
        script = f'''
        tell application "{browser}"
            if (count of windows) < {window_index} then error "Target browser window is not open."
            return execute tab {tab_index} of window {window_index} javascript "{safe_js}"
        end tell
        '''
    return _run_applescript(script)


def _execute_tab_js(browser: str, js: str) -> tuple[str, str]:
    safe_js = _escape_applescript(js)
    if browser == "Safari":
        script = f'''
        tell application "{browser}"
            if (count of windows) = 0 then error "No browser window is open."
            return do JavaScript "{safe_js}" in front document
        end tell
        '''
    else:
        script = f'''
        tell application "{browser}"
            if (count of windows) = 0 then error "No browser window is open."
            return execute active tab of front window javascript "{safe_js}"
        end tell
        '''
    return _run_applescript(script)


def _extract_page_text(browser: str, url: str) -> str:
    js = (
        f"(document.body && document.body.innerText ? document.body.innerText : '').slice(0, {_MAX_PAGE_TEXT})"
    )
    out, err = _execute_tab_js(browser, js)
    if not err and out.strip():
        return out.strip()
    if url:
        fetched = _fetch_url_text(url)
        if fetched:
            return fetched
    return ""


def _extract_meeting_caption_payload(browser: str | None = None) -> dict:
    page = _front_tab_data(browser)
    target = None
    if page.get("ok") and _meeting_label_for_url(page.get("url", "")):
        target = {
            "browser": page["browser"],
            "window_index": 1,
            "tab_index": 1,
            "title": page.get("title", ""),
            "url": page.get("url", ""),
            "meeting": _meeting_label_for_url(page.get("url", "")),
            "active": True,
        }
    else:
        tabs = _find_meeting_tabs(browser)
        if tabs:
            target = tabs[0]

    if not target:
        return {
            "ok": False,
            "browser": page.get("browser", _choose_browser(browser)),
            "error": "I couldn't find an open Meet, Teams, Zoom, or Webex tab. Open the meeting tab and try again.",
        }

    meeting_label = target["meeting"]

    js = """
    (() => {
      const selectors = [
        '[aria-live="polite"]',
        '[aria-live="assertive"]',
        '[role="log"]',
        '[data-subtitle-text]',
        '[data-caption-text]',
        '[class*="caption"]',
        '[class*="subtitle"]',
        '[id*="caption"]',
        '[id*="subtitle"]',
        '[aria-label*="caption"]',
        '[aria-label*="Caption"]',
        '[aria-label*="subtitle"]',
        '[aria-label*="Subtitle"]',
        '[aria-label*="captions"]',
        '[aria-label*="subtitles"]'
      ];
      const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style && style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
      };
      const cleaned = [];
      const seen = new Set();
      const pushLine = (text) => {
        const normalized = (text || '').replace(/\\s+/g, ' ').trim();
        if (!normalized || normalized.length < 2 || normalized.length > 220) return;
        if (seen.has(normalized)) return;
        seen.add(normalized);
        cleaned.push(normalized);
      };
      selectors.forEach((selector) => {
        document.querySelectorAll(selector).forEach((el) => {
          if (!visible(el)) return;
          const text = el.innerText || el.textContent || '';
          text.split('\\n').forEach(pushLine);
        });
      });
      return JSON.stringify({
        title: document.title,
        url: location.href,
        lines: cleaned.slice(-12)
      });
    })()
    """.strip()
    out, err = _execute_tab_js_target(target, js)
    if err:
        return {"ok": False, "browser": target["browser"], "error": _clean_browser_js_error(err, target["browser"])}
    try:
        payload = json.loads(out)
    except Exception:
        return {"ok": False, "browser": target["browser"], "error": "Couldn't parse meeting captions from the meeting tab."}
    lines = payload.get("lines") or []
    if not lines:
        return {
            "ok": False,
            "browser": target["browser"],
            "meeting": meeting_label,
            "title": payload.get("title", target["title"]),
            "url": payload.get("url", target["url"]),
            "error": "I found the meeting tab but could not find visible live captions. Turn captions on in the meeting and try again.",
        }
    return {
        "ok": True,
        "browser": target["browser"],
        "meeting": meeting_label,
        "title": payload.get("title", target["title"]),
        "url": payload.get("url", target["url"]),
        "lines": lines,
    }


def focus_meeting_tab(browser: str | None = None) -> str:
    tabs = _find_meeting_tabs(browser)
    if not tabs:
        return "I couldn't find an open Meet, Teams, Zoom, or Webex tab."

    target = _refresh_meeting_target(tabs[0])
    app = target["browser"]
    if app == "Safari":
        script = f'''
        tell application "{app}"
            activate
        end tell
        '''
    else:
        window_index = int(target["window_index"])
        tab_index = int(target["tab_index"])
        script = f'''
        tell application "{app}"
            activate
            set active tab index of window {window_index} to {tab_index}
            set index of window {window_index} to 1
        end tell
        '''
    _out, err = _run_applescript(script)
    if err:
        return f"Couldn't focus the meeting tab in {app}: {err}"
    title = target.get("title") or target.get("meeting") or "meeting tab"
    return f"Focused {title} in {app}."


def meeting_diagnostics(browser: str | None = None) -> dict:
    selected_browser = _choose_browser(browser)
    current_page = _front_tab_data(selected_browser)
    tabs = _find_meeting_tabs(browser)
    captions = _extract_meeting_caption_payload(browser)
    permission_blocked = "access not allowed" in (captions.get("error", "") or "").lower() or "-1723" in (captions.get("error", "") or "")
    return {
        "browser": selected_browser,
        "current_page": current_page,
        "meeting_tabs": tabs,
        "captions": captions,
        "permission_blocked": permission_blocked,
    }


def meeting_diagnostics_text(browser: str | None = None) -> str:
    diag = meeting_diagnostics(browser)
    current = diag.get("current_page", {})
    tabs = diag.get("meeting_tabs", [])
    captions = diag.get("captions", {})
    current_text = (
        f"Current page: {current.get('title', 'unknown')} ({current.get('url', 'unknown URL')})."
        if current.get("ok")
        else f"Current page: unavailable. {current.get('error', 'No browser context.')}"
    )
    if tabs:
        first = tabs[0]
        meeting_text = (
            f"Meeting detected: {first.get('meeting')} in {first.get('browser')} "
            f"(window {first.get('window_index')}, tab {first.get('tab_index')})."
        )
    else:
        meeting_text = "Meeting detected: none."

    if captions.get("ok"):
        caption_text = (
            f"Captions: accessible in {captions.get('browser')}. "
            f"I can currently read {len(captions.get('lines', []))} recent caption lines."
        )
    else:
        caption_text = f"Captions: blocked or unavailable. {captions.get('error', 'Unknown reason.')}"

    return f"{meeting_text} {current_text} {caption_text}"


def read_meeting_captions(browser: str | None = None) -> str:
    payload = _extract_meeting_caption_payload(browser=browser)
    if not payload.get("ok"):
        return f"Couldn't read meeting captions: {payload.get('error', 'Unknown error.')}"
    lines = payload.get("lines", [])
    joined = "\n- ".join(lines)
    return (
        f"Recent {payload['meeting']} captions in {payload['browser']}:\n"
        f"- {joined}"
    )


def summarize_meeting_captions(request: str = "", browser: str | None = None) -> str:
    payload = _extract_meeting_caption_payload(browser=browser)
    if not payload.get("ok"):
        return f"Couldn't summarize meeting captions: {payload.get('error', 'Unknown error.')}"

    caption_text = "\n".join(payload.get("lines", []))
    prompt = (
        f"Browser: {payload['browser']}\n"
        f"Meeting: {payload['meeting']}\n"
        f"Title: {payload.get('title', '')}\n"
        f"URL: {payload.get('url', '')}\n"
        f"User request: {request or 'Read the live meeting captions and help me respond.'}\n\n"
        f"Recent live captions:\n{caption_text}\n\n"
        "You are Jarvis helping during a live call. Give the most useful concise response, answer, or talking point based on these captions. "
        "If the user seems to need a direct answer, provide it. If the conversation is ambiguous, summarize the topic and give the best next thing to say."
    )
    return "".join(format_with_mini(prompt, skill_id="browser_execution", tool="browser"))


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


def _fetch_url_html(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _find_link_url(page_url: str, target_text: str) -> str:
    html = _fetch_url_html(page_url)
    if not html:
        return ""

    target = target_text.lower().strip()
    candidates = []
    for href, inner_html in re.findall(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html):
        text = re.sub(r"(?s)<[^>]+>", " ", inner_html)
        text = unescape(re.sub(r"\s+", " ", text).strip()).lower()
        href_lower = href.lower()
        score = 0
        if text == target:
            score = 100
        elif re.search(rf"(?:^|/){re.escape(target)}(?:/|$)", href_lower):
            score = 95
        elif re.search(rf"\b{re.escape(target)}\b", text):
            score = 80
        elif re.search(rf"\b{re.escape(target)}\b", href_lower):
            score = 60
        if score:
            candidates.append((score, urllib.parse.urljoin(page_url, href)))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


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
    return "".join(format_with_mini(prompt, skill_id="browser_execution", tool="browser"))


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
    js = (
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

    out, err = _execute_tab_js(app, js)
    if err:
        page = _front_tab_data(browser)
        fallback_url = _find_link_url(page.get("url", ""), target_text) if page.get("ok") else ""
        if fallback_url:
            return (
                f"Couldn't click '{target_text}' in {app} via JavaScript, so I opened the matching link directly. "
                f"{open_url(fallback_url, browser=browser)}"
            )
        return f"Couldn't click '{target_text}' in {app}: {err}"
    if out == "NOT_FOUND":
        page = _front_tab_data(browser)
        fallback_url = _find_link_url(page.get("url", ""), target_text) if page.get("ok") else ""
        if fallback_url:
            return (
                f"I didn't find an exact clickable element labeled '{target_text}', so I opened the closest matching link directly. "
                f"{open_url(fallback_url, browser=browser)}"
            )
        return f"I couldn't find a visible link or button matching '{target_text}' on the current page."
    return f"Clicked '{target_text}' in {app}."
