"""
harness/web_search.py — Web search harness for Jarvis.

Uses the `ddgs` package (DuckDuckGo) as the search backend — no API key needed.
Summarisation is done locally via Ollama (get_best_available → LOCAL_DEFAULT).

Public API:
    search(query, max_results=5, summarise=True) -> str
        Quick search returning ranked results with titles, URLs, and snippets.
        If Ollama is available the snippets are condensed to 2-3 spoken sentences.
        Falls back to raw snippet list on any Ollama error.
        Retries on rate-limit (max 3, exponential backoff 1s/2s/4s).
        In-memory query cache with 5-minute TTL.

    fetch_page(url, max_chars=6000) -> str
        Fetch a URL and return stripped plain text (scripts/styles removed).
        Retries once with doubled timeout on timeout error.

    search_and_fetch(query, max_results=5) -> str
        search() + fetch the top-ranked URL, then summarise the combined context.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
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

# Retry / backoff
_MAX_RETRIES  = 3         # max rate-limit retries (not counting the initial attempt)
_BACKOFF_BASE = 1.0       # seconds; doubles per retry: 1s → 2s → 4s

# Cache
_CACHE_TTL    = 300       # 5-minute in-memory cache TTL (seconds)

# ── In-memory query cache ─────────────────────────────────────────────────────

# Maps normalised query → (results_list, monotonic_timestamp)
_query_cache: dict[str, tuple[list[dict[str, Any]], float]] = {}
_cache_lock  = threading.Lock()


def _normalize_query(query: str) -> str:
    """Lowercase, strip, collapse whitespace — canonical cache key."""
    return " ".join(query.strip().lower().split())


def _cache_get(query: str) -> list[dict[str, Any]] | None:
    """Return cached results for *query* if still fresh, else None."""
    key = _normalize_query(query)
    with _cache_lock:
        entry = _query_cache.get(key)
        if entry is None:
            return None
        results, ts = entry
        if time.monotonic() - ts > _CACHE_TTL:
            del _query_cache[key]
            return None
        return results


def _cache_set(query: str, results: list[dict[str, Any]]) -> None:
    """Store results in cache and prune stale entries opportunistically."""
    key = _normalize_query(query)
    now = time.monotonic()
    with _cache_lock:
        _query_cache[key] = (results, now)
        expired = [k for k, (_, ts) in _query_cache.items() if now - ts > _CACHE_TTL]
        for k in expired:
            del _query_cache[k]


# ── Rate-limit / timeout detection ───────────────────────────────────────────

_RATE_LIMIT_SIGNALS = ("429", "ratelimit", "rate limit", "rate_limit", "too many requests")


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _RATE_LIMIT_SIGNALS)


def _is_timeout_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "timeout" in msg or "timed out" in msg


# ── Audit logging ─────────────────────────────────────────────────────────────

def _log_retry(query: str, attempt: int, reason: str, delay: float) -> None:
    """Fire audit event for each retry — never raises."""
    try:
        from harness.audit import audit_log
        audit_log(
            "web_search_retry",
            query=query,
            attempt=attempt,
            reason=reason,
            delay_secs=delay,
        )
    except Exception:
        log.debug("[WebSearch] audit_log failed during retry", exc_info=True)


# ── DDGS fetch with retry ─────────────────────────────────────────────────────

def _ddgs_search(query: str, max_results: int) -> list[dict[str, Any]]:
    """Call DDGS.text() with retry logic.

    - RateLimitError / HTTP 429: up to _MAX_RETRIES retries with exponential
      backoff (1s, 2s, 4s).  Raises RuntimeError after max retries exhausted.
    - Timeout: retry once immediately (no delay).
    - Any other exception: re-raised immediately without retry.
    """
    rate_limit_attempts = 0
    timeout_retried     = False
    last_exc: Exception | None = None

    while True:
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(query, max_results=max_results))
        except Exception as exc:
            last_exc = exc

            if _is_timeout_error(exc) and not timeout_retried:
                timeout_retried = True
                _log_retry(query, 1, "timeout", 0.0)
                log.warning("[WebSearch] Search timeout; retrying once")
                continue  # immediate retry

            if _is_rate_limit_error(exc):
                rate_limit_attempts += 1
                if rate_limit_attempts > _MAX_RETRIES:
                    break  # exhausted — raise below
                delay = _BACKOFF_BASE * (2 ** (rate_limit_attempts - 1))  # 1, 2, 4
                log.warning(
                    "[WebSearch] Rate limit on attempt %d; retrying in %.1fs",
                    rate_limit_attempts, delay,
                )
                _log_retry(query, rate_limit_attempts, "rate_limit", delay)
                time.sleep(delay)
                continue

            # Non-retryable — propagate immediately
            raise

    raise RuntimeError(
        f"Web search failed after {_MAX_RETRIES} retries (last error: {last_exc})"
    )


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, max_results: int = 5, summarise: bool = True) -> str:
    """Search DuckDuckGo and return results, optionally summarised by local LLM."""
    cached = _cache_get(query)
    if cached is not None:
        results = cached
    else:
        try:
            results = _ddgs_search(query, max_results)
        except RuntimeError as exc:
            log.warning("[WebSearch] Search exhausted retries: %s", exc)
            return f"Search failed: {exc}"
        except Exception as exc:
            log.warning("[WebSearch] DDGS search failed: %s", exc)
            return f"Search failed: {exc}"
        _cache_set(query, results)

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
    cached = _cache_get(query)
    if cached is not None:
        results = cached
    else:
        try:
            results = _ddgs_search(query, max_results)
        except RuntimeError as exc:
            log.warning("[WebSearch] Search exhausted retries: %s", exc)
            return f"Search failed: {exc}"
        except Exception as exc:
            log.warning("[WebSearch] DDGS search failed: %s", exc)
            return f"Search failed: {exc}"
        _cache_set(query, results)

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
    """Fetch a URL and return stripped plain text, capped at max_chars.

    Retries once with doubled timeout if the initial request times out.
    """
    return _fetch_with_timeout(url, max_chars, _FETCH_TIMEOUT, retry_on_timeout=True)


def _fetch_with_timeout(
    url: str,
    max_chars: int,
    timeout: float,
    *,
    retry_on_timeout: bool = False,
) -> str:
    """Internal fetch worker; optionally retries with doubled timeout on timeout."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read(_FETCH_MAX_BYTES)
        return _strip_html(raw_bytes, max_chars)
    except Exception as exc:
        if retry_on_timeout and _is_timeout_error(exc):
            log.warning(
                "[WebSearch] fetch_page timeout for %s; retrying with %.0fs timeout",
                url, timeout * 2,
            )
            return _fetch_with_timeout(url, max_chars, timeout * 2, retry_on_timeout=False)
        if isinstance(exc, urllib.error.URLError):
            log.debug("[WebSearch] fetch_page URLError for %s: %s", url, exc)
        else:
            log.debug("[WebSearch] fetch_page error for %s: %s", url, exc)
        return f"Could not fetch page: {exc}"


def _strip_html(raw_bytes: bytes, max_chars: int) -> str:
    """Decode raw bytes, strip scripts/styles/tags, collapse whitespace."""
    text = raw_bytes.decode("utf-8", errors="replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text[:max_chars]


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
