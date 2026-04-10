"""
Graphify-backed repo grounding for Jarvis.

This module reads the generated Graphify artifacts under graphify-out/ and
turns them into a compact prompt supplement for repo/codebase questions.
"""

from __future__ import annotations

import json
import re
from collections import deque
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent
GRAPH_DIR = ROOT / "graphify-out"
GRAPH_PATH = GRAPH_DIR / "graph.json"
REPORT_PATH = GRAPH_DIR / "GRAPH_REPORT.md"
ANALYSIS_PATH = GRAPH_DIR / "analysis.json"

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "does", "for", "from",
    "how", "i", "if", "in", "into", "is", "it", "of", "on", "or", "that", "the",
    "this", "to", "what", "when", "where", "which", "who", "why", "with", "work",
}
_REPO_PATTERNS = (
    "repo", "repository", "codebase", "in this project", "in this repo",
    "which file", "what file", "which module", "what module", "which class",
    "which function", "where is", "how does jarvis", "how does the app",
    "call path", "flow of", "defined in", "implemented in", "wired up",
    "used by", "references", "restored window", "meeting toolbar",
    "smart listen", "api route", "runtime state",
)
_GENERIC_LABELS = {
    "jarvis", "window", "tests", "test", "function", "class", "module", "community",
    "code", "file", "route", "helper", "status", "current", "build", "refresh",
}


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _looks_identifier(token: str) -> bool:
    return (
        "_" in token
        or "(" in token
        or ")" in token
        or re.search(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", token) is not None
    )


def _path_label(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except Exception:
        return path


def _display_label(label: str) -> str:
    return (label or "").lstrip(".")


def _node_text(node: dict) -> str:
    return " ".join(
        part
        for part in (
            node.get("label", ""),
            Path(node.get("source_file", "")).name,
            node.get("source_file", ""),
            node.get("source_location", ""),
        )
        if part
    )


def _link_key(link: dict) -> tuple[str, str, str]:
    src = str(link.get("source", ""))
    tgt = str(link.get("target", ""))
    relation = link.get("relation", "")
    ordered = tuple(sorted((src, tgt)))
    return ordered[0], ordered[1], relation


def _extract_summary_lines(text: str) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    summary: list[str] = []
    in_summary = False
    for line in lines:
        if line.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if in_summary and line.startswith("- "):
            summary.append(line[2:].strip())
    return summary[:2]


def _query_is_repo_related(query: str, tool: str | None = None) -> bool:
    lower = (query or "").lower()
    if tool in {"code", "coding"}:
        return True
    if any(pattern in lower for pattern in _REPO_PATTERNS):
        return True
    if re.search(r"\b[\w/-]+\.(py|ts|tsx|js|jsx|json|md)\b", lower):
        return True
    tokens = query.split()
    if any(_looks_identifier(token) for token in tokens):
        return True
    return False


@lru_cache(maxsize=1)
def _load_payload() -> dict | None:
    if not GRAPH_PATH.exists():
        return None

    try:
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    try:
        analysis = json.loads(ANALYSIS_PATH.read_text(encoding="utf-8")) if ANALYSIS_PATH.exists() else {}
    except (OSError, json.JSONDecodeError):
        analysis = {}

    node_map: dict[str, dict] = {}
    adjacency: dict[str, list[dict]] = {}
    nodes = graph.get("nodes", [])
    links = graph.get("links", graph.get("edges", []))

    for raw_node in nodes:
        node = dict(raw_node)
        node_id = str(node.get("id", ""))
        if not node_id:
            continue
        node["id"] = node_id
        node_map[node_id] = node
        adjacency.setdefault(node_id, [])

    extracted = inferred = ambiguous = 0
    normalized_links = []
    for raw_link in links:
        link = dict(raw_link)
        src = str(link.get("source", ""))
        tgt = str(link.get("target", ""))
        if not src or not tgt:
            continue
        link["source"] = src
        link["target"] = tgt
        normalized_links.append(link)
        adjacency.setdefault(src, []).append(link)
        adjacency.setdefault(tgt, []).append(link)
        confidence = link.get("confidence", "EXTRACTED")
        if confidence == "EXTRACTED":
            extracted += 1
        elif confidence == "INFERRED":
            inferred += 1
        elif confidence == "AMBIGUOUS":
            ambiguous += 1

    labels_raw = analysis.get("labels", {})
    labels = {int(key): value for key, value in labels_raw.items()} if labels_raw else {}

    report_summary = _extract_summary_lines(REPORT_PATH.read_text(encoding="utf-8")) if REPORT_PATH.exists() else []
    return {
        "nodes": node_map,
        "links": normalized_links,
        "adjacency": adjacency,
        "labels": labels,
        "report_summary": report_summary,
        "stats": {
            "node_count": len(node_map),
            "edge_count": len(normalized_links),
            "community_count": len(labels) or len({node.get("community") for node in node_map.values() if node.get("community") is not None}),
            "extracted": extracted,
            "inferred": inferred,
            "ambiguous": ambiguous,
        },
    }


def invalidate() -> None:
    _load_payload.cache_clear()


def status() -> dict:
    payload = _load_payload()
    if not payload:
        return {"ready": False, "graph_path": str(GRAPH_PATH)}
    return {
        "ready": True,
        "graph_path": str(GRAPH_PATH),
        "report_path": str(REPORT_PATH),
        **payload["stats"],
    }


def _score_node(query: str, query_tokens: set[str], node: dict) -> int:
    label = node.get("label", "")
    source_file = node.get("source_file", "")
    label_lower = label.lower()
    source_lower = source_file.lower()
    basename = Path(source_file).name.lower() if source_file else ""
    score = 0

    if label_lower and label_lower in query:
        score += 12
    if basename and basename in query:
        score += 14
    if source_lower and source_lower in query:
        score += 16

    overlap = query_tokens & set(_tokenize(_node_text(node)))
    score += len(overlap) * 3

    if node.get("file_type") == "code":
        score += 1
    if "/tests/" in source_lower or basename.startswith("test_"):
        score -= 2
    return score


def search(query: str, topn: int = 5) -> dict:
    payload = _load_payload()
    if not payload:
        return {"nodes": [], "edges": []}
    if not _query_is_repo_related(query):
        return {"nodes": [], "edges": []}

    lower = query.lower()
    query_tokens = set(_tokenize(query))
    node_scores: dict[str, int] = {}

    for node_id, node in payload["nodes"].items():
        score = _score_node(lower, query_tokens, node)
        if score > 0:
            node_scores[node_id] = score

    ranked_nodes = sorted(
        ((score, payload["nodes"][node_id]) for node_id, score in node_scores.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    if not ranked_nodes or ranked_nodes[0][0] < 4:
        return {"nodes": [], "edges": []}

    top_nodes = [node for _, node in ranked_nodes[:topn]]
    edge_scores: list[tuple[int, dict]] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for node in top_nodes[: max(topn, 3)]:
        for link in payload["adjacency"].get(node["id"], []):
            key = _link_key(link)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            src = payload["nodes"].get(str(link["source"]))
            tgt = payload["nodes"].get(str(link["target"]))
            if not src or not tgt:
                continue
            score = node_scores.get(src["id"], 0) + node_scores.get(tgt["id"], 0)
            relation = link.get("relation", "").lower()
            if relation and relation in lower:
                score += 3
            confidence = link.get("confidence", "EXTRACTED")
            if confidence == "EXTRACTED":
                score += 2
            elif confidence == "INFERRED":
                score += 1
            if score > 0:
                edge_scores.append((score, link))

    ranked_edges = [link for _, link in sorted(edge_scores, key=lambda item: item[0], reverse=True)[:topn]]
    return {"nodes": top_nodes, "edges": ranked_edges}


def query_graph(query: str, topn: int = 8) -> dict:
    payload = _load_payload()
    if not payload:
        return {"ready": False, "nodes": [], "edges": [], "stats": {}}
    results = search(query, topn=max(1, topn))
    return {
        "ready": True,
        "query": query,
        "stats": payload["stats"],
        "nodes": results.get("nodes", []),
        "edges": results.get("edges", []),
    }


def _resolve_nodes(term: str, payload: dict, limit: int = 5) -> list[dict]:
    needle = (term or "").strip().lower()
    if not needle:
        return []
    exact = []
    fuzzy = []
    for node in payload["nodes"].values():
        node_id = str(node.get("id", "")).lower()
        label = str(node.get("label", "")).lower()
        hay = f"{node_id} {label} {node.get('source_file', '').lower()}"
        if needle == node_id or needle == label:
            exact.append(node)
        elif needle in hay:
            fuzzy.append(node)
    if exact:
        return exact[:limit]
    return fuzzy[:limit]


def _find_link_between(payload: dict, src_id: str, tgt_id: str) -> dict | None:
    for link in payload["adjacency"].get(src_id, []):
        a = str(link.get("source", ""))
        b = str(link.get("target", ""))
        if {a, b} == {src_id, tgt_id}:
            return link
    return None


def shortest_path(source_term: str, target_term: str, max_depth: int = 6) -> dict:
    payload = _load_payload()
    if not payload:
        return {"ok": False, "error": "graph_not_ready", "path": []}

    sources = _resolve_nodes(source_term, payload, limit=3)
    targets = _resolve_nodes(target_term, payload, limit=3)
    if not sources:
        return {"ok": False, "error": "source_not_found", "path": [], "source_matches": []}
    if not targets:
        return {"ok": False, "error": "target_not_found", "path": [], "target_matches": []}

    target_ids = {node["id"] for node in targets}
    queue = deque([(src["id"], [src["id"]]) for src in sources])
    visited = {src["id"] for src in sources}

    best_path: list[str] | None = None
    while queue:
        node_id, path = queue.popleft()
        if node_id in target_ids:
            best_path = path
            break
        if len(path) > max_depth:
            continue
        for link in payload["adjacency"].get(node_id, []):
            nxt = str(link.get("target")) if str(link.get("source")) == node_id else str(link.get("source"))
            if not nxt or nxt in visited:
                continue
            visited.add(nxt)
            queue.append((nxt, path + [nxt]))

    if not best_path:
        return {
            "ok": False,
            "error": "path_not_found",
            "path": [],
            "source_matches": [n.get("id", "") for n in sources],
            "target_matches": [n.get("id", "") for n in targets],
        }

    nodes = [payload["nodes"].get(node_id, {"id": node_id, "label": node_id}) for node_id in best_path]
    edges = []
    for left, right in zip(best_path, best_path[1:]):
        link = _find_link_between(payload, left, right)
        if link:
            edges.append(link)

    return {
        "ok": True,
        "path": best_path,
        "nodes": nodes,
        "edges": edges,
        "source_matches": [n.get("id", "") for n in sources],
        "target_matches": [n.get("id", "") for n in targets],
    }


def context_for_query(query: str, tool: str | None = None, topn: int = 5, max_chars: int = 1400) -> str:
    payload = _load_payload()
    if not payload or not _query_is_repo_related(query, tool=tool):
        return ""

    results = search(query, topn=topn)
    if not results["nodes"] and not results["edges"]:
        return ""

    stats = payload["stats"]
    lines = [
        "Relevant Graphify repo context (supporting codebase context from the current graph; not runtime state):",
        (
            f"- Repo graph: {stats['node_count']} nodes, {stats['edge_count']} edges, "
            f"{stats['community_count']} communities."
        ),
        (
            f"- Edge confidence mix: {stats['extracted']} EXTRACTED, "
            f"{stats['inferred']} INFERRED, {stats['ambiguous']} AMBIGUOUS."
        ),
    ]
    for summary_line in payload.get("report_summary", []):
        if "nodes" in summary_line or "Extraction" in summary_line:
            continue
        lines.append(f"- {summary_line}")

    if results["nodes"]:
        lines.append("Relevant nodes:")
        for node in results["nodes"]:
            path = _path_label(node.get("source_file", ""))
            community_id = node.get("community")
            community_label = payload["labels"].get(community_id, f"Community {community_id}") if community_id is not None else "Unassigned"
            location = node.get("source_location", "")
            location_text = f":{location}" if location else ""
            lines.append(
                f"- {_display_label(node.get('label', node['id']))} [{path}{location_text}; {community_label}]"
            )

    if results["edges"]:
        lines.append("Relevant relationships:")
        for link in results["edges"]:
            src = payload["nodes"].get(str(link["source"]), {})
            tgt = payload["nodes"].get(str(link["target"]), {})
            relation = link.get("relation", "related_to")
            confidence = link.get("confidence", "EXTRACTED")
            path = _path_label(link.get("source_file", "")) or _path_label(src.get("source_file", ""))
            location = link.get("source_location", "")
            suffix = f" ({path}:{location})" if path and location else (f" ({path})" if path else "")
            lines.append(
                f"- {_display_label(src.get('label', link['source']))} --{relation}--> {_display_label(tgt.get('label', link['target']))} "
                f"[{confidence}]{suffix}"
            )

    text = "\n".join(lines)
    return text[:max_chars].rstrip()
