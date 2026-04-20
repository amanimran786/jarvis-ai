from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import skills


_ROOT = Path(__file__).resolve().parent
_CONNECTORS_INDEX = _ROOT / "connectors" / "index.json"
_PLUGINS_INDEX = _ROOT / "plugins" / "index.json"


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    description: str
    transport: str
    auth: str
    local_only: bool
    tools: tuple[str, ...]
    capabilities: tuple[str, ...]
    status_hint: str


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    description: str
    category: str
    local_only: bool
    skills: tuple[str, ...]
    connectors: tuple[str, ...]
    agents: tuple[str, ...]
    entrypoints: tuple[str, ...]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def all_connectors() -> tuple[Connector, ...]:
    raw = _load_json(_CONNECTORS_INDEX)
    items: list[Connector] = []
    for item in raw.get("connectors", []):
        items.append(
            Connector(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                transport=item.get("transport", "local"),
                auth=item.get("auth", "none"),
                local_only=bool(item.get("local_only", True)),
                tools=tuple(item.get("tools", [])),
                capabilities=tuple(item.get("capabilities", [])),
                status_hint=item.get("status_hint", "unknown"),
            )
        )
    return tuple(items)


@lru_cache(maxsize=1)
def all_plugins() -> tuple[Plugin, ...]:
    raw = _load_json(_PLUGINS_INDEX)
    items: list[Plugin] = []
    for item in raw.get("plugins", []):
        items.append(
            Plugin(
                id=item["id"],
                name=item["name"],
                description=item["description"],
                category=item.get("category", "general"),
                local_only=bool(item.get("local_only", True)),
                skills=tuple(item.get("skills", [])),
                connectors=tuple(item.get("connectors", [])),
                agents=tuple(item.get("agents", [])),
                entrypoints=tuple(item.get("entrypoints", [])),
            )
        )
    return tuple(items)


def get_connector(connector_id: str | None) -> Connector | None:
    if not connector_id:
        return None
    for connector in all_connectors():
        if connector.id == connector_id:
            return connector
    return None


def get_plugin(plugin_id: str | None) -> Plugin | None:
    if not plugin_id:
        return None
    for plugin in all_plugins():
        if plugin.id == plugin_id:
            return plugin
    return None


def list_skills() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for skill in skills.all_skills():
        items.append(
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "tool": skill.tool,
                "cost_hint": skill.cost_hint,
                "triggers": list(skill.triggers),
                "negative_triggers": list(skill.negative_triggers),
                "path": str(skill.path.relative_to(_ROOT)),
                "resources": [str(resource.relative_to(_ROOT)) for resource in skill.resources],
            }
        )
    return items


def get_skill_detail(skill_id: str | None) -> dict[str, Any] | None:
    skill = skills.get_skill(skill_id)
    if not skill:
        return None
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "tool": skill.tool,
        "cost_hint": skill.cost_hint,
        "triggers": list(skill.triggers),
        "negative_triggers": list(skill.negative_triggers),
        "path": str(skill.path.relative_to(_ROOT)),
        "resources": [str(resource.relative_to(_ROOT)) for resource in skill.resources],
        "instructions": skills.load_skill_bundle(skill.id),
    }


def list_connectors() -> list[dict[str, Any]]:
    return [asdict(connector) for connector in all_connectors()]


def connector_detail(connector_id: str | None) -> dict[str, Any] | None:
    connector = get_connector(connector_id)
    if not connector:
        return None
    return asdict(connector)


def list_plugins() -> list[dict[str, Any]]:
    return [asdict(plugin) for plugin in all_plugins()]


def plugin_detail(plugin_id: str | None) -> dict[str, Any] | None:
    plugin = get_plugin(plugin_id)
    if not plugin:
        return None

    resolved_skills = [get_skill_detail(skill_id) for skill_id in plugin.skills]
    resolved_connectors = [connector_detail(connector_id) for connector_id in plugin.connectors]
    return {
        **asdict(plugin),
        "skills_detail": [item for item in resolved_skills if item],
        "connectors_detail": [item for item in resolved_connectors if item],
    }


def discovery_snapshot() -> dict[str, Any]:
    return {
        "skills": {"count": len(skills.all_skills()), "items": list_skills()},
        "connectors": {"count": len(all_connectors()), "items": list_connectors()},
        "plugins": {"count": len(all_plugins()), "items": list_plugins()},
    }
