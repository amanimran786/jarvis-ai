"""
Jarvis Deep Research Agent.

Pipeline:
  1. Generate 3-5 diverse search queries from the topic
  2. Execute searches (DuckDuckGo)
  3. Fetch and extract text from top result pages
  4. Synthesize into a structured report with citations
  5. Return report + source list

Usage:
  from research import deep_research
  result = deep_research("quantum computing breakthroughs 2025")
  # result = {"report": "...", "sources": [...], "query": "...", "queries_used": [...]}
"""

import re
import urllib.request
import urllib.error
import html
from ddgs import DDGS
from brain_claude import ask_claude
from config import SONNET, HAIKU
import skills


# ── Query generation ──────────────────────────────────────────────────────────

def _generate_queries(topic: str) -> list[str]:
    """Use Haiku to generate diverse search queries for better coverage."""
    system_extra, _ = skills.build_system_extra(topic, skill_id="research_synthesis", tool="deep_research")
    prompt = (
        f"Generate 4 diverse search queries to thoroughly research: {topic}\n"
        f"Make them specific and complementary — different angles.\n"
        f"Return ONLY a JSON array of strings, nothing else."
    )
    try:
        raw = ask_claude(prompt, model=HAIKU, system_extra=system_extra)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:-1])
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            import json
            queries = json.loads(match.group())
            return [str(q) for q in queries[:5]]
    except Exception:
        pass
    # Fallback: simple variations
    return [topic, f"{topic} explained", f"{topic} latest research", f"{topic} overview"]


# ── Web search ────────────────────────────────────────────────────────────────

def _search(query: str, n: int = 4) -> list[dict]:
    """Search DuckDuckGo and return top results."""
    try:
        with DDGS() as d:
            return list(d.text(query, max_results=n))
    except Exception:
        return []


# ── Page fetcher ──────────────────────────────────────────────────────────────

def _fetch_page(url: str, max_chars: int = 4000) -> str:
    """Fetch a web page and return clean text content."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # Strip HTML tags
        text = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>",  " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()

        return text[:max_chars]
    except Exception:
        return ""


# ── Synthesis ─────────────────────────────────────────────────────────────────

def _synthesize(topic: str, sources: list[dict]) -> str:
    """Use Sonnet to synthesize all sources into a structured report."""
    system_extra, _ = skills.build_system_extra(topic, skill_id="research_synthesis", tool="deep_research")
    source_block = ""
    for i, src in enumerate(sources):
        source_block += f"\n\n--- Source {i+1}: {src['title']} ({src['url']}) ---\n{src['content']}"

    prompt = f"""You are a research analyst. Write a thorough, well-structured research report on:

TOPIC: {topic}

SOURCE MATERIAL:
{source_block}

Report requirements:
- Start with a concise executive summary (2-3 sentences)
- Cover key findings, facts, and insights from the sources
- Note any conflicting information or uncertainty
- End with a "Key Takeaways" section (3-5 bullet points as plain text)
- Cite sources inline using [Source N] format
- Write in clear, intelligent prose — no excessive markdown
- Length: 400-600 words

Do not invent facts not supported by the sources."""

    return ask_claude(prompt, model=SONNET, system_extra=system_extra)


# ── Main entry point ──────────────────────────────────────────────────────────

def deep_research(
    topic: str,
    depth: int = 3,
    on_progress=None,
) -> dict:
    """
    Run the full deep research pipeline.

    Args:
        topic:       What to research
        depth:       Number of pages to read per query (1-5)
        on_progress: Optional callback(step: str, detail: str)

    Returns:
        {
          "report":       full text report,
          "sources":      list of {title, url, snippet},
          "query":        original topic,
          "queries_used": list of search queries run,
        }
    """

    def _progress(step, detail=""):
        print(f"[Research] {step}" + (f": {detail[:80]}" if detail else ""))
        if on_progress:
            on_progress(step, detail)

    # ── Step 1: Generate queries ──────────────────────────────────────────
    _progress("Generating search queries")
    queries = _generate_queries(topic)
    _progress("Queries", str(queries))

    # ── Step 2: Search ────────────────────────────────────────────────────
    _progress("Searching the web")
    all_results: list[dict] = []
    seen_urls: set[str] = set()

    for q in queries:
        for r in _search(q, n=3):
            url = r.get("href", r.get("url", ""))
            if url and url not in seen_urls:
                seen_urls.add(url)
                all_results.append({
                    "title":   r.get("title", ""),
                    "url":     url,
                    "snippet": r.get("body", ""),
                })

    _progress(f"Found {len(all_results)} unique results")

    # ── Step 3: Fetch page content ────────────────────────────────────────
    sources_with_content: list[dict] = []
    fetch_limit = min(len(all_results), depth * len(queries))

    for i, result in enumerate(all_results[:fetch_limit]):
        _progress(f"Reading page {i+1}/{fetch_limit}", result["title"])
        content = _fetch_page(result["url"])
        if content and len(content) > 200:
            sources_with_content.append({**result, "content": content})
        else:
            # Fall back to snippet
            sources_with_content.append({**result, "content": result["snippet"]})

    # ── Step 4: Synthesize ────────────────────────────────────────────────
    _progress("Synthesizing report")
    report = _synthesize(topic, sources_with_content[:12])

    # ── Step 5: Build clean source list ──────────────────────────────────
    sources = [
        {"title": s["title"], "url": s["url"], "snippet": s["snippet"]}
        for s in sources_with_content
    ]

    return {
        "report":       report,
        "sources":      sources,
        "query":        topic,
        "queries_used": queries,
    }


def format_for_voice(result: dict) -> str:
    """Condense a research result to a spoken summary (2-3 sentences)."""
    prompt = (
        f"Summarize this research report in 2-3 spoken sentences. "
        f"No markdown, no bullets — natural spoken language:\n\n{result['report'][:2000]}"
    )
    return ask_claude(prompt, model=HAIKU)
