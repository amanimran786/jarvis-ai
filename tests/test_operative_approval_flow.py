from __future__ import annotations

import datetime as dt
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

import execution_engine
import operative
import operative_approval
import safety_permissions
import task_persistence
from task_planner import TaskStep


@pytest.fixture
def approval_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIS_TASK_DB_PATH", str(tmp_path / "tasks.sqlite3"))
    task_persistence.reset_for_tests()
    yield tmp_path
    task_persistence.reset_for_tests()


@pytest.fixture
def context() -> operative_approval.RouteContext:
    return operative_approval.RouteContext(
        principal="tester",
        session_id="session-a",
        source="pytest",
        authenticated=True,
    )


def _policy() -> dict:
    return operative._current_provider_policy()


def _budget() -> dict:
    return operative._execution_budget_contract()


def _write_step(path: Path, content: str = "hello") -> TaskStep:
    return TaskStep(
        1,
        "write exact output",
        "file",
        {"action": "write", "path": str(path), "content": content},
    )


def _persist_manifest(
    manifest: operative_approval.ExecutionManifest,
    approval_id: str = "op_test_approval_123456",
) -> str:
    assert task_persistence.create_operative_proposal(
        {
            "approval_id": approval_id,
            "manifest_digest": manifest.digest,
            "principal": manifest.principal,
            "session_id": manifest.session_id,
            "source": manifest.source,
            "created_at": manifest.created_at,
            "expires_at": manifest.expires_at,
            "manifest": manifest.to_dict(),
        }
    )
    return approval_id


def _consume_manifest(
    approval_id: str,
    manifest: operative_approval.ExecutionManifest,
    context: operative_approval.RouteContext,
    run_id: str,
) -> dict | None:
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.isoformat()
    return task_persistence.consume_operative_approval(
        approval_id,
        manifest_digest=manifest.digest,
        principal=context.principal,
        session_id=context.session_id,
        source=context.source,
        run_id=run_id,
        now_iso=now_iso,
        grant_expires_at=(now + dt.timedelta(minutes=5)).isoformat(),
        task_record={
            "id": run_id,
            "status": "running",
            "created_at": now_iso,
            "updated_at": now_iso,
            "finished_at": "",
            "task": manifest.task,
        },
    )


