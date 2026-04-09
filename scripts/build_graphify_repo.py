#!/usr/bin/env python3
"""
Build Graphify artifacts for the current Jarvis repo.

Outputs:
  - graphify-out/graph.json
  - graphify-out/GRAPH_REPORT.md
  - graphify-out/analysis.json

Optional:
  - graphify-out/wiki/
  - graphify-out/obsidian/
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.detect import detect
from graphify.export import to_json, to_obsidian
from graphify.extract import extract
from graphify.report import generate
from graphify.wiki import to_wiki


_GENERIC_TOKENS = {
    "community", "tests", "test", "jarvis", "window", "router", "status",
    "refresh", "build", "current", "helper", "module", "function", "class",
}


def _tokenize(text: str) -> list[str]:
    return [
        token for token in re.findall(r"[a-z0-9_]+", (text or "").lower())
        if len(token) > 2 and token not in _GENERIC_TOKENS
    ]


def _label_communities(graph, communities: dict[int, list[str]]) -> dict[int, str]:
    labels: dict[int, str] = {}
    for cid, node_ids in communities.items():
        stem_counts: Counter[str] = Counter()
        label_counts: Counter[str] = Counter()
        for node_id in node_ids:
            data = graph.nodes[node_id]
            source_file = data.get("source_file", "")
            if source_file:
                stem = Path(source_file).stem.replace("_", " ").strip()
                if stem:
                    stem_counts[stem] += 3
            label = data.get("label", "")
            if label.endswith(".py") or label.startswith("_"):
                continue
            for token in _tokenize(label):
                label_counts[token] += 1

        source_terms = [term for term, _ in stem_counts.most_common(2)]
        label_terms = [term for term, _ in label_counts.most_common(2)]
        parts = source_terms or label_terms
        if not parts:
            labels[cid] = f"Community {cid}"
            continue
        cleaned = " / ".join(parts[:2]).replace("  ", " ").strip(" /")
        labels[cid] = cleaned.title()
    return labels


def build_graph(root: Path, *, export_wiki: bool = False, export_obsidian: bool = False) -> dict:
    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)

    detection = detect(root)
    code_paths = [Path(path) for path in detection["files"].get("code", [])]
    extraction = extract(code_paths)
    graph = build_from_json(extraction)
    communities = cluster(graph)
    cohesion = score_all(graph, communities)
    labels = _label_communities(graph, communities)
    gods = god_nodes(graph)
    surprises = surprising_connections(graph, communities)
    questions = suggest_questions(graph, communities, labels)
    token_cost = {
        "input": extraction.get("input_tokens", 0),
        "output": extraction.get("output_tokens", 0),
    }

    report = generate(
        graph,
        communities,
        cohesion,
        labels,
        gods,
        surprises,
        detection,
        token_cost,
        str(root),
        suggested_questions=questions,
    )

    analysis = {
        "communities": {str(key): value for key, value in communities.items()},
        "cohesion": {str(key): value for key, value in cohesion.items()},
        "gods": gods,
        "surprises": surprises,
        "questions": questions,
        "labels": {str(key): value for key, value in labels.items()},
    }

    (out / ".graphify_detect.json").write_text(json.dumps(detection, indent=2), encoding="utf-8")
    (out / ".graphify_extract.json").write_text(json.dumps(extraction, indent=2), encoding="utf-8")
    (out / "analysis.json").write_text(json.dumps(analysis, indent=2), encoding="utf-8")
    (out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    to_json(graph, communities, str(out / "graph.json"))

    if export_wiki:
        to_wiki(
            graph,
            communities,
            out / "wiki",
            community_labels=labels,
            cohesion=cohesion,
            god_nodes_data=gods,
        )
    if export_obsidian:
        to_obsidian(
            graph,
            communities,
            str(out / "obsidian"),
            community_labels=labels,
            cohesion=cohesion,
        )

    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "community_count": len(communities),
        "code_file_count": len(code_paths),
        "labels": labels,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Graphify artifacts for this repo.")
    parser.add_argument("--root", default=".", help="Repository root to graphify.")
    parser.add_argument("--wiki", action="store_true", help="Also export graphify-out/wiki/")
    parser.add_argument("--obsidian", action="store_true", help="Also export graphify-out/obsidian/")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    result = build_graph(root, export_wiki=args.wiki, export_obsidian=args.obsidian)
    print(
        json.dumps(
            {
                "root": str(root),
                "nodes": result["node_count"],
                "edges": result["edge_count"],
                "communities": result["community_count"],
                "code_files": result["code_file_count"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
