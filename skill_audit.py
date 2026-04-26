"""Local diagnostics for Jarvis skill packages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import skills


REPO_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class SkillAuditIssue:
    skill_id: str
    severity: str
    code: str
    message: str
    path: str


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _issue(skill: skills.Skill, severity: str, code: str, message: str, path: Path | None = None) -> SkillAuditIssue:
    return SkillAuditIssue(
        skill_id=skill.id,
        severity=severity,
        code=code,
        message=message,
        path=_relative(path or skill.path),
    )


def audit_skill(skill: skills.Skill) -> list[SkillAuditIssue]:
    issues: list[SkillAuditIssue] = []

    if not skill.path.exists():
        return [_issue(skill, "error", "missing_skill_file", "SKILL.md is missing.")]

    body = skill.path.read_text(encoding="utf-8").strip()
    lower_body = body.lower()

    if not skill.description.strip():
        issues.append(_issue(skill, "error", "missing_description", "Skill registry entry has no description."))
    if len(skill.description.strip()) < 40:
        issues.append(_issue(skill, "warning", "short_description", "Description may be too vague for routing."))
    if not skill.triggers:
        issues.append(_issue(skill, "error", "missing_triggers", "Skill has no positive triggers."))
    elif len(skill.triggers) < 3:
        issues.append(_issue(skill, "warning", "few_triggers", "Skill has fewer than 3 positive triggers."))
    if not skill.negative_triggers:
        issues.append(_issue(skill, "warning", "missing_negative_triggers", "Skill has no negative triggers."))

    if not body.startswith("Name:"):
        issues.append(_issue(skill, "warning", "missing_name_header", "SKILL.md should start with a Name header."))
    if "purpose:" not in lower_body:
        issues.append(_issue(skill, "warning", "missing_purpose", "SKILL.md should include a Purpose section."))
    if "rules:" not in lower_body:
        issues.append(_issue(skill, "warning", "missing_rules", "SKILL.md should include concrete Rules."))
    if "do not" not in lower_body and "never " not in lower_body and not skill.negative_triggers:
        issues.append(
            _issue(
                skill,
                "warning",
                "missing_boundary_language",
                "Skill has no negative triggers and no obvious boundary language.",
            )
        )

    for resource in skill.resources:
        if not resource.exists():
            issues.append(_issue(skill, "error", "missing_resource", f"Missing resource: {_relative(resource)}", resource))

    return issues


def audit_skills() -> dict:
    all_items = list(skills.all_skills())
    issues: list[SkillAuditIssue] = []
    for skill in all_items:
        issues.extend(audit_skill(skill))

    by_severity = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        by_severity[issue.severity] = by_severity.get(issue.severity, 0) + 1

    return {
        "ok": by_severity.get("error", 0) == 0,
        "skill_count": len(all_items),
        "issue_count": len(issues),
        "by_severity": by_severity,
        "next_step": "Fix errors first, then add negative triggers to broad skills before real .agents/skills export.",
        "issues": [asdict(issue) for issue in issues],
    }


def format_audit(limit: int = 30) -> str:
    payload = audit_skills()
    lines = [
        "Jarvis Skill Audit",
        f"Status   : {'PASS' if payload['ok'] else 'FAIL'}",
        f"Skills   : {payload['skill_count']}",
        f"Issues   : {payload['issue_count']}",
        "Severity : "
        + ", ".join(f"{key}={value}" for key, value in sorted(payload["by_severity"].items())),
        "",
        "Findings",
    ]
    for issue in payload["issues"][:limit]:
        lines.append(
            f"  [{issue['severity']}] {issue['skill_id']} {issue['code']}: {issue['message']} ({issue['path']})"
        )
    if payload["issue_count"] > limit:
        lines.append(f"  ... {payload['issue_count'] - limit} more")
    lines.extend(["", f"Next: {payload['next_step']}"])
    return "\n".join(lines)
