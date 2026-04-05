"""
Source ingestion pipeline for the local vault.

Supported sources:
- local files and directories
- repository/docs directories
- web pages
- PDFs
- PowerPoint slides (.pptx)
- Google Drive / Docs / Sheets URLs
- notes export
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests
from pypdf import PdfReader

import google_services as gs
import notes as notes_module
from vault import RAW_DIR, VAULT_ROOT, init_vault


TEXT_EXTENSIONS = {".md", ".markdown", ".txt", ".rst", ".py", ".json", ".yaml", ".yml"}
DOC_PRIORITY_NAMES = ("README", "AGENTS", "CLAUDE", "SKILL")
PRESENTATION_EXTENSIONS = {".pptx"}
GOOGLE_DOC_HOSTS = {"docs.google.com", "drive.google.com"}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "source"


def _safe_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _write_raw_markdown(name: str, body: str) -> Path:
    init_vault()
    filename = f"{_slugify(name)}.md"
    path = RAW_DIR / filename
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _extract_pdf_pages_from_reader(reader: PdfReader) -> list[dict]:
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = _safe_text(page.extract_text() or "")
        if text:
            pages.append({"number": index, "text": text})
    return pages


def _extract_pdf_bytes(data: bytes) -> list[dict]:
    return _extract_pdf_pages_from_reader(PdfReader(io.BytesIO(data)))


def _extract_pdf_file(path: Path) -> list[dict]:
    return _extract_pdf_pages_from_reader(PdfReader(str(path)))


def _format_pdf_markdown(title: str, source_label: str, pages: list[dict]) -> str:
    lines = [f"# Ingested PDF: {title}", "", f"Source: `{source_label}`", ""]
    if not pages:
        lines.extend(["## OCR / Text Extraction", "No extractable text was found in the PDF.", ""])
        return "\n".join(lines)

    for page in pages:
        lines.extend([f"## Page {page['number']}", page["text"], ""])
    return "\n".join(lines)


def _extract_pptx_slides(path: Path) -> list[dict]:
    slides = []
    namespace = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for index, name in enumerate(slide_names, start=1):
            raw = archive.read(name)
            root = ET.fromstring(raw)
            texts = [node.text.strip() for node in root.findall(".//a:t", namespace) if node.text and node.text.strip()]
            if texts:
                slides.append({"number": index, "text": "\n".join(texts)})
    return slides


def _format_slides_markdown(title: str, source_label: str, slides: list[dict]) -> str:
    lines = [f"# Ingested Slides: {title}", "", f"Source: `{source_label}`", ""]
    if not slides:
        lines.extend(["## Slide 1", "No extractable slide text was found.", ""])
        return "\n".join(lines)

    for slide in slides:
        lines.extend([f"## Slide {slide['number']}", slide["text"], ""])
    return "\n".join(lines)


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<script.*?>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style.*?>.*?</style>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()


def _is_url(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_google_drive_source(source: str) -> bool:
    parsed = urlparse(source)
    return parsed.netloc in GOOGLE_DOC_HOSTS or "/document/d/" in source or "/spreadsheets/d/" in source or "/presentation/d/" in source


def _finalize_result(result: dict, auto_build: bool, dry_run: bool) -> dict:
    build = None
    if auto_build and not dry_run:
        from wiki_builder import build_wiki
        build = build_wiki()
    result["build"] = build
    return result


def ingest_notes(auto_build: bool = True, dry_run: bool = False) -> dict:
    records = notes_module._load()
    if not records:
        return {"ok": False, "error": "No notes found to ingest."}

    lines = ["# Jarvis Notes Export", ""]
    for note in records[-100:]:
        lines.extend([f"## {note['date']}", note["content"], ""])
    preview_path = RAW_DIR / "jarvis-notes-export.md"
    path = preview_path if dry_run else _write_raw_markdown("jarvis-notes-export", "\n".join(lines))

    return _finalize_result(
        {
            "ok": True,
            "kind": "notes",
            "saved_paths": [str(path.relative_to(VAULT_ROOT))],
            "citations": [{"path": str(path.relative_to(VAULT_ROOT)), "heading": "Jarvis Notes Export"}],
        },
        auto_build,
        dry_run,
    )


def ingest_file(source: str, auto_build: bool = True, dry_run: bool = False) -> dict:
    path = Path(source).expanduser().resolve()
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": f"File not found: {source}"}

    if path.suffix.lower() == ".pdf":
        pages = _extract_pdf_file(path)
        body = _format_pdf_markdown(path.stem, str(path), pages)
        structure = "pdf"
        citations = [{"path": f"raw/{_slugify(path.stem)}.md", "heading": f"Page {page['number']}", "page": page["number"]} for page in pages[:12]]
    elif path.suffix.lower() in PRESENTATION_EXTENSIONS:
        slides = _extract_pptx_slides(path)
        body = _format_slides_markdown(path.stem, str(path), slides)
        structure = "slides"
        citations = [{"path": f"raw/{_slugify(path.name)}.md", "heading": f"Slide {slide['number']}", "page": slide["number"]} for slide in slides[:12]]
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(
            [
                f"# Ingested File: {path.name}",
                "",
                f"Source path: `{path}`",
                "",
                _safe_text(text),
            ]
        )
        structure = "text"
        citations = [{"path": f"raw/{_slugify(path.name)}.md", "heading": f"Ingested File: {path.name}"}]

    preview_path = RAW_DIR / f"{_slugify(path.name)}.md"
    saved = preview_path if dry_run else _write_raw_markdown(path.name, body)
    for citation in citations:
        citation["path"] = str(saved.relative_to(VAULT_ROOT))

    return _finalize_result(
        {
            "ok": True,
            "kind": "file",
            "structure": structure,
            "saved_paths": [str(saved.relative_to(VAULT_ROOT))],
            "citations": citations,
        },
        auto_build,
        dry_run,
    )


def _candidate_repo_docs(root: Path) -> list[Path]:
    candidates = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if ".git" in path.parts or "node_modules" in path.parts or "venv" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        candidates.append(path)

    def rank(path: Path) -> tuple[int, str]:
        score = 0
        if any(path.name.upper().startswith(name) for name in DOC_PRIORITY_NAMES):
            score -= 20
        if "docs" in path.parts:
            score -= 10
        if path.name == "SKILL.md":
            score -= 8
        return (score, str(path))

    candidates.sort(key=rank)
    return candidates[:40]


def ingest_directory(source: str, auto_build: bool = True, dry_run: bool = False) -> dict:
    root = Path(source).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return {"ok": False, "error": f"Directory not found: {source}"}

    docs = _candidate_repo_docs(root)
    if not docs:
        return {"ok": False, "error": f"No supported docs found under {source}"}

    lines = [f"# Repository Ingest: {root.name}", "", f"Source path: `{root}`", ""]
    citations = []
    for doc in docs:
        rel = doc.relative_to(root).as_posix()
        text = doc.read_text(encoding="utf-8", errors="replace")
        lines.extend([f"## {rel}", _safe_text(text)[:8000], ""])
        citations.append({"path": f"raw/{_slugify(root.name)}-repo-ingest.md", "heading": rel})

    preview_path = RAW_DIR / f"{_slugify(root.name)}-repo-ingest.md"
    saved = preview_path if dry_run else _write_raw_markdown(f"{root.name}-repo-ingest", "\n".join(lines))
    for citation in citations:
        citation["path"] = str(saved.relative_to(VAULT_ROOT))

    return _finalize_result(
        {
            "ok": True,
            "kind": "directory",
            "structure": "repository",
            "saved_paths": [str(saved.relative_to(VAULT_ROOT))],
            "doc_count": len(docs),
            "citations": citations[:20],
        },
        auto_build,
        dry_run,
    )


def ingest_google_drive(source: str, auto_build: bool = True, dry_run: bool = False) -> dict:
    payload = gs.get_drive_file_text(source)
    text_payload = payload["text"]
    mime_type = payload["mime_type"]
    name = payload["name"]

    if isinstance(text_payload, bytes) or mime_type == "application/pdf":
        pages = _extract_pdf_bytes(text_payload if isinstance(text_payload, bytes) else bytes(text_payload))
        body = _format_pdf_markdown(name, payload["web_url"], pages)
        structure = "google_drive_pdf"
        citations = [{"path": f"raw/{_slugify(name)}.md", "heading": f"Page {page['number']}", "page": page["number"]} for page in pages[:12]]
    else:
        cleaned = _safe_text(str(text_payload))
        body = "\n".join(
            [
                f"# Google Drive Ingest: {name}",
                "",
                f"Source URL: `{payload['web_url']}`",
                f"Source MIME type: `{mime_type}`",
                "",
                cleaned,
            ]
        )
        structure = "google_drive"
        citations = [{"path": f"raw/{_slugify(name)}.md", "heading": f"Google Drive Ingest: {name}"}]

    preview_path = RAW_DIR / f"{_slugify(name)}.md"
    saved = preview_path if dry_run else _write_raw_markdown(name, body)
    for citation in citations:
        citation["path"] = str(saved.relative_to(VAULT_ROOT))

    return _finalize_result(
        {
            "ok": True,
            "kind": "google_drive",
            "structure": structure,
            "saved_paths": [str(saved.relative_to(VAULT_ROOT))],
            "source_url": payload["web_url"],
            "mime_type": mime_type,
            "citations": citations,
        },
        auto_build,
        dry_run,
    )


def ingest_url(source: str, auto_build: bool = True, dry_run: bool = False) -> dict:
    if _is_google_drive_source(source):
        return ingest_google_drive(source, auto_build=auto_build, dry_run=dry_run)

    response = requests.get(source, timeout=20, headers={"User-Agent": "Jarvis/1.0"})
    response.raise_for_status()

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or source.lower().endswith(".pdf"):
        pages = _extract_pdf_bytes(response.content)
        text = _format_pdf_markdown(Path(urlparse(source).path).stem or "web-pdf", source, pages)
        structure = "pdf"
        citations = [{"path": f"raw/{_slugify(urlparse(source).netloc + urlparse(source).path.replace('/', '-'))}.md", "heading": f"Page {page['number']}", "page": page["number"]} for page in pages[:12]]
    elif "html" in content_type:
        text = "\n".join(
            [
                f"# Ingested URL: {source}",
                "",
                f"Source URL: `{source}`",
                "",
                _safe_text(_html_to_text(response.text)),
            ]
        )
        structure = "html"
        citations = [{"path": f"raw/{_slugify(urlparse(source).netloc + urlparse(source).path.replace('/', '-'))}.md", "heading": f"Ingested URL: {source}"}]
    else:
        text = "\n".join(
            [
                f"# Ingested URL: {source}",
                "",
                f"Source URL: `{source}`",
                "",
                _safe_text(response.text),
            ]
        )
        structure = "text"
        citations = [{"path": f"raw/{_slugify(urlparse(source).netloc + urlparse(source).path.replace('/', '-'))}.md", "heading": f"Ingested URL: {source}"}]

    parsed = urlparse(source)
    name = parsed.netloc + parsed.path.replace("/", "-")
    preview_path = RAW_DIR / f"{_slugify(name)}.md"
    saved = preview_path if dry_run else _write_raw_markdown(name, text)
    for citation in citations:
        citation["path"] = str(saved.relative_to(VAULT_ROOT))

    return _finalize_result(
        {
            "ok": True,
            "kind": "url",
            "structure": structure,
            "saved_paths": [str(saved.relative_to(VAULT_ROOT))],
            "citations": citations,
        },
        auto_build,
        dry_run,
    )


def ingest_source(source: str, source_type: str = "auto", auto_build: bool = True, dry_run: bool = False) -> dict:
    source_type = (source_type or "auto").lower()

    if source_type == "notes" or (source_type == "auto" and source.strip().lower() in {"notes", "my notes"}):
        return ingest_notes(auto_build=auto_build, dry_run=dry_run)

    if source_type == "google_drive":
        return ingest_google_drive(source, auto_build=auto_build, dry_run=dry_run)

    if source_type == "url" or (source_type == "auto" and _is_url(source)):
        return ingest_url(source, auto_build=auto_build, dry_run=dry_run)

    path = Path(source).expanduser()
    if source_type == "directory" or (source_type == "auto" and path.exists() and path.is_dir()):
        return ingest_directory(source, auto_build=auto_build, dry_run=dry_run)

    return ingest_file(source, auto_build=auto_build, dry_run=dry_run)


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Ingestion failed.")

    saved = ", ".join(result.get("saved_paths", []))
    structure = result.get("structure")
    structure_text = f" using preserved {structure} structure" if structure and structure not in {"text", "html"} else ""
    build = result.get("build")
    build_text = ""
    if build:
        build_text = (
            f" I then rebuilt the local wiki into {build['page_count']} compiled pages "
            f"and {build['index_doc_count']} indexed documents."
        )
    return f"Ingested the {result.get('kind', 'source')} into the vault at {saved}{structure_text}.{build_text}"
