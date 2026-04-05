"""
Promote stable vault knowledge or repeated eval patterns into local skills.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import evals
import skills
import vault
from wiki_builder import build_wiki


SKILLS_DIR = Path(__file__).resolve().parent / "skills"
INDEX_PATH = SKILLS_DIR / "index.json"
PROMOTION_THRESHOLD = 2

CATEGORY_TOOL_MAP = {
    "browser": ("browser", "local"),
    "routing": ("chat", "local"),
    "memory": ("chat", "local"),
    "self_improve": ("self_improve", "sonnet"),
    "tool_execution": ("terminal", "local"),
    "formatting": ("chat", "local"),
}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "generated-skill"


def _title_case(text: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[-_\s]+", text) if part)


def _load_index() -> dict:
    return json.loads(INDEX_PATH.read_text(encoding="utf-8"))


def _save_index(data: dict) -> None:
    INDEX_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    skills.all_skills.cache_clear()


def _upsert_skill_entry(entry: dict) -> None:
    data = _load_index()
    existing = [item for item in data.get("skills", []) if item["id"] == entry["id"]]
    if existing:
        existing[0].update(entry)
    else:
        data.setdefault("skills", []).append(entry)
    data["skills"] = sorted(data["skills"], key=lambda item: item["id"])
    _save_index(data)


def _validate_skill(skill_id: str) -> dict:
    skill = skills.get_skill(skill_id)
    if not skill:
        return {"ok": False, "error": f"Skill {skill_id} is not loadable from skills/index.json."}
    if not skill.path.exists():
        return {"ok": False, "error": f"Skill file is missing: {skill.path}"}
    body = skill.path.read_text(encoding="utf-8")
    if "Name:" not in body or "Purpose:" not in body:
        return {"ok": False, "error": f"Skill {skill_id} is missing required sections."}
    return {"ok": True}


def _validate_skill_payload(skill_id: str, body: str, reference: str, entry: dict) -> dict:
    if "Name:" not in body or "Purpose:" not in body or "Rules:" not in body:
        return {"ok": False, "error": f"Generated skill {skill_id} is missing required sections."}
    if not reference.strip():
        return {"ok": False, "error": f"Generated skill {skill_id} has no reference material."}
    required_keys = {"id", "name", "description", "tool", "cost_hint", "triggers", "path", "resources"}
    missing = sorted(required_keys - set(entry))
    if missing:
        return {"ok": False, "error": f"Generated skill {skill_id} is missing index keys: {', '.join(missing)}."}
    if entry["path"] != f"{skill_id}/SKILL.md":
        return {"ok": False, "error": f"Generated skill {skill_id} has an invalid skill path."}
    if not entry["resources"]:
        return {"ok": False, "error": f"Generated skill {skill_id} has no reference resources."}
    return {"ok": True}


def _snapshot_skill_state(skill_id: str) -> dict:
    skill_dir = SKILLS_DIR / skill_id
    state = {
        "index": _load_index(),
        "skill_exists": (skill_dir / "SKILL.md").exists(),
        "ref_exists": (skill_dir / "references" / "vault_context.md").exists(),
        "skill_body": None,
        "ref_body": None,
    }
    if state["skill_exists"]:
        state["skill_body"] = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    if state["ref_exists"]:
        state["ref_body"] = (skill_dir / "references" / "vault_context.md").read_text(encoding="utf-8")
    return state


def _restore_skill_state(skill_id: str, state: dict) -> None:
    skill_dir = SKILLS_DIR / skill_id
    refs_dir = skill_dir / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    _save_index(state["index"])

    skill_path = skill_dir / "SKILL.md"
    ref_path = refs_dir / "vault_context.md"

    if state["skill_exists"] and state["skill_body"] is not None:
        skill_path.write_text(state["skill_body"], encoding="utf-8")
    else:
        skill_path.unlink(missing_ok=True)

    if state["ref_exists"] and state["ref_body"] is not None:
        ref_path.write_text(state["ref_body"], encoding="utf-8")
    else:
        ref_path.unlink(missing_ok=True)

    if refs_dir.exists() and not any(refs_dir.iterdir()):
        refs_dir.rmdir()
    if skill_dir.exists() and not any(skill_dir.iterdir()):
        skill_dir.rmdir()


def create_skill_from_vault(query: str, tool: str = "chat", cost_hint: str = "local", dry_run: bool = False) -> dict:
    matches = vault.search(query, topn=3)
    if not matches:
        return {"ok": False, "error": f"No relevant vault material found for {query}."}

    primary = matches[0]
    skill_id = _slugify(primary["title"])
    name = _title_case(skill_id)
    keywords = [kw for kw in primary.get("keywords", [])[:6] if len(kw) > 2]
    triggers = sorted(set([query.lower(), primary["title"].lower(), *keywords]))[:8]

    skill_dir = SKILLS_DIR / skill_id
    refs_dir = skill_dir / "references"
    if not dry_run:
        refs_dir.mkdir(parents=True, exist_ok=True)

    rule_lines = []
    for match in matches:
        citation = match.get("citation", {}).get("label", match["path"])
        rule_lines.append(f"- Ground answers in `{citation}` before broader reasoning.")
    rule_lines.append("- Use only the smallest relevant snippet needed for the current request.")
    rule_lines.append("- If the local vault evidence is weak, say that plainly before escalating.")
    rule_lines.append("- Cite the exact local file and heading you used whenever you answer from this skill.")

    body = "\n".join(
        [
            f"Name: {name}",
            "",
            "Purpose:",
            f"Use local vault knowledge about {primary['title']} before relying on broader model reasoning.",
            "",
            "Rules:",
            *rule_lines,
        ]
    )
    if not dry_run:
        (skill_dir / "SKILL.md").write_text(body + "\n", encoding="utf-8")

    reference = "\n\n".join(
        [
            "\n".join(
                [
                    f"# Source: {match['title']}",
                    f"Path: `{match['path']}`",
                    f"Citation: `{match.get('citation', {}).get('label', match['path'])}`",
                    "",
                    match["excerpt"],
                ]
            )
            for match in matches
        ]
    )
    reference_path = refs_dir / "vault_context.md"
    if not dry_run:
        reference_path.write_text(reference + "\n", encoding="utf-8")

    entry = {
        "id": skill_id,
        "name": name,
        "description": f"Use local vault knowledge about {primary['title']}.",
        "tool": tool,
        "cost_hint": cost_hint,
        "triggers": triggers,
        "path": f"{skill_id}/SKILL.md",
        "resources": [f"{skill_id}/references/vault_context.md"],
    }
    payload_validation = _validate_skill_payload(skill_id, body, reference, entry)
    if not payload_validation["ok"]:
        return payload_validation
    if not dry_run:
        snapshot = _snapshot_skill_state(skill_id)
        try:
            _upsert_skill_entry(entry)
            validation = _validate_skill(skill_id)
            if not validation["ok"]:
                raise RuntimeError(validation["error"])
        except Exception as exc:
            _restore_skill_state(skill_id, snapshot)
            return {"ok": False, "error": f"Skill creation rolled back: {exc}"}

    return {
        "ok": True,
        "skill_id": skill_id,
        "name": name,
        "source_paths": [match["path"] for match in matches],
        "dry_run": dry_run,
    }


def _write_failure_vault_page(category: str, failures: list[dict]) -> dict:
    lines = [f"# Eval Pattern: {category}", ""]
    for failure in failures:
        lines.extend(
            [
                f"## {failure['id']}",
                f"Issue: {failure['issue']}",
                f"Expected: {failure.get('expected', '')}",
                f"User input: {failure.get('user_input', '')}",
                f"Response: {failure.get('response', '')[:500]}",
                "",
            ]
        )
    raw_path = vault.RAW_DIR / f"eval-{_slugify(category)}-patterns.md"
    raw_path.write_text("\n".join(lines), encoding="utf-8")
    build = build_wiki()
    return {
        "ok": True,
        "path": str(raw_path.relative_to(vault.VAULT_ROOT)),
        "build": build,
    }


def promote_failures(min_failures: int = PROMOTION_THRESHOLD) -> dict:
    failures = evals.recent_failures(limit=20, hours=24 * 14)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for failure in failures:
        grouped[failure["category"]].append(failure)

    repeated = [(category, items) for category, items in grouped.items() if len(items) >= min_failures]
    if not repeated:
        return {"ok": False, "error": f"Not enough repeated eval failures to promote. Need at least {min_failures} in one category."}

    repeated.sort(key=lambda item: len(item[1]), reverse=True)
    category, items = repeated[0]
    page_result = _write_failure_vault_page(category, items)
    if not page_result.get("ok"):
        return page_result
    build = page_result.get("build") or {}
    if build.get("page_count", 0) <= 0 or build.get("index_doc_count", 0) <= 0:
        return {
            "ok": False,
            "error": "Eval promotion did not produce a valid wiki build, so I did not create a skill.",
            "vault_page": page_result,
        }

    tool, cost_hint = CATEGORY_TOOL_MAP.get(category, ("chat", "local"))
    skill_result = create_skill_from_vault(f"eval pattern {category}", tool=tool, cost_hint=cost_hint)

    return {
        "ok": True,
        "category": category,
        "failure_count": len(items),
        "vault_page": page_result,
        "skill": skill_result if skill_result.get("ok") else None,
        "skill_error": None if skill_result.get("ok") else skill_result.get("error"),
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Skill generation failed.")

    if "category" in result:
        base = (
            f"Promoted repeated {result['category']} failures into the vault. "
            f"I captured {result['failure_count']} failures at {result['vault_page']['path']}."
        )
        if result.get("skill"):
            base += f" I also created the skill {result['skill']['skill_id']}."
        elif result.get("skill_error"):
            base += f" I did not create a skill because {result['skill_error']}"
        return base

    return f"Created the skill {result['skill_id']} from local vault sources."
