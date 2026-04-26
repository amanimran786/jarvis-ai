"""Preview Jarvis skill exports for shared agent-skill hosts.

This module is intentionally preview-only by default. Jarvis's canonical skill
registry remains ``skills/index.json`` plus ``skills/<id>/SKILL.md``; the
``.agents/skills`` layout is a compatibility surface for Codex, Gemini CLI,
OpenCode, and similar hosts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import skills


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_EXPORT_ROOT = REPO_ROOT / ".agents" / "skills"


@dataclass(frozen=True)
class SkillExportPreview:
    skill_id: str
    name: str
    source_path: str
    target_path: str
    action: str
    ok: bool
    warnings: tuple[str, ...]
    source_sha256: str


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "skill"


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _warnings_for(skill: skills.Skill) -> tuple[str, ...]:
    warnings: list[str] = []
    if not skill.path.exists():
        warnings.append("missing SKILL.md")
    if not skill.description.strip():
        warnings.append("missing description")
    if not skill.triggers:
        warnings.append("missing positive triggers")
    if not skill.negative_triggers:
        warnings.append("missing negative triggers")
    return tuple(warnings)


def render_export_content(skill: skills.Skill) -> str:
    """Render the compatibility SKILL.md content that would be exported."""
    body = skill.path.read_text(encoding="utf-8").strip() if skill.path.exists() else ""
    frontmatter = {
        "name": skill.name,
        "description": skill.description,
        "jarvis_skill_id": skill.id,
        "source": "jarvis-local",
        "source_path": _relative(skill.path),
        "source_sha256": _sha256(skill.path),
        "tool": skill.tool,
        "cost_hint": skill.cost_hint,
        "triggers": list(skill.triggers),
        "negative_triggers": list(skill.negative_triggers),
    }
    metadata = json.dumps(frontmatter, indent=2, sort_keys=True)
    metadata = "\n".join(f"# {line}" for line in metadata.splitlines())
    return (
        "---\n"
        "jarvis_export: true\n"
        f"{metadata}\n"
        "---\n\n"
        "<!-- Generated compatibility preview from Jarvis canonical skills. -->\n"
        "<!-- Do not edit this copy directly; edit skills/index.json and the canonical SKILL.md. -->\n\n"
        f"{body}\n"
    )


def preview_skill_exports(export_root: Path = DEFAULT_EXPORT_ROOT) -> dict:
    previews: list[SkillExportPreview] = []
    for skill in skills.all_skills():
        target = export_root / _slug(skill.id) / "SKILL.md"
        warnings = _warnings_for(skill)
        action = "create"
        if target.exists():
            current = target.read_text(encoding="utf-8")
            desired = render_export_content(skill)
            action = "unchanged" if current == desired else "update"
        previews.append(
            SkillExportPreview(
                skill_id=skill.id,
                name=skill.name,
                source_path=_relative(skill.path),
                target_path=_relative(target),
                action=action,
                ok=not warnings,
                warnings=warnings,
                source_sha256=_sha256(skill.path),
            )
        )

    counts: dict[str, int] = {}
    for item in previews:
        counts[item.action] = counts.get(item.action, 0) + 1

    return {
        "mode": "preview_only",
        "canonical_source": _relative(skills.INDEX_PATH),
        "export_root": _relative(export_root),
        "would_write": False,
        "skill_count": len(previews),
        "action_counts": counts,
        "warnings_count": sum(len(item.warnings) for item in previews),
        "next_step": "Run an explicit reviewed export command after inspecting this preview.",
        "skills": [asdict(item) for item in previews],
    }


def format_preview(limit: int = 12) -> str:
    payload = preview_skill_exports()
    lines = [
        "Jarvis Skill Export Preview",
        f"Mode          : {payload['mode']}",
        f"Canonical     : {payload['canonical_source']}",
        f"Export target : {payload['export_root']}",
        f"Would write   : {'yes' if payload['would_write'] else 'no'}",
        f"Skills        : {payload['skill_count']}",
        f"Warnings      : {payload['warnings_count']}",
        "",
        "Sample",
    ]
    for item in payload["skills"][:limit]:
        warning_text = f" | warnings: {', '.join(item['warnings'])}" if item["warnings"] else ""
        lines.append(f"  {item['action']}: {item['skill_id']} -> {item['target_path']}{warning_text}")
    if payload["skill_count"] > limit:
        lines.append(f"  ... {payload['skill_count'] - limit} more")
    lines.extend(
        [
            "",
            "Policy: preview-only. This command does not create .agents/skills.",
            "Next: add a reviewed export command once the preview is clean enough.",
        ]
    )
    return "\n".join(lines)
