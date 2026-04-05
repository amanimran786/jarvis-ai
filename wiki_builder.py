"""
Build the local vault wiki from raw markdown sources.

The compiler stays deterministic and inspectable:
- raw files from vault/raw/
- compiled topic pages written to vault/wiki/compiled/
- cross-topic indexes written to vault/indexes/
- section and citation metadata carried into the manifest
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from vault import RAW_DIR, WIKI_DIR, INDEXES_DIR, init_vault, refresh_index


COMPILED_DIR = WIKI_DIR / "compiled"
MANIFEST_FILE = INDEXES_DIR / "wiki_manifest.json"
TOPICS_INDEX_FILE = INDEXES_DIR / "topics.md"
KEYWORD_INDEX_FILE = INDEXES_DIR / "keyword_index.md"
SOURCE_MAP_FILE = INDEXES_DIR / "source_map.md"
_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "how",
    "i", "if", "in", "is", "it", "of", "on", "or", "that", "the", "this", "to",
    "was", "what", "when", "where", "which", "who", "why", "with", "you", "your",
}


def _clean_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_title(path: Path, raw: str) -> str:
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:80]
    return path.stem.replace("_", " ").replace("-", " ").title()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "topic"


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _summary(text: str, limit: int = 2) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text)
    chosen = [part.strip() for part in parts if part.strip()][:limit]
    if chosen:
        return " ".join(chosen)
    return text[:280].strip()


def _parse_sections(raw: str, fallback_title: str) -> list[dict]:
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
                "heading": fallback_title,
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
                "summary": _summary(text, limit=1),
            }
        )
    return normalized


def _iter_raw_docs() -> list[Path]:
    init_vault()
    docs = []
    for path in RAW_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in _TEXT_EXTENSIONS:
            docs.append(path)
    return sorted(docs)


def _clear_compiled_pages() -> None:
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    for path in COMPILED_DIR.glob("*.md"):
        path.unlink(missing_ok=True)


def build_wiki() -> dict:
    init_vault()
    COMPILED_DIR.mkdir(parents=True, exist_ok=True)
    raw_docs = _iter_raw_docs()
    _clear_compiled_pages()

    pages = []
    keyword_counter: Counter[str] = Counter()

    for path in raw_docs:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue

        cleaned = _clean_text(raw)
        if not cleaned:
            continue

        title = _extract_title(path, raw)
        slug = _slugify(title)
        keywords = [token for token, _ in Counter(_tokenize(cleaned)).most_common(12)]
        keyword_counter.update(keywords[:8])
        summary = _summary(cleaned)
        rel_source = path.relative_to(path.parents[1]).as_posix()
        rel_page = f"wiki/compiled/{slug}.md"
        sections = _parse_sections(raw, title)
        citation_lines = []
        for section in sections[:12]:
            citation = f"- {section['heading']} at line {section['line_start']}"
            if section.get("page"):
                citation += f", page {section['page']}"
            citation += f": {section['summary']}"
            citation_lines.append(citation)
        if not citation_lines:
            citation_lines = ["- No section citations available."]

        page_body = "\n".join(
            [
                f"# {title}",
                "",
                f"Source file: `{rel_source}`",
                "",
                "## Summary",
                summary,
                "",
                "## Key Terms",
                ", ".join(keywords) if keywords else "None extracted",
                "",
                "## Citation Map",
                *citation_lines,
            ]
        )
        (COMPILED_DIR / f"{slug}.md").write_text(page_body + "\n", encoding="utf-8")

        pages.append(
            {
                "title": title,
                "slug": slug,
                "source": rel_source,
                "page": rel_page,
                "summary": summary,
                "keywords": keywords,
                "sections": sections[:20],
            }
        )

    pages.sort(key=lambda item: item["title"].lower())

    TOPICS_INDEX_FILE.write_text(_build_topics_index(pages), encoding="utf-8")
    KEYWORD_INDEX_FILE.write_text(_build_keyword_index(pages, keyword_counter), encoding="utf-8")
    SOURCE_MAP_FILE.write_text(_build_source_map(pages), encoding="utf-8")
    MANIFEST_FILE.write_text(json.dumps({"page_count": len(pages), "pages": pages}, indent=2), encoding="utf-8")

    vault_index = refresh_index()
    return {
        "raw_doc_count": len(raw_docs),
        "page_count": len(pages),
        "index_doc_count": vault_index.get("doc_count", 0),
        "pages": pages,
    }


def _build_topics_index(pages: list[dict]) -> str:
    lines = [
        "# Vault Topics",
        "",
        f"Compiled topic pages: {len(pages)}",
        "",
    ]
    if not pages:
        lines.append("No compiled topic pages yet. Add markdown files under `vault/raw/` and rebuild the wiki.")
        lines.append("")
        return "\n".join(lines)

    for page in pages:
        lines.extend(
            [
                f"## {page['title']}",
                f"Page: `{page['page']}`",
                f"Source: `{page['source']}`",
                f"Keywords: {', '.join(page['keywords'][:8]) or 'None'}",
                page["summary"],
            ]
        )
        if page.get("sections"):
            lines.append("Citations:")
            for section in page["sections"][:5]:
                citation = f"- {section['heading']} at line {section['line_start']}"
                if section.get("page"):
                    citation += f", page {section['page']}"
                lines.append(citation)
        lines.append("")
    return "\n".join(lines)


def _build_keyword_index(pages: list[dict], keyword_counter: Counter[str]) -> str:
    lines = [
        "# Vault Keyword Index",
        "",
    ]
    if not pages:
        lines.append("No keywords indexed yet.")
        lines.append("")
        return "\n".join(lines)

    mapping: dict[str, list[str]] = {}
    for page in pages:
        for keyword in page["keywords"][:8]:
            mapping.setdefault(keyword, []).append(page["title"])

    for keyword, _ in keyword_counter.most_common(40):
        titles = ", ".join(sorted(mapping.get(keyword, [])))
        if titles:
            lines.append(f"- {keyword}: {titles}")
    lines.append("")
    return "\n".join(lines)


def _build_source_map(pages: list[dict]) -> str:
    lines = [
        "# Vault Source Map",
        "",
    ]
    if not pages:
        lines.append("No raw sources compiled yet.")
        lines.append("")
        return "\n".join(lines)

    for page in pages:
        lines.append(f"- `{page['source']}` -> `{page['page']}`")
        for section in page.get("sections", [])[:3]:
            citation = f"  - {section['heading']} at line {section['line_start']}"
            if section.get("page"):
                citation += f", page {section['page']}"
            lines.append(citation)
    lines.append("")
    return "\n".join(lines)
