"""
harness/web_search.py — Web search harness for Jarvis.

Uses the `ddgs` package (DuckDuckGo) as the search backend — no API key needed.
Summarisation is done locally via Ollama (get_best_available → LOCAL_DEFAULT).

Public API:
    search(query, max_results=5, summarise=True) -> str
        Quick search returning ranked results with titles, URLs, and snippets.
        If Ollama is available the snippets are condensed to 2-3 spoken sentences.
        Falls back to raw snippet list on any Ollama error.

    fetch_page(url, max_chars=6000) -> str
        Fetch a URL and return stripped plain text (scripts/styles removed).

    search_and_fetch(query, max_results=5) -> str
        search() + fetch the top-ranked URL, then summarise the combined context.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import urllib.error
import urllib.request
from typing import Any

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover — package always present in production
    DDGS = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_SUMMARISE_TIMEOUT = 12   # seconds — local model call max wait
_FETCH_TIMEOUT     = 8    # seconds — HTTP page fetch max wait
_FETCH_MAX_BYTES   = 60_000  # cap raw bytes read before stripping
_PAGE_MAX_CHARS    = 6_000   # cap stripped plain text
_RAW_SUMMARISE_MIN = 300     # only summarise if raw snippets > N chars


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, max_results: int = 5, summarise: bool = True) -> str:
    """Search DuckDuckGo and return results, optionally summarised by local LLM."""
    try:
        with DDGS() as ddgs:
            results: list[dict[str, Any]] = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        log.warning("[WebSearch] DDGS search failed: %s", exc)
        return f"Search failed: {exc}"

    if not results:
        return "I couldn't find anything on that."

    raw = "\n".join(
        f"[{i + 1}] {r.get('title', '')} ({r.get('href', '')})\n    {r.get('body', '')}"
        for i, r in enumerate(results)
    )

    if summarise and len(raw) > _RAW_SUMMARISE_MIN:
        summary = _summarise(raw, query)
        if summary:
            return summary

    return raw


def search_and_fetch(query: str, max_results: int = 5) -> str:
    """Search + fetch top result, then summarise combined context locally."""
    try:
        with DDGS() as ddgs:
            results: list[dict[str, Any]] = list(ddgs.text(query, max_results=max_results))
    except Exception as exc:
        log.warning("[WebSearch] DDGS search failed: %s", exc)
        return f"Search failed: {exc}"

    if not results:
        return "I couldn't find anything on that."

    snippets = "\n".join(
        f"[{i + 1}] {r.get('title', '')} ({r.get('href', '')})\n    {r.get('body', '')}"
        for i, r in enumerate(results)
    )

    top_url = results[0].get("href", "")
    page_text = ""
    if top_url:
        page_text = fetch_page(top_url)

    context = f"Search snippets:\n{snippets}"
    if page_text and not page_text.startswith("Could not fetch"):
        context += f"\n\nFull text of top result ({top_url}):\n{page_text[:2000]}"

    summary = _summarise(context, query)
    return summary if summary else snippets


# ── Page fetch ────────────────────────────────────────────────────────────────

def fetch_page(url: str, max_chars: int = _PAGE_MAX_CHARS) -> str:
    """Fetch a URL and return stripped plain text, capped at max_chars."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            raw_bytes = resp.read(_FETCH_MAX_BYTES)
        text = raw_bytes.decode("utf-8", errors="replace")
        text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s{2,}", " ", text).strip()
        return text[:max_chars]
    except urllib.error.URLError as exc:
        log.debug("[WebSearch] fetch_page URLError for %s: %s", url, exc)
        return f"Could not fetch page: {exc}"
    except Exception as exc:
        log.debug("[WebSearch] fetch_page error for %s: %s", url, exc)
        return f"Could not fetch page: {exc}"


# ── Local summarisation ───────────────────────────────────────────────────────

def _summarise(raw: str, query: str) -> str:
    """Summarise search context in 2-3 spoken sentences via local LLM.

    Returns empty string on any failure so callers can fall back to raw.
    """
    result_holder: list[str] = []

    def _run() -> None:
        try:
            from brains.brain_ollama import ask_local, get_best_available
            from config import LOCAL_DEFAULT
            model = get_best_available(LOCAL_DEFAULT)
            prompt = f"Search results for: {query}\n\n{raw[:1500]}"
            system = (
                "You are Jarvis. Summarise these search results in 2-3 natural spoken sentences. "
                "No markdown. No bullet points. Lead with the key finding."
            )
            text = ask_local(prompt, model=model, system_extra=system)
            if text and len(text.strip()) > 20:
                result_holder.append(text.strip())
        except Exception:
            log.debug("[WebSearch] local summarise failed", exc_info=True)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=_SUMMARISE_TIMEOUT)
    return result_holder[0] if result_holder else ""
