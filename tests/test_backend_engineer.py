import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Real qdrant_client imports must come FIRST — before any sys.modules patching
# that could replace qdrant_client with a MagicMock in another test module.
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

# ── Mock/Stub external dependencies before import ─────────────────────────────

# Stub config
mock_config = MagicMock()
mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
mock_config.AGENT_ROSTER = {
    "backend_engineer": {
        "role": "Backend engineer",
        "tools": ["file_read", "file_write", "run_tests"],
        "model": "glm-4.7-flash",
        "system_prompt": "Test Prompt",
    }
}
sys.modules["config"] = mock_config

# Stub qdrant_client and qdrant_client.models
mock_qdrant = MagicMock()
mock_models = MagicMock()
mock_models.Distance = Distance
mock_models.VectorParams = VectorParams
mock_models.PointStruct = PointStruct
mock_models.Filter = Filter
mock_models.FieldCondition = FieldCondition
mock_models.MatchValue = MatchValue

sys.modules["qdrant_client"] = mock_qdrant
sys.modules["qdrant_client.models"] = mock_models

# Stub brains.brain_ollama
mock_brain = MagicMock()
sys.modules["brains.brain_ollama"] = mock_brain

# Clean up sys.modules to ensure a fresh import of everything
for mod in [
    "infra.memory",
    "tools",
    "tools.fs_tools",
    "tools.shell_tools",
    "agents.backend_engineer",
]:
    if mod in sys.modules:
        del sys.modules[mod]

import pytest
from agents.backend_engineer import process_task
from agents import backend_worker
from infra.memory import store
from tools.fs_tools import WORKSPACE_DIR, read_file, write_file
from tools.shell_tools import run_tests


# ── Filesystem & Shell Tool Tests ─────────────────────────────────────────────

def test_workspace_file_operations():
    """Verify that read_file and write_file successfully manipulate files inside workspace/."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_workspace = Path(tmp_dir) / "workspace"
        tmp_workspace.mkdir()

        # Patch WORKSPACE_DIR to use our temp workspace
        with patch("tools.fs_tools.WORKSPACE_DIR", tmp_workspace):
            # Write a file
            w_res = write_file("test.txt", "Hello Workspace")
            assert "Success" in w_res
            assert (tmp_workspace / "test.txt").exists()

            # Read the file
            r_res = read_file("test.txt")
            assert r_res == "Hello Workspace"


def test_path_traversal_confinement():
    """Verify that files outside the workspace are blocked with PermissionError."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_workspace = Path(tmp_dir) / "workspace"
        tmp_workspace.mkdir()

        with patch("tools.fs_tools.WORKSPACE_DIR", tmp_workspace):
            # Attempt path traversal read
            r_res = read_file("../some_external_file.txt")
            assert "Access denied" in r_res or "outside the workspace" in r_res

            # Attempt path traversal write
            w_res = write_file("../some_external_file.txt", "exploit")
            assert "Access denied" in w_res or "outside the workspace" in w_res


def test_shell_run_tests():
    """Verify that run_tests runs pytest securely."""
    import subprocess
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_workspace = Path(tmp_dir) / "workspace"
        tmp_workspace.mkdir()

        with patch("tools.shell_tools.WORKSPACE_DIR", tmp_workspace), \
             patch("subprocess.run") as mock_sub_run:
            mock_sub_run.return_value.stdout = "pytest output: 5 passed"
            mock_sub_run.return_value.returncode = 0

            res = run_tests("-v -k test_math")

            assert res == "pytest output: 5 passed"
            mock_sub_run.assert_called_once_with(
                ["pytest", "-v", "-k", "test_math"],
                cwd=str(tmp_workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
            )


# ── Worker DAG Task Processing Tests ──────────────────────────────────────────

def test_backend_engineer_worker_flow():
    """Verify backend_engineer worker processes tasks, recalls memory, and posts results."""
    payload = {
        "task_id": "task-uuid-456",
        "title": "Build FastAPI user endpoint",
        "description": "Create a POST /users router.",
        "context": {"session_id": "sess-999", "project_id": "proj-888"},
    }

    # Mock memory recall
    with patch.object(store, "context_for_prompt", return_value="FastAPI user guidelines") as mock_recall, \
         patch("agents.backend_engineer.ask_local_with_tools") as mock_llm_call, \
         patch("httpx.post") as mock_http_post:

        mock_llm_call.return_value = iter(["Endpoint logic", " completed successfully."])
        mock_http_post.return_value.status_code = 200

        output = process_task(payload)

        # Asserts
        assert output == "Endpoint logic completed successfully."

        # Verify memory recall was called with appropriate query
        mock_recall.assert_called_once_with(
            query="Build FastAPI user endpoint Create a POST /users router.",
            top_k=3,
            session_id="sess-999",
            project_id="proj-888",
            max_chars=1500,
        )

        # Verify LLM loop was called with system prompt and tools list
        mock_llm_call.assert_called_once_with(
            user_input=(
                "Task Description:\nCreate a POST /users router.\n\n"
                "Relevant Memory Context:\nFastAPI user guidelines\n\n"
                "Task Context Metadata:\n{\n  \"session_id\": \"sess-999\",\n  \"project_id\": \"proj-888\"\n}\n\n"
                "Please resolve the task inside the workspace directory using your tools."
            ),
            model="glm-4.7-flash",
            system_extra="Test Prompt",
            tools=["read_file", "write_file", "run_tests"],
        )

        # Verify HTTP post back to Event Bus results
        mock_http_post.assert_called_once()
        called_url = mock_http_post.call_args[0][0]
        called_json = mock_http_post.call_args[1]["json"]
        assert "/results" in called_url
        assert called_json["task_id"] == "task-uuid-456"
        assert called_json["output"] == "Endpoint logic completed successfully."


def test_backend_worker_run_once_processes_inbox_task():
    event = (
        'data: {"type":"task","task_id":"task-1","task":'
        '{"title":"Build API","description":"Create route","context":{"project_id":"p1"}}}\n\n'
    )
    response = MagicMock()
    response.text = event
    response.raise_for_status.return_value = None

    with patch("httpx.get", return_value=response) as mock_get, \
         patch("agents.backend_worker.process_task", return_value="done") as mock_process:
        result = backend_worker.run_once(timeout_ms=100)

    assert result["ok"] is True
    assert result["status"] == "processed"
    assert result["task_id"] == "task-1"
    mock_get.assert_called_once()
    mock_process.assert_called_once_with({
        "task_id": "task-1",
        "title": "Build API",
        "description": "Create route",
        "context": {"project_id": "p1"},
    })


def test_backend_worker_run_once_handles_heartbeat():
    response = MagicMock()
    response.text = 'data: {"type":"heartbeat"}\n\n'
    response.raise_for_status.return_value = None

    with patch("httpx.get", return_value=response), \
         patch("agents.backend_worker.process_task") as mock_process:
        result = backend_worker.run_once(timeout_ms=100)

    assert result == {"ok": True, "status": "idle", "agent": "backend_engineer"}
    mock_process.assert_not_called()
