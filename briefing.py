"""Jarvis smart briefing — on-demand and morning status report."""

import threading
from datetime import datetime
from typing import Callable

import vault


def _fetch_parallel(tasks: dict[str, Callable[[], str]], timeout: float = 8.0) -> dict[str, str]:
    results: dict[str, str] = {}
    lock = threading.Lock()

    def _run(key: str, fn: Callable[[], str]) -> None:
        try:
            value = fn()
        except Exception as e:
            value = f"unavailable ({e})"
        with lock:
            results[key] = value

    threads = [threading.Thread(target=_run, args=(k, fn), daemon=True) for k, fn in tasks.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    return results


def _greeting(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        period = "morning"
    elif hour < 17:
        period = "afternoon"
    else:
        period = "evening"
    return f"Good {period}, Aman."


def _focus_line() -> str:
    try:
        hits = vault.search("Jarvis roadmap local-first current focus", topn=5)
    except Exception:
        return ""
    for hit in hits or []:
        path = (hit.get("path") or "").lower()
        excerpt = (hit.get("excerpt") or "").strip()
        if not excerpt or "/raw/" in f"/{path}" or "raw/imports" in path:
            continue
        if excerpt.lower().startswith("purpose:"):
            continue
        return f"Current focus: {excerpt}"
    return ""


def _legacy_fact_briefing(facts: list[str] | tuple[str, ...]) -> str:
    name = "Aman"
    for fact in facts:
        match = None
        try:
            import re
            match = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z .'-]{0,40})", str(fact), flags=re.IGNORECASE)
        except Exception:
            match = None
        if match:
            name = match.group(1).strip().split()[0]
            break
    greeting = _greeting(datetime.now())
    if "aman" not in greeting.lower() and name:
        greeting = f"{greeting}, {name}."
    focus = _focus_line()
    return f"{greeting} {focus}".strip()


def _format_calendar(events_raw: str) -> str:
    if not events_raw or "unavailable" in events_raw.lower() or "no events" in events_raw.lower() or "no upcoming" in events_raw.lower():
        return ""
    lines = [line.strip() for line in events_raw.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    bullets = "\n".join(f"- {line}" for line in lines[:5])
    return f"**Calendar:**\n{bullets}"


def _format_email(email_raw: str) -> str:
    if not email_raw or "unavailable" in email_raw.lower() or "no unread" in email_raw.lower() or "0 unread" in email_raw.lower():
        return ""
    lines = [line.strip() for line in email_raw.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    bullets = "\n".join(f"- {line}" for line in lines[:5])
    return f"**Email:**\n{bullets}"


def _normalize_memory_summary(value: str | list[str] | tuple[str, ...] | None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        lines = [str(item).strip() for item in value if str(item).strip()]
        return "; ".join(lines[:6])
    return str(value).strip()


def build_briefing(include_memory_summary: str | list[str] | tuple[str, ...] | None = "") -> str:
    if isinstance(include_memory_summary, (list, tuple)):
        return _legacy_fact_briefing(include_memory_summary)

    now = datetime.now()

    import tools
    try:
        import google_services as gs
        has_google = True
    except Exception:
        has_google = False

    task_map: dict[str, Callable[[], str]] = {
        "weather": lambda: tools.get_weather(),
    }

    if has_google:
        task_map["calendar"] = lambda: _get_calendar_summary(gs)
        task_map["email"] = lambda: _get_email_summary(gs)

    data = _fetch_parallel(task_map)

    sections: list[str] = []

    weather = data.get("weather", "")
    if weather and "unavailable" not in weather.lower() and "couldn't" not in weather.lower():
        sections.append(f"**Weather:** {weather}")

    cal_section = _format_calendar(data.get("calendar", ""))
    if cal_section:
        sections.append(cal_section)

    email_section = _format_email(data.get("email", ""))
    if email_section:
        sections.append(email_section)

    memory_summary = _normalize_memory_summary(include_memory_summary)
    if memory_summary:
        sections.append(f"**Memory:** {memory_summary}")

    greeting = _greeting(now)
    if sections:
        body = "\n\n".join(sections)
        return f"{greeting} Here's your status.\n\n{body}\n\nStanding by."
    else:
        return f"{greeting} All systems nominal. Standing by."


def _get_calendar_summary(gs) -> str:
    try:
        events = gs.get_upcoming_events(max_results=5)
        if not events:
            return "No upcoming events today."
        lines = []
        for e in events:
            title = e.get("summary", "Untitled")
            start = e.get("start", {})
            time_str = start.get("dateTime", start.get("date", ""))
            if time_str:
                try:
                    dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                    time_label = dt.strftime("%-I:%M %p")
                except Exception:
                    time_label = time_str[:10]
                lines.append(f"{time_label} — {title}")
            else:
                lines.append(title)
        return "\n".join(lines)
    except Exception as e:
        return f"unavailable ({e})"


def _get_email_summary(gs) -> str:
    try:
        emails = gs.get_unread_emails(max_results=5)
        if not emails:
            return "No unread email."
        lines = []
        for em in emails:
            sender = em.get("from", em.get("sender", "Unknown"))
            subject = em.get("subject", "(no subject)")
            lines.append(f"{sender}: {subject}")
        return "\n".join(lines)
    except Exception as e:
        return f"unavailable ({e})"