def test_manifest_binds_resolved_file_and_exact_call(tmp_path: Path, context):
    manifest = operative_approval.build_manifest(
        "write a file",
        [_write_step(tmp_path / "output.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )

    assert manifest.capabilities == (execution_engine.CAP_LOCAL_WRITE,)
    assert manifest.plan[0]["params"]["path"] == str((tmp_path / "output.txt").resolve())
    assert manifest.resources[0]["kind"] == "file"
    assert manifest.resources[0]["call_sha256"] == operative_approval.tool_call_sha256(
        "file", manifest.plan[0]["params"]
    )


@pytest.mark.parametrize("tool", ["terminal", "code_task", "specialized_agent"])
def test_manifest_blocks_unisolated_privileged_tools(tool: str, context):
    params = {
        "terminal": {"command": "pwd"},
        "code_task": {"task": "change code"},
        "specialized_agent": {"agent": "reviewer", "task": "delegate"},
    }[tool]

    with pytest.raises(operative_approval.ApprovalError, match="isolated execution"):
        operative_approval.build_manifest(
            "unsafe broad task",
            [TaskStep(1, "blocked", tool, params)],
            context=context,
            budget=_budget(),
            provider_policy=_policy(),
            approval_ttl_seconds=300,
        )


def test_manifest_blocks_dynamic_privileged_resource(tmp_path: Path, context):
    with pytest.raises(operative_approval.ApprovalError, match="dynamic privileged"):
        operative_approval.build_manifest(
            "write dynamic output",
            [_write_step(tmp_path / "$step_1_result.txt")],
            context=context,
            budget=_budget(),
            provider_policy=_policy(),
            approval_ttl_seconds=300,
        )


def test_prepare_side_effect_plan_persists_pending_proposal(
    approval_db: Path,
    context,
):
    step = _write_step(approval_db / "prepared.txt")
    with patch("operative.plan_task", return_value=[step]) as planner:
        result = operative.prepare_task("write prepared output", context=context)

    assert result["status"] == "approval_required"
    planner.assert_called_once_with("write prepared output")
    record = task_persistence.get_operative_proposal(result["approval_id"])
    assert record is not None
    assert record["status"] == "pending"
    assert record["manifest_digest"] == result["manifest_digest"]


def test_prepare_task_honors_preexisting_cancellation(context):
    cancelled = threading.Event()
    cancelled.set()

    with patch("operative.plan_task") as planner:
        with pytest.raises(operative_approval.ApprovalError, match="cancelled"):
            operative.prepare_task("write output", context=context, cancel_event=cancelled)

    planner.assert_not_called()


def test_prepare_task_rejects_plan_that_exceeds_timeout(context):
    step = TaskStep(1, "answer", "chat", {"prompt": "hello"})
    with patch("operative.plan_task", return_value=[step]), patch(
        "operative.time.monotonic",
        side_effect=[0.0, operative.OPERATIVE_TIMEOUT_SECONDS + 0.01],
    ):
        with pytest.raises(operative_approval.ApprovalError, match="timeout"):
            operative.prepare_task("say hello", context=context)


def test_schema_migrates_existing_approval_completion_columns(approval_db: Path):
    db = approval_db / "tasks.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE operative_approvals (
                approval_id TEXT PRIMARY KEY,
                manifest_digest TEXT NOT NULL,
                principal TEXT NOT NULL,
                session_id TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                approved_at TEXT NOT NULL DEFAULT '',
                grant_expires_at TEXT NOT NULL DEFAULT '',
                consumed_at TEXT NOT NULL DEFAULT '',
                cancelled_at TEXT NOT NULL DEFAULT '',
                run_id TEXT NOT NULL DEFAULT '',
                manifest_json TEXT NOT NULL
            )
            """
        )
    task_persistence._INITIALIZED = False

    assert task_persistence._ensure_schema() is True
    with task_persistence._connect() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(operative_approvals)")
        }

    assert {"completed_at", "outcome"} <= columns


def test_capability_free_prepared_plan_executes_without_replanning(context):
    step = TaskStep(1, "answer", "chat", {"prompt": "hello"})
    with patch("operative.plan_task", return_value=[step]) as planner:
        prepared = operative.prepare_task("say hello", context=context)
    assert prepared["status"] == "ready"

    expected = {"ok": True, "summary": "done", "steps": []}
    with patch("operative._run_task_locked", return_value=expected) as execute:
        result = operative.execute_prepared_task(prepared["manifest"], context=context)

    assert result == expected
    planner.assert_called_once()
    assert execute.call_args.kwargs["prepared_steps"][0].tool == "chat"


def test_approved_execution_consumes_once_without_replanning(
    approval_db: Path,
    context,
):
    manifest = operative_approval.build_manifest(
        "write once",
        [_write_step(approval_db / "once.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    expected = {"ok": True, "summary": "done", "steps": []}

    with patch("operative.plan_task") as planner, patch(
        "operative._run_task_locked", return_value=expected
    ) as execute:
        first = operative.approve_and_run_task(approval_id, context=context)
        second = operative.approve_and_run_task(approval_id, context=context)

    assert first == expected
    assert second["stop_reason"] in {"approval_not_pending", "approval_already_consumed"}
    planner.assert_not_called()
    execute.assert_called_once()
    record = task_persistence.get_operative_proposal(approval_id)
    assert record is not None and record["status"] == "consumed"


def test_wrong_session_cannot_approve(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write scoped",
        [_write_step(approval_db / "scoped.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    wrong = operative_approval.RouteContext("tester", "session-b", "pytest", True)

    result = operative.approve_and_run_task(approval_id, context=wrong)

    assert result["stop_reason"] == "approval_context_mismatch"
    assert task_persistence.get_operative_proposal(approval_id)["status"] == "pending"


def test_expired_proposal_fails_closed(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "expired write",
        [_write_step(approval_db / "expired.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=30,
        now=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
    )
    approval_id = _persist_manifest(manifest)

    result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "approval_expired_or_runtime_changed"


def test_manifest_mutation_invalidates_approval(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write original",
        [_write_step(approval_db / "original.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    with task_persistence._connect() as conn:
        record = task_persistence.get_operative_proposal(approval_id)
        changed = dict(record["manifest"])
        changed["task"] = "write changed"
        conn.execute(
            "UPDATE operative_approvals SET manifest_json=? WHERE approval_id=?",
            (task_persistence._json_dumps(changed), approval_id),
        )

    result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "approval_manifest_changed"


def test_cancelled_proposal_cannot_execute(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "cancel write",
        [_write_step(approval_db / "cancelled.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    assert operative.cancel_task_approval(approval_id, context=context) is True
    result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "approval_cancelled"


def test_concurrent_approval_consumers_execute_exactly_once(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "concurrent write",
        [_write_step(approval_db / "concurrent.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    with patch(
        "operative._run_task_locked",
        return_value={"ok": True, "summary": "done", "steps": []},
    ) as execute:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: operative.approve_and_run_task(approval_id, context=context),
                    range(2),
                )
            )

    assert sum(result.get("ok", False) for result in results) == 1
    execute.assert_called_once()


def test_replayed_consume_does_not_create_orphan_task(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write once atomically",
        [_write_step(approval_db / "atomic.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    assert _consume_manifest(approval_id, manifest, context, "run_first") is not None
    assert _consume_manifest(approval_id, manifest, context, "run_replay") is None
    with task_persistence._connect() as conn:
        task_ids = [row["id"] for row in conn.execute("SELECT id FROM tasks")]

    assert task_ids == ["run_first"]


def test_task_insert_failure_rolls_back_approval_consume(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write after collision",
        [_write_step(approval_db / "collision.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    assert task_persistence.upsert_task(
        {
            "id": "run_collision",
            "status": "running",
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    assert _consume_manifest(approval_id, manifest, context, "run_collision") is None
    record = task_persistence.get_operative_proposal(approval_id)

    assert record is not None
    assert record["status"] == "pending"
    assert record["run_id"] == ""


def test_task_and_approval_terminalize_in_one_transaction(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write and finish",
        [_write_step(approval_db / "finish.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    run_id = "run_terminal"
    assert _consume_manifest(approval_id, manifest, context, run_id) is not None
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    assert task_persistence.terminalize_operative_task(
        {
            "id": run_id,
            "status": "succeeded",
            "created_at": now_iso,
            "updated_at": now_iso,
            "finished_at": now_iso,
        },
        approval_id=approval_id,
        run_id=run_id,
        outcome="succeeded",
        now_iso=now_iso,
    )

    approval = task_persistence.get_operative_proposal(approval_id)
    assert approval is not None
    assert approval["outcome"] == "succeeded"
    assert task_persistence.load_snapshot()["tasks"][0]["status"] == "succeeded"


def test_terminalization_failure_rolls_back_task_update(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write and roll back",
        [_write_step(approval_db / "rollback.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)
    run_id = "run_terminal_rollback"
    assert _consume_manifest(approval_id, manifest, context, run_id) is not None
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    assert not task_persistence.terminalize_operative_task(
        {
            "id": run_id,
            "status": "succeeded",
            "created_at": now_iso,
            "updated_at": now_iso,
            "finished_at": now_iso,
        },
        approval_id="op_wrong_approval_12345",
        run_id=run_id,
        outcome="succeeded",
        now_iso=now_iso,
    )

    tasks = task_persistence.load_snapshot()["tasks"]
    task = next(item for item in tasks if item["id"] == run_id)
    assert task["status"] == "running"
    assert task_persistence.get_operative_proposal(approval_id)["outcome"] == ""


def test_approved_read_failure_requires_new_approval_for_recovery(approval_db: Path, context):
    initial = TaskStep(
        1,
        "fetch approved page",
        "fetch_page",
        {"url": "https://example.com/report", "max_chars": 1000},
    )
    manifest = operative_approval.build_manifest(
        "fetch then recover",
        [initial],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    corrective = _write_step(approval_db / "recovery.txt", "recovered")
    with patch("operative._persist_task_start", return_value=True), patch(
        "operative._checkpoint_step", return_value=True
    ), patch("operative._persist_task_finish", return_value=True), patch(
        "operative._summarize", return_value="stopped"
    ), patch("operative.preflect.is_enabled", return_value=False), patch(
        "execution_engine._execute_tool_call", return_value=(False, "write failed")
    ), patch("operative.replan_after_failure", return_value=[corrective]) as replan:
        result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "reapproval_required"
    replan.assert_called_once()
    recovery = result["reapproval"]
    assert recovery["approval_id"] != approval_id
    record = task_persistence.get_operative_proposal(recovery["approval_id"])
    assert record is not None
    assert record["status"] == "pending"
    assert record["manifest"]["plan"][0]["params"]["path"] == str(
        (approval_db / "recovery.txt").resolve()
    )


def test_approved_side_effect_failure_requires_reconciliation_not_replan(
    approval_db: Path,
    context,
):
    manifest = operative_approval.build_manifest(
        "write with uncertain outcome",
        [_write_step(approval_db / "uncertain.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    with patch("operative._persist_task_start", return_value=True), patch(
        "operative._checkpoint_step", return_value=True
    ), patch("operative._persist_task_finish", return_value=True), patch(
        "operative._summarize", return_value="stopped"
    ), patch("operative.preflect.is_enabled", return_value=False), patch(
        "execution_engine._execute_tool_call", return_value=(False, "write timed out")
    ), patch("operative.replan_after_failure") as replan:
        result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "uncertain_side_effect_outcome"
    assert result["reapproval"] == {}
    replan.assert_not_called()


def test_runtime_budget_change_invalidates_pending_approval(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write under budget",
        [_write_step(approval_db / "budget.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    with patch("operative.OPERATIVE_MAX_STEPS", operative.OPERATIVE_MAX_STEPS + 1):
        result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "approval_expired_or_runtime_changed"


def test_runtime_model_change_invalidates_pending_approval(approval_db: Path, context):
    manifest = operative_approval.build_manifest(
        "write with current model",
        [_write_step(approval_db / "model-policy.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    approval_id = _persist_manifest(manifest)

    with patch("operative.LOCAL_CODER", operative.LOCAL_CODER + "-changed"):
        result = operative.approve_and_run_task(approval_id, context=context)

    assert result["stop_reason"] == "approval_expired_or_runtime_changed"
    assert task_persistence.get_operative_proposal(approval_id)["status"] == "pending"


def test_legacy_capability_keyword_fails_closed_without_planning():
    with patch("operative.plan_task") as planner:
        result = operative.run_task(
            "write without a proposal",
            authorized_capabilities=[execution_engine.CAP_LOCAL_WRITE],
        )

    assert result["ok"] is False
    assert result["stop_reason"] == "explicit_approval_required_for_capabilities"
    planner.assert_not_called()


def test_resource_scope_rejects_parameter_mutation(tmp_path: Path, context):
    manifest = operative_approval.build_manifest(
        "write exact",
        [_write_step(tmp_path / "approved.txt", "approved")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    grant = operative_approval.ExecutionGrant(
        approval_id="op_scope_1234567890",
        manifest_digest=manifest.digest,
        principal=context.principal,
        session_id=context.session_id,
        source=context.source,
        run_id="run_scope",
        grant_expires_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
        capabilities=manifest.capabilities,
        resources_json=manifest.resources_json,
    )
    approved_params = manifest.plan[0]["params"]
    changed_params = {**approved_params, "content": "changed"}

    with safety_permissions.execution_grant_scope(grant.to_scope()):
        ok, _ = safety_permissions.authorize_tool_call(
            "file",
            approved_params,
            step_number=1,
            run_id="run_scope",
            required_capabilities={execution_engine.CAP_LOCAL_WRITE},
        )
        changed_ok, error = safety_permissions.authorize_tool_call(
            "file",
            changed_params,
            step_number=1,
            run_id="run_scope",
            required_capabilities={execution_engine.CAP_LOCAL_WRITE},
        )

    assert ok is True
    assert changed_ok is False
    assert "outside" in error


def test_resource_scope_can_be_claimed_only_once(tmp_path: Path, context):
    manifest = operative_approval.build_manifest(
        "write once",
        [_write_step(tmp_path / "single-claim.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    grant = operative_approval.ExecutionGrant(
        approval_id="op_single_claim_123456",
        manifest_digest=manifest.digest,
        principal=context.principal,
        session_id=context.session_id,
        source=context.source,
        run_id="run_single_claim",
        grant_expires_at=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat(),
        capabilities=manifest.capabilities,
        resources_json=manifest.resources_json,
    )

    with safety_permissions.execution_grant_scope(grant.to_scope()):
        first, _ = safety_permissions.authorize_tool_call(
            "file",
            manifest.plan[0]["params"],
            step_number=1,
            run_id="run_single_claim",
            required_capabilities={execution_engine.CAP_LOCAL_WRITE},
        )
        replay, error = safety_permissions.authorize_tool_call(
            "file",
            manifest.plan[0]["params"],
            step_number=1,
            run_id="run_single_claim",
            required_capabilities={execution_engine.CAP_LOCAL_WRITE},
        )

    assert first is True
    assert replay is False
    assert "already been claimed" in error


def test_expired_execution_grant_fails_before_dispatch(tmp_path: Path, context):
    manifest = operative_approval.build_manifest(
        "write expired grant",
        [_write_step(tmp_path / "grant.txt")],
        context=context,
        budget=_budget(),
        provider_policy=_policy(),
        approval_ttl_seconds=300,
    )
    grant = operative_approval.ExecutionGrant(
        approval_id="op_expired_1234567890",
        manifest_digest=manifest.digest,
        principal=context.principal,
        session_id=context.session_id,
        source=context.source,
        run_id="run_expired",
        grant_expires_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(),
        capabilities=manifest.capabilities,
        resources_json=manifest.resources_json,
    )

    with safety_permissions.execution_grant_scope(grant.to_scope()):
        ok, error = safety_permissions.authorize_tool_call(
            "file",
            manifest.plan[0]["params"],
            step_number=1,
            run_id="run_expired",
            required_capabilities={execution_engine.CAP_LOCAL_WRITE},
        )

    assert ok is False
    assert "expired" in error.lower()


def test_operative_resource_enforcement_fails_when_grant_scope_is_missing(tmp_path: Path):
    step = _write_step(tmp_path / "missing-grant.txt")
    with execution_engine.execution_capability_scope(
        {execution_engine.CAP_LOCAL_WRITE},
        require_resource_grant=True,
    ), patch("execution_engine._execute_tool_call") as dispatch:
        ok, error = execution_engine.execute_step(step, {}, run_id="run_missing_grant")

    assert ok is False
    assert "digest-bound" in error
    dispatch.assert_not_called()


def test_approval_database_is_private(approval_db: Path):
    assert task_persistence._ensure_schema() is True

    assert stat.S_IMODE(task_persistence.db_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(task_persistence.db_path().parent.stat().st_mode) == 0o700


def test_database_override_preserves_existing_parent_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)
    monkeypatch.setenv(
        "JARVIS_TASK_DB_PATH",
        str(shared_parent / "tasks.sqlite3"),
    )
    task_persistence.reset_for_tests()

    task_persistence.db_path()

    assert stat.S_IMODE(shared_parent.stat().st_mode) == 0o755


def test_bare_confirmation_and_allow_text_are_not_approval_commands():
    import router

    assert router._parse_task_approval_command("yes") is None
    assert router._parse_task_approval_command("approve") is None
    assert router._parse_task_approval_command("[allow:local_write] write it") is None
    assert router._parse_task_approval_command("/task approve op_123456789012") == (
        "approve",
        "op_123456789012",
    )


def test_classified_operative_returns_approval_without_execution(context):
    import router
    from orchestrator import ToolDecision

    decision = ToolDecision(
        tool="operative",
        confidence=0.99,
        action="run_task",
        params={"task": "write a report"},
    )
    prepared = {
        "status": "approval_required",
        "approval_id": "op_router_1234567890",
        "summary": "Approval required.",
    }
    with patch("orchestrator.classify", return_value=decision), patch(
        "operative.prepare_task", return_value=prepared
    ) as prepare, patch("operative.execute_prepared_task") as execute, patch(
        "router.audit_log"
    ):
        stream, label = router._orchestrate(
            "write a report",
            "write a report",
            context=context,
        )
        output = "".join(stream)

    assert label == "Operative"
    assert "Approval required" in output
    prepare.assert_called_once()
    assert prepare.call_args.args == ("write a report",)
    assert prepare.call_args.kwargs["context"] == context
    assert isinstance(prepare.call_args.kwargs["cancel_event"], threading.Event)
    execute.assert_not_called()


def test_explicit_approval_route_preserves_context(context):
    import router

    result = {"ok": True, "summary": "executed", "steps": []}
    with patch("operative.approve_and_run_task", return_value=result) as approve:
        stream, label = router.route_stream(
            "/task approve op_router_1234567890",
            context=context,
        )
        output = "".join(stream)

    assert label == "Operative"
    assert "executed" in output
    assert approve.call_args.kwargs["context"] == context


def test_openai_approval_routes_only_final_user_message_with_stable_context():
    import api

    approval_command = "/task approve op_abcdefghijkl"
    request = api.OAICompletionRequest(
        session_id="client-session",
        messages=[
            api.OAIMessage(role="system", content="stay local"),
            api.OAIMessage(role="user", content="prepare the task"),
            api.OAIMessage(role="assistant", content="approval required"),
            api.OAIMessage(role="user", content=approval_command),
        ],
    )

    with patch("api.route_stream", return_value=(iter(["approved"]), "Operative")) as route:
        result = api.oai_chat_completions(request)

    assert result["choices"][0]["message"]["content"] == "approved"
    assert route.call_args.args[0] == approval_command
    assert route.call_args.kwargs["context"] == api._api_route_context(
        "openai_compat", "client-session"
    )


def test_openai_nonapproval_routes_only_final_turn():
    import api

    request = api.OAICompletionRequest(
        messages=[
            api.OAIMessage(role="system", content="stay local"),
            api.OAIMessage(role="user", content="first question"),
            api.OAIMessage(role="assistant", content="first answer"),
            api.OAIMessage(role="user", content="second question"),
        ],
    )

    with patch("api.route_stream", return_value=(iter(["answer"]), "Chat")) as route:
        api.oai_chat_completions(request)

    assert route.call_args.args[0] == "second question"


def test_api_context_is_stable_and_external_source_is_not_trusted():
    import api

    with patch.object(api, "_API_TOKEN", "test-token"):
        first = api._api_route_context("openai_compat", "same-session")
        second = api._api_route_context("openai_compat", "same-session")
        different = api._api_route_context("openai_compat", "other-session")

    assert first == second
    assert first.session_id != different.session_id
    assert first.principal.startswith("api:")
    assert first.authenticated is True
    assert api._api_source("openai_compat") == "api"
    assert api._api_source("untrusted-client-value") == "api"
    assert api._api_source("mobile_web") == "api"

    with patch.object(api, "_API_TOKEN", ""):
        anonymous = api._api_route_context("api", "same-session")
    assert anonymous.authenticated is False


def test_chat_redacts_approval_ids_from_telemetry_but_not_response():
    import api

    approval_id = "op_abcdefghijkl"
    response_text = f"Approval recorded for {approval_id}."
    request = api.ChatRequest(
        message=f"/task approve {approval_id}",
        source="api",
        session_id="redaction-session",
        meta={"nested": {"approval": approval_id}},
    )

    with patch(
        "api.route_stream", return_value=(iter([response_text]), "Operative")
    ), patch("api.usage_tracker.current_seq", return_value=1), patch(
        "api.usage_tracker.summarize", return_value={}
    ), patch("api.ctx.record_request_stats", return_value={}), patch(
        "api.evals.log_interaction", return_value={"id": "interaction-1"}
    ) as log_interaction, patch("api.evals.maybe_log_automatic_failure"), patch(
        "api.semantic_memory.log_conversation_turn"
    ) as semantic_log, patch("api._record_turn") as record_turn, patch(
        "api._audit_log"
    ) as audit_log:
        result = api.chat(request)

    assert approval_id in result["response"]
    safe_message = "/task approve op_[REDACTED]"
    safe_response = "Approval recorded for op_[REDACTED]."
    assert log_interaction.call_args.args[:2] == (safe_message, safe_response)
    semantic_log.assert_called_once_with(
        safe_message,
        safe_response,
        model="Operative",
        source="api",
    )
    record_turn.assert_called_once_with(safe_message, safe_response)
    assert audit_log.call_args.kwargs["query"] == safe_message
    assert log_interaction.call_args.kwargs["context"]["client_meta"] == {
        "nested": {"approval": "op_[REDACTED]"}
    }


def test_chat_cancellation_bypasses_busy_lock_for_same_session():
    import api

    request = api.ChatRequest(
        message="/cancel",
        source="api",
        session_id="cancel-session",
    )
    api._CHAT_LOCK.acquire()
    try:
        with patch(
            "api.route_stream", return_value=(iter(["Cancelling task."]), "Operative")
        ) as route, patch("api.usage_tracker.current_seq", return_value=1), patch(
            "api.usage_tracker.summarize", return_value={}
        ), patch("api.ctx.record_request_stats", return_value={}), patch(
            "api.evals.log_interaction", return_value={"id": "interaction-1"}
        ), patch("api.evals.maybe_log_automatic_failure"), patch(
            "api.semantic_memory.log_conversation_turn"
        ), patch("api._record_turn"), patch("api._audit_log"):
            result = api.chat(request)
    finally:
        api._CHAT_LOCK.release()

    assert result["response"] == "Cancelling task."
    assert route.call_args.kwargs["context"] == api._api_route_context(
        "api", "cancel-session"
    )
