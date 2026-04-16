"""
Local markdown vault for Jarvis.

The vault is a cheap, inspectable knowledge layer that sits between memory and
full web/cloud research. Jarvis can index local markdown files, search them,
and inject only the relevant snippets into the active request with citations.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


VAULT_ROOT = Path(__file__).resolve().parent / "vault"
RAW_DIR = VAULT_ROOT / "raw"
WIKI_DIR = VAULT_ROOT / "wiki"
INDEXES_DIR = VAULT_ROOT / "indexes"
OUTPUTS_DIR = VAULT_ROOT / "outputs"
TEMPLATES_DIR = VAULT_ROOT / "templates"
INDEX_FILE = INDEXES_DIR / "index.json"

_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "how",
    "i", "if", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "was", "what", "when", "where", "which", "who", "why", "with", "you", "your",
}
_KNOWLEDGE_PATTERNS = (
    "what do you know about",
    "from the vault",
    "in the vault",
    "knowledge base",
    "wiki",
    "notes on",
    "summarize the vault",
    "search the vault",
    "search your knowledge",
)
_BRAIN_PATTERNS = (
    "my background",
    "my experience",
    "my projects",
    "my priorities",
    "my preferences",
    "what are we building",
    "what are we working on",
    "what am i working on",
    "jarvis roadmap",
    "jarvis goals",
    "jarvis priorities",
    "career story",
    "interview stories",
    "target roles",
    "role fit",
    "why anthropic",
    "why openai",
    "why apple",
    "why youtube",
    "openai fit",
    "anthropic fit",
    "apple fit",
    "youtube fit",
)
_MEMORY_PATTERNS = (
    "what do you know about me",
    "catch me up",
    "what did i miss",
    "what should i focus on",
    "where are we",
    "what are my priorities",
    "what's my background",
)


def _path_bias(path: str) -> int:
    normalized = (path or "").replace("\\", "/").lower()
    if normalized.startswith("wiki/brain/"):
        return 25
    if normalized.startswith("wiki/"):
        return 10
    if normalized.startswith("indexes/"):
        return 4
    if normalized.startswith("raw/imports/"):
        return -12
    if normalized.startswith("raw/"):
        return -4
    return 0


def _is_brain_query(query: str) -> bool:
    lower = (query or "").lower().strip()
    if not lower:
        return False
    if any(pattern in lower for pattern in _BRAIN_PATTERNS):
        return True
    short_query = len(lower.split()) <= 8
    if not short_query:
        return False
    first_person_project = (
        any(token in lower for token in ("my ", "our ", "we ", "jarvis"))
        and any(token in lower for token in ("project", "projects", "priority", "priorities", "preference", "preferences", "background", "experience", "roadmap", "goal", "goals"))
    )
    career_targeting = (
        any(token in lower for token in ("career", "interview", "role", "story", "stories", "fit"))
        and any(token in lower for token in ("openai", "anthropic", "apple", "youtube", "jarvis", "background", "experience"))
    )
    return first_person_project or career_targeting


def _is_memory_context_query(query: str) -> bool:
    lower = (query or "").lower().strip()
    return any(pattern in lower for pattern in _MEMORY_PATTERNS)


def _rewrite_context_query(query: str, tool: str | None = None) -> str:
    lower = (query or "").lower().strip()
    if tool == "memory":
        if "what do you know about me" in lower:
            return "identity my background my projects my preferences"
        if any(pattern in lower for pattern in ("catch me up", "what did i miss", "where are we")):
            return "my priorities jarvis roadmap current focus"
        if "what should i focus on" in lower:
            return "my priorities jarvis roadmap"
    if "what are we building" in lower or "what are we working on" in lower:
        return "jarvis roadmap current focus"
    if "my priorities" in lower:
        return "my priorities current focus"
    return query


def init_vault() -> None:
    for path in (RAW_DIR, WIKI_DIR, INDEXES_DIR, OUTPUTS_DIR, TEMPLATES_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _clean_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _extract_title(path: Path, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return path.stem.replace("_", " ").replace("-", " ").title()


def _iter_docs() -> list[Path]:
    init_vault()
    docs = []
    for root in (WIKI_DIR, INDEXES_DIR, RAW_DIR):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in _TEXT_EXTENSIONS:
                docs.append(path)
    return sorted(docs)


def _parse_sections(path: Path, raw: str) -> list[dict]:
    sections = []
    current = None
    page_number = None
    line_no = 0

    for line in raw.splitlines():
        line_no += 1
        stripped = line.strip()
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        page_match = re.match(r"^#+\s+(Page|Slide)\s+(\d+)\b", stripped, re.IGNORECASE)

        if heading_match:
            if current:
                current["text"] = "\n".join(current["lines"]).strip()
                sections.append(current)
            page_number = int(page_match.group(2)) if page_match else page_number
            current = {
                "heading": heading_match.group(2).strip(),
                "level": len(heading_match.group(1)),
                "page": page_number,
                "line_start": line_no,
                "lines": [],
            }
            continue

        if current is None:
            current = {
                "heading": _extract_title(path, raw),
                "level": 1,
                "page": page_number,
                "line_start": 1,
                "lines": [],
            }
        if stripped:
            current["lines"].append(stripped)

    if current:
        current["text"] = "\n".join(current["lines"]).strip()
        sections.append(current)

    normalized = []
    for section in sections:
        text = _clean_text(section.get("text", ""))
        if not text:
            continue
        normalized.append(
            {
                "heading": section["heading"],
                "level": section["level"],
                "page": section.get("page"),
                "line_start": section["line_start"],
                "text": text,
                "keywords": [token for token, _ in Counter(_tokenize(text)).most_common(12)],
            }
        )
    return normalized or [
        {
            "heading": _extract_title(path, raw),
            "level": 1,
            "page": None,
            "line_start": 1,
            "text": _clean_text(raw),
            "keywords": [token for token, _ in Counter(_tokenize(raw)).most_common(12)],
        }
    ]


def refresh_index() -> dict:
    init_vault()
    entries = []
    for path in _iter_docs():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        cleaned = _clean_text(raw)
        if not cleaned:
            continue
        sections = _parse_sections(path, raw)
        tokens = _tokenize(cleaned)
        common = [token for token, _ in Counter(tokens).most_common(20)]
        entries.append(
            {
                "path": str(path.relative_to(VAULT_ROOT)),
                "title": _extract_title(path, raw),
                "preview": cleaned[:500],
                "keywords": common,
                "chars": len(cleaned),
                "sections": sections[:40],
            }
        )

    payload = {
        "doc_count": len(entries),
        "docs": entries,
    }
    INDEX_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_index() -> dict:
    init_vault()
    if not INDEX_FILE.exists():
        return refresh_index()
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return refresh_index()


def _score_text(query: str, title: str, keywords: list[str], text: str) -> int:
    lower = query.lower()
    score = 0
    tokens = set(_tokenize(query))
    keyword_set = set(keywords)
    title_lower = (title or "").lower()
    preview = text.lower()

    if title_lower and title_lower in lower:
        score += 12
    for phrase in filter(None, re.split(r"[,.?!]", lower)):
        phrase = phrase.strip()
        if phrase and len(phrase) > 6 and phrase in preview:
            score += 8
    score += len(tokens & keyword_set) * 4
    score += sum(1 for token in tokens if token in title_lower) * 6
    return score


def _best_section(query: str, doc: dict) -> tuple[int, dict] | None:
    best = None
    for section in doc.get("sections", []):
        score = _score_text(query, section.get("heading", ""), section.get("keywords", []), section.get("text", ""))
        if not best or score > best[0]:
            best = (score, section)
    return best


def _citation_for(doc: dict, section: dict | None) -> dict:
    citation = {
        "path": doc["path"],
        "title": doc["title"],
        "heading": section.get("heading") if section else doc["title"],
        "page": section.get("page") if section else None,
        "line_start": section.get("line_start") if section else 1,
    }
    label = f"{citation['path']}"
    if citation["heading"]:
        label += f" > {citation['heading']}"
    if citation["page"]:
        label += f" (page {citation['page']})"
    citation["label"] = label
    return citation


def search(query: str, topn: int = 3) -> list[dict]:
    index = load_index()
    ranked = []
    for doc in index.get("docs", []):
        section_match = _best_section(query, doc)
        score = section_match[0] if section_match else _score_text(query, doc.get("title", ""), doc.get("keywords", []), doc.get("preview", ""))
        if score <= 0:
            continue
        score += _path_bias(doc.get("path", ""))
        section = section_match[1] if section_match else None
        excerpt_source = (section or {}).get("text") or doc.get("preview", "")
        excerpt = excerpt_source[:320]
        ranked.append(
            {
                **doc,
                "score": score,
                "excerpt": excerpt,
                "citation": _citation_for(doc, section),
                "matched_heading": (section or {}).get("heading"),
            }
        )

    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:topn]


def should_query(query: str, tool: str | None = None) -> bool:
    lower = (query or "").lower()
    if tool in {"deep_research", "knowledge"}:
        return True
    if tool == "memory" and _is_memory_context_query(query):
        return True
    if any(pattern in lower for pattern in _KNOWLEDGE_PATTERNS):
        return True
    if _is_brain_query(query):
        return True
    if "?" in lower and len(lower.split()) >= 8:
        return True
    return False


def build_context(query: str, tool: str | None = None, topn: int = 3) -> str:
    if not should_query(query, tool=tool):
        return ""
    brain_query = _is_brain_query(query)
    rewritten_query = _rewrite_context_query(query, tool=tool)
    results = search(rewritten_query, topn=min(topn, 2) if brain_query else topn)
    if brain_query:
        curated_hits = [
            item for item in results
            if str(item.get("path", "")).replace("\\", "/").lower().startswith("wiki/brain/")
            and int(item.get("score", 0)) >= 6
        ]
        if curated_hits:
            results = curated_hits[: min(topn, 2)]
    if not results:
        return ""

    snippets = []
    for item in results:
        snippets.append(f"[{item['citation']['label']}] {item['excerpt']}")
    return "Relevant local vault context with citations:\n" + "\n".join(snippets)


def status() -> dict:
    index = load_index()
    manifest = {}
    manifest_path = INDEXES_DIR / "wiki_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
    return {
        "root": str(VAULT_ROOT),
        "doc_count": index.get("doc_count", 0),
        "wiki_page_count": manifest.get("page_count", 0),
        "indexed_files": [doc["path"] for doc in index.get("docs", [])[:10]],
        "citation_ready": True,
    }


def status_text() -> str:
    info = status()
    if not info["doc_count"]:
        return "The local vault is set up but still empty. Add markdown files under vault/wiki, vault/indexes, or vault/raw, then ask me to refresh the vault index."
    sample = ", ".join(info["indexed_files"][:5])
    return (
        f"The local vault currently has {info['doc_count']} indexed documents and {info['wiki_page_count']} compiled wiki pages. "
        f"It is citation-ready and can point to exact local files and headings. Sample files: {sample}."
    )


def search_text(query: str, topn: int = 3) -> str:
    results = search(query, topn=topn)
    if not results:
        return f"I didn't find anything relevant in the local vault for {query}."

    lines = []
    for item in results:
        lines.append(f"{item['citation']['label']}: {item['excerpt']}")
    return "Here is the most relevant local vault context. " + " ".join(lines)


def build_wiki_text() -> str:
    from wiki_builder import build_wiki

    result = build_wiki()
    return (
        f"Built the local vault wiki from {result['raw_doc_count']} raw markdown files. "
        f"I generated {result['page_count']} compiled wiki pages and the vault now has "
        f"{result['index_doc_count']} indexed documents with citation metadata."
    )
