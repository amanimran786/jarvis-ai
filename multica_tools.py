"""Multica issue tracker integration for Jarvis.

Jarvis can create, list, and assign issues on the local Multica board
so agent coordination stays in one place instead of CODEX_HANDOFF.md.

Config is read from ~/Library/Application Support/Jarvis/.multica.json.
Falls back to environment variables MULTICA_TOKEN and MULTICA_WORKSPACE_ID.
"""
import json
import os
import pathlib
import urllib.request
import urllib.error

_CONFIG_PATH = pathlib.Path.home() / "Library" / "Application Support" / "Jarvis" / ".multica.json"
_SERVER = "http://localhost:8080"
_WORKSPACE_ID = "d5626b14-1fa6-41d9-b05f-7a18ba0d883e"
_PAT = "mul_66b10b98e967134b9e9be3e2e8a97f4d7555f740"

# Agent IDs on the board
CLAUDE_AGENT_ID = "493a37df-e3d1-40dc-8b13-7502be370b8d"
CODEX_AGENT_ID  = "78840da1-8fb4-42f5-a7c7-3b425739d8ca"


def _cfg() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _token() -> str:
    return _cfg().get("token") or os.environ.get("MULTICA_TOKEN") or _PAT


def _ws() -> str:
    return _cfg().get("workspace_id") or os.environ.get("MULTICA_WORKSPACE_ID") or _WORKSPACE_ID


def _req(method: str, path: str, body: dict | None = None) -> dict | list:
    url = f"{_SERVER}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_token()}",
            "X-Workspace-ID": _ws(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


def create_issue(
    title: str,
    description: str = "",
    priority: str = "medium",
    assignee_id: str | None = None,
    status: str = "todo",
) -> dict:
    """Create a new issue on the Jarvis AI board."""
    body: dict = {"title": title, "priority": priority, "status": status}
    if description:
        body["description"] = description
    if assignee_id:
        body["assignee_id"] = assignee_id
    return _req("POST", "/api/issues", body)


def list_issues(status: str | None = None, limit: int = 20) -> list:
    """List issues, optionally filtered by status."""
    path = f"/api/issues?limit={limit}"
    if status:
        path += f"&status={status}"
    result = _req("GET", path)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("issues") or result.get("data") or []
    return []


def get_issue(issue_id: str) -> dict:
    return _req("GET", f"/api/issues/{issue_id}")


def update_issue(issue_id: str, **kwargs) -> dict:
    return _req("PUT", f"/api/issues/{issue_id}", kwargs)


def assign_issue(issue_id: str, agent_id: str) -> dict:
    return _req("PUT", f"/api/issues/{issue_id}", {"assignee_id": agent_id})


def status_summary() -> str:
    """Return a one-line board summary for the briefing."""
    try:
        issues = list_issues()
        in_prog = [i for i in issues if i.get("status") == "in_progress"]
        todo    = [i for i in issues if i.get("status") == "todo"]
        done    = [i for i in issues if i.get("status") == "done"]
        parts = []
        if in_prog:
            parts.append(f"{len(in_prog)} in progress")
        if todo:
            parts.append(f"{len(todo)} queued")
        if done:
            parts.append(f"{len(done)} done")
        return "Board: " + ", ".join(parts) if parts else "Board: empty"
    except Exception:
        return ""
