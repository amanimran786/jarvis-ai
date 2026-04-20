"""
Local skill registry for Jarvis.

L1 metadata lives in skills/index.json and is always cheap to load.
L2 instructions live in skills/<skill_id>/SKILL.md and load only for the
selected request.
L3 resources live under each skill's references/ directory and are also loaded
only when that skill is active.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


SKILLS_DIR = Path(__file__).resolve().parent / "skills"
INDEX_PATH = SKILLS_DIR / "index.json"


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    tool: str
    cost_hint: str
    triggers: tuple[str, ...]
    negative_triggers: tuple[str, ...]
    path: Path
    resources: tuple[Path, ...]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _normalize(text)))


@lru_cache(maxsize=1)
def all_skills() -> tuple[Skill, ...]:
    if not INDEX_PATH.exists():
        return ()

    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    skills = []
    for item in raw.get("skills", []):
        path = SKILLS_DIR / item["path"]
        resources = tuple(SKILLS_DIR / ref for ref in item.get("resources", []))
        skills.append(
            Skill(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                tool=item.get("tool", "chat"),
                cost_hint=item.get("cost_hint", "local"),
                triggers=tuple(item.get("triggers", [])),
                negative_triggers=tuple(item.get("negative_triggers", [])),
                path=path,
                resources=resources,
            )
        )
    return tuple(skills)


def get_skill(skill_id: str | None) -> Skill | None:
    if not skill_id:
        return None
    for skill in all_skills():
        if skill.id == skill_id:
            return skill
    return None


def match_skills(user_input: str, limit: int = 3) -> list[tuple[Skill, int]]:
    lower = _normalize(user_input)
    tokens = _tokenize(user_input)
    matches: list[tuple[Skill, int]] = []

    for skill in all_skills():
        negative_triggers = [_normalize(trigger) for trigger in skill.negative_triggers]
        if any(trigger and trigger in lower for trigger in negative_triggers):
            continue
        score = 0
        for trigger in skill.triggers:
            trigger_lower = _normalize(trigger)
            if not trigger_lower:
                continue
            if trigger_lower in lower:
                score += 8 if " " in trigger_lower else 4
        for token in _tokenize(skill.name + " " + skill.description):
            if token in tokens:
                score += 1
        if score > 0:
            matches.append((skill, score))

    matches.sort(key=lambda item: item[1], reverse=True)
    return matches[:limit]


def choose_skill(user_input: str, tool: str | None = None) -> Skill | None:
    for skill, score in match_skills(user_input, limit=5):
        if tool and skill.tool != tool:
            continue
        if score >= 4:
            return skill
    return None


def choose_skills(
    user_input: str,
    tool: str | None = None,
    limit: int = 3,
    min_score: int = 4,
    include_chat_helpers: bool = True,
) -> list[Skill]:
    selected: list[Skill] = []
    seen: set[str] = set()

    for skill, score in match_skills(user_input, limit=max(limit * 4, 8)):
        if score < min_score:
            continue
        if tool:
            allowed = skill.tool == tool or (include_chat_helpers and tool == "chat" and skill.tool == "chat")
            if not allowed:
                continue
        if skill.id in seen:
            continue
        selected.append(skill)
        seen.add(skill.id)
        if len(selected) >= limit:
            break

    return selected


def metadata_block(user_input: str, tool: str | None = None, limit: int = 3) -> str:
    lines = []
    for skill, score in match_skills(user_input, limit=limit):
        if tool and skill.tool != tool:
            continue
        negative = f' | negative_triggers: {", ".join(skill.negative_triggers[:4])}' if skill.negative_triggers else ""
        lines.append(
            f'- id: "{skill.id}" | tool: "{skill.tool}" | cost_hint: "{skill.cost_hint}" | '
            f'description: "{skill.description}" | triggers: {", ".join(skill.triggers[:4])}{negative}'
        )
    return "\n".join(lines)


def load_skill_bundle(skill_id: str | None) -> str:
    skill = get_skill(skill_id)
    if not skill:
        return ""

    sections = [
        f"Active local skill: {skill.name} ({skill.id})",
        f"Description: {skill.description}",
        "Use this skill only for the current request.",
    ]

    if skill.path.exists():
        body = skill.path.read_text(encoding="utf-8").strip()
        if body:
            sections.append("Skill instructions:")
            sections.append(body)

    resource_chunks = []
    for resource in skill.resources:
        if resource.exists():
            text = resource.read_text(encoding="utf-8").strip()
            if text:
                resource_chunks.append(f"[{resource.name}]\n{text}")

    if resource_chunks:
        sections.append("Referenced resources:")
        sections.extend(resource_chunks)

    return "\n\n".join(sections)


def load_skill_bundles(skill_ids: list[str]) -> str:
    bundles = []
    for skill_id in skill_ids:
        bundle = load_skill_bundle(skill_id)
        if bundle:
            bundles.append(bundle)
    if not bundles:
        return ""
    active = ", ".join(skill_ids)
    return f"Active skill stack: {active}\n\n" + "\n\n".join(bundles)


def resolve_skills(user_input: str, skill_id: str | None = None, tool: str | None = None, limit: int = 3) -> list[Skill]:
    explicit = get_skill(skill_id)
    resolved: list[Skill] = []
    seen: set[str] = set()

    if explicit:
        resolved.append(explicit)
        seen.add(explicit.id)

    for skill in choose_skills(user_input, tool=tool, limit=limit):
        if skill.id in seen:
            continue
        resolved.append(skill)
        seen.add(skill.id)
        if len(resolved) >= limit:
            break

    return resolved


def build_system_extra(user_input: str, skill_id: str | None = None, tool: str | None = None, limit: int = 3) -> tuple[str, list[Skill]]:
    resolved = resolve_skills(user_input, skill_id=skill_id, tool=tool, limit=limit)
    if not resolved:
        return "", []
    return load_skill_bundles([skill.id for skill in resolved]), resolved


def skill_cost_hint(skill_ref) -> str | None:
    if not skill_ref:
        return None

    if isinstance(skill_ref, Skill):
        return skill_ref.cost_hint

    if isinstance(skill_ref, str):
        skill = get_skill(skill_ref)
        return skill.cost_hint if skill else None

    hints = [skill_cost_hint(item) for item in skill_ref]
    hints = [hint for hint in hints if hint]
    if not hints:
        return None

    order = {"local": 0, "mini": 1, "haiku": 2, "sonnet": 3, "opus": 4}
    return max(hints, key=lambda hint: order.get(hint, 0))
