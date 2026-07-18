from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import execution_engine
import operative
import operative_approval
import safety_permissions
import task_persistence
import tool_registry
from task_planner import TaskStep


def _step(number: int, tool: str, **params) -> TaskStep:
    return TaskStep(number=number, description=f"{tool} step", tool=tool, params=params)


@contextmanager
def _authorized_scope(
    run_id: str,
    capabilities: set[str],
    calls: list[tuple[TaskStep, dict]],
    *,
    deadline: float | None = None,
    sensitive_step_numbers: set[int] | None = None,
    unavailable_step_numbers: set[int] | None = None,
):
    resources = []
    for step, resolved in calls:
        ok, normalized, error = tool_registry.validate_args(step.tool, resolved)
        assert ok, error
        if step.tool == "file":
            normalized["path"] = str(Path(normalized["path"]).expanduser().resolve(strict=False))
        resources.append({
            "step_number": step.number,
            "tool": step.tool,
            "call_sha256": operative_approval.tool_call_sha256(step.tool, normalized),
        })
    grant = {
        "run_id": run_id,
        "grant_expires_at": (
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
        ).isoformat(),
        "capabilities": sorted(capabilities),
        "resources": resources,
    }
    with execution_engine.execution_capability_scope(
        capabilities,
        deadline=deadline,
        sensitive_step_numbers=sensitive_step_numbers or set(),
        unavailable_step_numbers=unavailable_step_numbers or set(),
    ), safety_permissions.execution_grant_scope(grant):
        yield


def _task_payload(run_id: str, plan: list[dict], **overrides) -> dict:
    payload = {
        "id": run_id,
        "status": "running",
        "task": "Resume the task.",
        "created_at": "2026-07-16T00:00:00+00:00",
        "updated_at": "2026-07-16T00:00:00+00:00",
        "finished_at": "",
        "steps_total": len(plan),
        "steps_done": 0,
        "plan": plan,
        "authorized_capabilities": [],
        "execution_budget": {
            "executed_steps": 0,
            "recovery_attempts": 0,
            "elapsed_seconds": 0.0,
            "sensitive_step_numbers": [],
        },
        "result": "",
    }
    payload.update(overrides)
    return payload


class TestTrustedCapabilityBoundary:
    def test_task_text_never_grants_capabilities(self):
        assert execution_engine.capabilities_for_task(
            "[allow:local_write,shell_execute] Write a file and run tests."
        ) == frozenset()
        assert execution_engine.capabilities_for_task(
            'Summarize this quote: "write a file and run tests".'
        ) == frozenset()

    def test_trusted_grant_is_normalized_and_allowlisted(self):
        capabilities = execution_engine.capabilities_for_task(
            "untrusted text",
            trusted_capabilities={"LOCAL-WRITE", "git_write", "made_up"},
        )
        assert capabilities == frozenset({
            execution_engine.CAP_LOCAL_WRITE,
            execution_engine.CAP_GIT_WRITE,
        })

    def test_allow_directive_is_plain_task_text(self):
        with patch("operative.plan_task", return_value=[]) as planner, \
             patch("operative._persist_task_start", return_value=True), \
             patch("operative._persist_task_finish", return_value=True), \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False):
            result = operative.run_task("[allow:local_write] Write report.md")

        planner.assert_called_once_with("[allow:local_write] Write report.md")
        assert result["authorized_capabilities"] == []

    def test_file_write_fails_closed_without_capability(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch:
            ok, result = execution_engine.execute_step(
                _step(1, "file", action="write", path="out.txt", content="data"),
                {},
                run_id="run_denied",
            )

        assert ok is False
        assert "authorization" in result.lower()
        dispatch.assert_not_called()

    def test_authorized_file_write_reaches_dispatch(self):
        step = _step(1, "file", action="write", path="out.txt", content="data")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", return_value=(True, "written")) as dispatch, \
             _authorized_scope(
                 "run_allowed",
                 {execution_engine.CAP_LOCAL_WRITE},
                 [(step, step.params)],
             ):
            ok, result = execution_engine.execute_step(step, {}, run_id="run_allowed")

        assert ok is True
        assert result == "written"
        dispatch.assert_called_once()

    def test_file_read_requires_local_read_capability(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch:
            ok, result = execution_engine.execute_step(
                _step(1, "file", action="read", path=".env"), {}, run_id="read_denied"
            )

        assert ok is False
        assert execution_engine.CAP_LOCAL_READ in result
        dispatch.assert_not_called()

    def test_git_status_needs_no_write_capability(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", return_value=(True, "clean")) as dispatch:
            ok, result = execution_engine.execute_step(
                _step(1, "git", action="status"), {}, run_id="run_read"
            )

        assert ok is True
        assert result == "clean"
        dispatch.assert_called_once()

    def test_generic_shell_requires_unrestricted_shell_grant(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch, \
             execution_engine.execution_capability_scope({execution_engine.CAP_SHELL_EXECUTE}):
            ok, result = execution_engine.execute_step(
                _step(1, "terminal", command="cat .env"), {}, run_id="shell_denied"
            )

        assert ok is False
        assert execution_engine.CAP_UNRESTRICTED_SHELL in result
        dispatch.assert_not_called()

    def test_generic_terminal_stays_disabled_even_with_broad_grant(self):
        ok, result = execution_engine._execute_tool_call(
            "terminal", {"command": "false"}, _step(1, "terminal"), {}
        )
        assert ok is False
        assert "disabled" in result.lower()

    def test_specialist_delegation_stays_disabled_until_child_scope_propagates(self):
        ok, result = execution_engine._execute_tool_call(
            "specialized_agent",
            {"agent": "backend_engineer", "task": "fix tests"},
            _step(1, "specialized_agent"),
            {},
        )
        assert ok is False
        assert "disabled" in result.lower()

    def test_delegation_requires_parent_read_write_network_and_shell_grants(self):
        required = execution_engine.required_capabilities_for_tool(
            "specialized_agent", {"agent": "coder", "task": "fix tests"}
        )
        assert {
            execution_engine.CAP_AGENT_DELEGATE,
            execution_engine.CAP_LOCAL_READ,
            execution_engine.CAP_LOCAL_WRITE,
            execution_engine.CAP_NETWORK_ACCESS,
            execution_engine.CAP_SHELL_EXECUTE,
            execution_engine.CAP_UNRESTRICTED_SHELL,
        } <= required

    def test_git_validation_text_cannot_be_reported_as_success(self):
        step = _step(1, "git", action="add", paths="")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", return_value=(True, "No paths provided.")), \
             _authorized_scope(
                 "git_false_success",
                 {execution_engine.CAP_GIT_WRITE},
                 [(step, step.params)],
             ):
            ok, result = execution_engine.execute_step(step, {}, run_id="git_false_success")

        assert ok is False
        assert "no paths provided" in result.lower()


class TestToolArgumentBounds:
    def test_unknown_argument_is_rejected(self):
        ok, normalized, error = tool_registry.validate_args(
            "search", {"query": "jarvis", "unexpected": "value"}
        )
        assert ok is False
        assert normalized == {}
        assert "unknown argument" in error.lower()

    def test_code_iterations_above_limit_are_rejected(self):
        ok, normalized, error = tool_registry.validate_args(
            "code_task", {"task": "fix tests", "max_iterations": 1_000_000}
        )
        assert ok is False
        assert normalized == {}
        assert "at most" in error.lower()

    def test_numeric_lower_bound_is_enforced(self):
        ok, normalized, error = tool_registry.validate_args(
            "search", {"query": "jarvis", "max_results": 0}
        )
        assert ok is False
        assert normalized == {}
        assert "at least" in error.lower()

    def test_chat_accepts_content_without_inserting_blank_prompt(self):
        ok, normalized, error = tool_registry.validate_args("chat", {"content": "hello"})
        assert ok is True
        assert error == ""
        assert normalized == {"content": "hello"}

    def test_supported_tool_aliases_survive_strict_validation(self):
        for tool, params in (
            ("terminal", {"cmd": "pwd"}),
            ("research", {"topic": "jarvis"}),
            ("notes", {"action": "write", "text": "remember this"}),
        ):
            ok, _, error = tool_registry.validate_args(tool, params)
            assert ok is True, error

    def test_chat_dispatch_uses_content_then_description(self):
        with patch.object(execution_engine, "DEFAULT_MODE", "cloud"), \
             patch("execution_engine.skills.build_system_extra", return_value=("", None)), \
             patch("execution_engine.ask_with_priority", return_value="answer") as ask:
            ok, _ = execution_engine._execute_tool_call(
                "chat", {"prompt": "   ", "content": "from content"}, _step(1, "chat"), {}
            )
            assert ok is True
            assert ask.call_args.args[0] == "from content"
            execution_engine._execute_tool_call("chat", {}, _step(2, "chat"), {})
            assert ask.call_args.args[0] == "chat step"


class TestDataFlowAndNetworkSafety:
    def test_sensitive_read_cannot_flow_to_search(self):
        capabilities = {execution_engine.CAP_LOCAL_READ, execution_engine.CAP_NETWORK_ACCESS}
        read_step = _step(1, "file", action="read", path=".env")
        search_step = _step(2, "search", query="$step_1_result")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", return_value=(True, "sensitive-value")) as dispatch, \
             _authorized_scope(
                 "taint",
                 capabilities,
                 [(read_step, read_step.params), (search_step, {"query": "sensitive-value"})],
             ):
            ok, value = execution_engine.execute_step(read_step, {}, run_id="taint")
            assert ok is True
            ok, result = execution_engine.execute_step(
                search_step, {1: value}, run_id="taint"
            )

        assert ok is False
        assert "sensitive local data" in result.lower()
        assert dispatch.call_count == 1

    def test_taint_propagates_through_local_chat_transform(self):
        capabilities = {execution_engine.CAP_LOCAL_READ, execution_engine.CAP_NETWORK_ACCESS}
        read_step = _step(1, "file", action="read", path=".env")
        chat_step = _step(2, "chat", prompt="$step_1_result")
        search_step = _step(3, "search", query="$step_2_result")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", side_effect=[
                 (True, "sensitive-value"), (True, "transformed value")
             ]) as dispatch, \
             _authorized_scope(
                 "transitive",
                 capabilities,
                 [
                     (read_step, read_step.params),
                     (search_step, {"query": "transformed value"}),
                 ],
             ):
            ok, first = execution_engine.execute_step(read_step, {}, run_id="transitive")
            assert ok is True
            ok, second = execution_engine.execute_step(
                chat_step, {1: first}, run_id="transitive"
            )
            assert ok is True
            ok, result = execution_engine.execute_step(
                search_step,
                {1: first, 2: second},
                run_id="transitive",
            )

        assert ok is False
        assert "sensitive local data" in result.lower()
        assert dispatch.call_count == 2

    def test_malformed_placeholder_is_not_partially_resolved(self):
        assert execution_engine.resolve_params(
            {"query": "$step_1_result suffix"}, {1: "secret"}
        ) == {"query": "$step_1_result suffix"}

    def test_resumed_redacted_result_cannot_be_written_as_real_data(self):
        step = _step(2, "file", action="write", path="out.txt", content="$step_1_result")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch, \
             _authorized_scope(
                 "resume_redacted",
                 {execution_engine.CAP_LOCAL_WRITE},
                 [(step, {**step.params, "content": "[SENSITIVE RESULT REDACTED]"})],
                 sensitive_step_numbers={1},
                 unavailable_step_numbers={1},
             ):
            ok, result = execution_engine.execute_step(
                step,
                {1: "[SENSITIVE RESULT REDACTED]"},
                run_id="resume_redacted",
            )

        assert ok is False
        assert "unavailable after resume" in result.lower()
        dispatch.assert_not_called()

    def test_resumed_redacted_result_cannot_be_implicitly_written_to_notes(self):
        step = _step(2, "notes", action="write", title="Recovered")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch, \
             _authorized_scope(
                 "resume_notes",
                 {execution_engine.CAP_LOCAL_WRITE},
                 [(step, step.params)],
                 sensitive_step_numbers={1},
                 unavailable_step_numbers={1},
             ):
            ok, result = execution_engine.execute_step(
                step,
                {1: "[SENSITIVE RESULT REDACTED]"},
                run_id="resume_notes",
            )

        assert ok is False
        assert "unavailable after resume" in result.lower()
        dispatch.assert_not_called()

    def test_localhost_fetch_is_blocked_before_dispatch(self):
        step = _step(1, "fetch_page", url="http://127.0.0.1:8000/private")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call") as dispatch, \
             _authorized_scope(
                 "ssrf",
                 {execution_engine.CAP_NETWORK_ACCESS},
                 [(step, step.params)],
             ):
            ok, result = execution_engine.execute_step(step, {}, run_id="ssrf")

        assert ok is False
        assert "blocks" in result.lower()
        dispatch.assert_not_called()

    def test_redirect_to_localhost_is_blocked(self):
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("execution_engine.socket.getaddrinfo", return_value=public_dns), \
             patch(
                 "execution_engine._pinned_http_request",
                 return_value=(302, {"location": "http://127.0.0.1/x"}, b""),
             ):
            ok, result = execution_engine._fetch_public_page("https://example.com", 1000)

        assert ok is False
        # execution_engine now returns "cross-origin redirect is outside approval scope"
        assert "redirect" in result.lower() or "blocks" in result.lower()

    def test_cross_origin_redirect_is_outside_approved_scope(self):
        public_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with patch("execution_engine.socket.getaddrinfo", return_value=public_dns), patch(
            "execution_engine._pinned_http_request",
            return_value=(302, {"location": "https://other.example/x"}, b""),
        ):
            ok, result = execution_engine._fetch_public_page(
                "https://example.com/start", 1000
            )

        assert ok is False
        assert "cross-origin redirect" in result.lower()

    def test_approved_file_path_rejects_symlink_parent(self, tmp_path: Path):
        real_parent = tmp_path / "real"
        real_parent.mkdir()
        linked_parent = tmp_path / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)

        ok, result = execution_engine._approved_file_call(
            str(linked_parent / "output.txt"),
            "write",
            "content",
        )

        assert ok is False
        assert "approved file operation failed" in result.lower()
        assert not (real_parent / "output.txt").exists()

    def test_invalid_url_port_fails_closed(self):
        error = execution_engine._validate_public_http_url("http://example.com:99999")
        assert "invalid port" in error.lower()

    def test_search_fetch_top_is_disabled(self):
        ok, result = execution_engine._execute_tool_call(
            "search",
            {"query": "jarvis", "max_results": 5, "fetch_top": True},
            _step(1, "search"),
            {},
        )
        assert ok is False
        assert "disabled" in result.lower()

    def test_side_effecting_tool_is_never_retried(self):
        step = _step(
            1,
            "notes",
            action="write",
            title="Approved note",
            content="one write",
        )
        with execution_engine.execution_capability_scope(
            {execution_engine.CAP_LOCAL_WRITE}
        ), patch(
            "execution_engine._execute_tool_call",
            return_value=(False, "write timed out"),
        ) as dispatch:
            ok, _ = execution_engine.execute_step(step, {}, run_id="single-attempt")

        assert ok is False
        dispatch.assert_called_once()

    def test_approved_local_provider_failure_cannot_fall_back_to_cloud(self):
        policy = {
            "mode": "open-source",
            "models": {"local_default": "approved-local-model"},
        }
        client = type("Client", (), {})()
        client.chat = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("offline"))
        with execution_engine.execution_capability_scope(
            set(), provider_policy=policy
        ), patch("ollama.Client", return_value=client), patch(
            "execution_engine.ask_with_priority"
        ) as cloud:
            ok, result = execution_engine.execute_step(
                _step(1, "chat", prompt="local only"),
                {},
                run_id="provider-policy",
            )

        assert ok is False
        assert "cloud fallback is blocked" in result.lower()
        cloud.assert_not_called()

    def test_deep_research_is_disabled_until_fetches_are_ssrf_safe(self):
        ok, result = execution_engine._execute_tool_call(
            "research", {"query": "jarvis", "depth": 2}, _step(1, "research"), {}
        )
        assert ok is False
        assert "disabled" in result.lower()

    def test_trace_redacts_resolved_parameters_and_results(self):
        step = _step(1, "file", action="read", path=".env")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine._execute_tool_call", return_value=(True, "sensitive-value")), \
             _authorized_scope(
                 "trace",
                 {execution_engine.CAP_LOCAL_READ},
                 [(step, step.params)],
             ):
            execution_engine.execute_step(step, {}, run_id="trace")
            trace_text = next(Path(tmp).glob("*.json")).read_text()

        assert "sensitive-value" not in trace_text
        assert '"path": ".env"' not in trace_text
        assert '"description": "file step"' not in trace_text
        assert "[REDACTED]" in trace_text


class TestDeadlineAndContextIsolation:
    def test_tool_needs_its_declared_timeout_remaining(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine.time.monotonic", return_value=80.0), \
             patch("execution_engine._execute_tool_call") as dispatch, \
             execution_engine.execution_capability_scope(set(), deadline=100.0):
            ok, result = execution_engine.execute_step(_step(1, "chat"), {}, run_id="deadline")

        assert ok is False
        assert "insufficient execution budget" in result.lower()
        dispatch.assert_not_called()

    def test_retry_rechecks_remaining_budget(self):
        step = _step(1, "search", query="jarvis")
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp)), \
             patch("execution_engine.time.monotonic", side_effect=[0.0, 80.0]), \
             patch("execution_engine._execute_tool_call", return_value=(False, "offline")) as dispatch, \
             _authorized_scope(
                 "retry_deadline",
                 {execution_engine.CAP_NETWORK_ACCESS},
                 [(step, step.params)],
                 deadline=100.0,
             ):
            ok, result = execution_engine.execute_step(step, {}, run_id="retry_deadline")

        assert ok is False
        assert "retry blocked" in result.lower()
        dispatch.assert_called_once()

    def test_contextvars_are_isolated_across_threads(self):
        barrier = threading.Barrier(2)
        observed: list[frozenset[str]] = []

        def worker(capability: str) -> None:
            with execution_engine.execution_capability_scope({capability}):
                barrier.wait(timeout=2)
                observed.append(execution_engine._ACTIVE_CAPABILITIES.get())

        threads = [
            threading.Thread(target=worker, args=(execution_engine.CAP_LOCAL_READ,)),
            threading.Thread(target=worker, args=(execution_engine.CAP_NETWORK_ACCESS,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        assert set(observed) == {
            frozenset({execution_engine.CAP_LOCAL_READ}),
            frozenset({execution_engine.CAP_NETWORK_ACCESS}),
        }
        assert execution_engine._ACTIVE_CAPABILITIES.get() == frozenset()

    def test_contextvars_reset_after_exception(self):
        try:
            with execution_engine.execution_capability_scope({execution_engine.CAP_LOCAL_WRITE}):
                raise RuntimeError("stop")
        except RuntimeError:
            pass
        assert execution_engine._ACTIVE_CAPABILITIES.get() == frozenset()


class TestOperativeBudgetsAndPersistence:
    def _patch_runtime(self):
        return (
            patch("operative._persist_task_start", return_value=True),
            patch("operative._checkpoint_step", return_value=True),
            patch("operative._persist_task_finish", return_value=True),
            patch("operative._summarize", return_value="done"),
            patch("operative.preflect.is_enabled", return_value=False),
        )

    def test_recovery_cannot_expand_original_capabilities(self):
        initial = _step(1, "search", query="public docs")
        corrective = _step(2, "terminal", command="touch unauthorized.txt")
        runtime = self._patch_runtime()
        with runtime[0], runtime[1], runtime[2], runtime[3], runtime[4], \
             patch("operative.plan_task", return_value=[initial]), \
             patch("operative.replan_after_failure", return_value=[corrective]), \
             patch("operative.OPERATIVE_MAX_RECOVERY_ATTEMPTS", 1), \
             patch("execution_engine._execute_tool_call", return_value=(False, "offline")) as dispatch:
            result = operative.run_task("Research public docs.")

        assert len(result["steps"]) == 2
        assert "authorization" in result["steps"][1].result.lower()
        dispatch.assert_not_called()

    def test_execution_stops_when_initial_persistence_fails(self):
        with patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative._persist_task_start", return_value=False), \
             patch("operative._persist_task_finish", return_value=True), \
             patch("operative._summarize", return_value="blocked"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.execute_step") as execute:
            result = operative.run_task("Summarize this text.")

        execute.assert_not_called()
        assert result["ok"] is False
        assert result["stop_reason"] == "task_persistence_failed"

    def test_recovery_and_total_step_budgets_stop_growth(self):
        def corrective(*_args, **_kwargs):
            return [_step(1, "chat"), _step(2, "chat"), _step(3, "chat")]

        runtime = self._patch_runtime()
        with runtime[0], runtime[1], runtime[2], runtime[3], runtime[4], \
             patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative.execute_step", return_value=(False, "failed")) as execute, \
             patch("operative.replan_after_failure", side_effect=corrective) as replan, \
             patch("operative.OPERATIVE_MAX_STEPS", 5), \
             patch("operative.OPERATIVE_MAX_RECOVERY_ATTEMPTS", 2):
            result = operative.run_task("Summarize this text.")

        assert execute.call_count == 5
        assert replan.call_count == 2
        assert len(result["steps"]) == 5
        assert result["stop_reason"] == "step_limit"

    def test_deadline_starts_before_planning(self):
        runtime = self._patch_runtime()
        with runtime[0], runtime[1], runtime[2], runtime[3], runtime[4], \
             patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative.execute_step") as execute, \
             patch("operative.OPERATIVE_TIMEOUT_SECONDS", 10), \
             patch("operative.time.monotonic", side_effect=[0.0, 11.0, 11.0, 11.0, 11.0]):
            result = operative.run_task("Summarize this text.")

        execute.assert_not_called()
        assert result["stop_reason"] == "time_limit"

    def test_tool_returning_after_deadline_cannot_report_run_success(self):
        clock = iter([0.0, 0.0, 0.0, 0.0, 0.0, 11.0, 11.0, 11.0, 11.0])
        runtime = self._patch_runtime()
        with runtime[0], runtime[1], runtime[2], runtime[3], runtime[4], \
             patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative.execute_step", return_value=(True, "late answer")), \
             patch("operative.OPERATIVE_TIMEOUT_SECONDS", 10), \
             patch("operative.time.monotonic", side_effect=lambda: next(clock)):
            result = operative.run_task("Summarize this text.")

        assert result["ok"] is False
        assert result["stop_reason"] == "time_limit"

    def test_recovery_counter_is_persisted_before_replan(self):
        events: list[str] = []

        def persist(*_args, **kwargs):
            if kwargs["budget"]["recovery_attempts"] == 1:
                events.append("persisted")
            return True

        def replan(*_args, **_kwargs):
            events.append("replan")
            return None

        with patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative.execute_step", return_value=(False, "failed")), \
             patch("operative._persist_task_start", side_effect=persist), \
             patch("operative._checkpoint_step", return_value=True), \
             patch("operative._persist_task_finish", return_value=True), \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.replan_after_failure", side_effect=replan):
            operative.run_task("Summarize this text.")

        assert events.index("persisted") < events.index("replan")

    def test_failed_step_checkpoint_keeps_uncertain_in_flight_state(self):
        with patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative._persist_task_start", return_value=True), \
             patch("operative._checkpoint_step", return_value=False), \
             patch("operative._persist_task_finish", return_value=True) as finish, \
             patch("operative._summarize", return_value="done"), \
             patch("operative.preflect.is_enabled", return_value=False), \
             patch("operative.execute_step", return_value=(True, "answer")):
            result = operative.run_task("Summarize this text.")

        assert result["ok"] is False
        assert result["stop_reason"] == "step_checkpoint_failed"
        finish.assert_not_called()

    def test_final_persistence_failure_cannot_report_success(self):
        runtime = self._patch_runtime()
        with runtime[0], runtime[1], \
             patch("operative._persist_task_finish", return_value=False), runtime[3], runtime[4], \
             patch("operative.plan_task", return_value=[_step(1, "chat")]), \
             patch("operative.execute_step", return_value=(True, "answer")):
            result = operative.run_task("Summarize this text.")

        assert result["ok"] is False
        assert result["stop_reason"] == "final_persistence_failed"

    def test_resume_rejects_uncertain_in_flight_step(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"JARVIS_TASK_DB_PATH": str(Path(tmp) / "tasks.sqlite3")}):
            task_persistence.reset_for_tests()
            task_persistence.upsert_task(_task_payload(
                "run_uncertain",
                [{"number": 1, "description": "write", "tool": "file", "params": {
                    "action": "write", "path": "out.txt", "content": "data"
                }}],
                execution_budget={
                    "executed_steps": 0,
                    "recovery_attempts": 0,
                    "elapsed_seconds": 1.0,
                    "sensitive_step_numbers": [],
                    "in_flight_step": {"number": 1, "tool": "file"},
                },
            ))
            with patch("operative.execute_step") as execute:
                result = operative.resume_task("run_uncertain")
            task_persistence.reset_for_tests()

        execute.assert_not_called()
        assert result["stop_reason"] == "uncertain_in_flight_step"

    def test_resume_never_replays_a_failed_attempt(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"JARVIS_TASK_DB_PATH": str(Path(tmp) / "tasks.sqlite3")}):
            task_persistence.reset_for_tests()
            task_persistence.upsert_task(_task_payload(
                "run_failed_attempt",
                [{"number": 1, "description": "commit", "tool": "git", "params": {
                    "action": "commit", "message": "test"
                }}],
                authorized_capabilities=[execution_engine.CAP_GIT_WRITE],
                execution_budget={
                    "executed_steps": 1, "recovery_attempts": 0,
                    "elapsed_seconds": 1.0, "sensitive_step_numbers": [],
                },
            ))
            task_persistence.checkpoint_step(
                "run_failed_attempt", 1, "commit", "git", False, "commit status uncertain"
            )
            with patch("operative._persist_task_start", return_value=True), \
                 patch("operative._persist_task_finish", return_value=True), \
                 patch("operative._summarize", return_value="failed"), \
                 patch("operative.execute_step") as execute:
                result = operative.resume_task("run_failed_attempt")
            task_persistence.reset_for_tests()

        execute.assert_not_called()
        assert result["ok"] is False

    def test_resumed_taint_blocks_outbound_step(self):
        plan = [
            {"number": 1, "description": "read", "tool": "file", "params": {
                "action": "read", "path": ".env"
            }},
            {"number": 2, "description": "search", "tool": "search", "params": {
                "query": "$step_1_result"
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"JARVIS_TASK_DB_PATH": str(Path(tmp) / "tasks.sqlite3")}), \
             patch.object(execution_engine, "TRACE_DIR", Path(tmp) / "traces"):
            task_persistence.reset_for_tests()
            task_persistence.upsert_task(_task_payload(
                "run_tainted", plan,
                authorized_capabilities=[
                    execution_engine.CAP_LOCAL_READ, execution_engine.CAP_NETWORK_ACCESS
                ],
                execution_budget={
                    "executed_steps": 1, "recovery_attempts": 0,
                    "elapsed_seconds": 1.0, "sensitive_step_numbers": [1],
                },
            ))
            task_persistence.checkpoint_step(
                "run_tainted", 1, "read", "file", True, "[SENSITIVE RESULT REDACTED]"
            )
            with patch("operative._persist_task_start", return_value=True), \
                 patch("operative._checkpoint_step", return_value=True), \
                 patch("operative._persist_task_finish", return_value=True), \
                 patch("operative._summarize", return_value="blocked"), \
                 patch("operative.replan_after_failure", return_value=None), \
                 patch("execution_engine._execute_tool_call") as dispatch:
                result = operative.resume_task("run_tainted")
            task_persistence.reset_for_tests()

        dispatch.assert_not_called()
        assert result["stop_reason"] == "resume_approval_missing"

    def test_completed_record_retains_budget_without_forged_grant(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"JARVIS_TASK_DB_PATH": str(Path(tmp) / "tasks.sqlite3")}):
            task_persistence.reset_for_tests()
            with patch("operative.plan_task", return_value=[_step(1, "chat", prompt="hello")]), \
                 patch("operative.execute_step", return_value=(True, "written")), \
                 patch("operative._summarize", return_value="done"), \
                 patch("operative.preflect.is_enabled", return_value=False):
                result = operative.run_task("Say hello")
            snapshot = task_persistence.load_snapshot()
            task_persistence.reset_for_tests()

        assert result["ok"] is True
        record = snapshot["tasks"][0]
        assert record["authorized_capabilities"] == []
        assert record["execution_budget"]["executed_steps"] == 1
        assert "elapsed_seconds" in record["execution_budget"]

    def test_concurrent_resume_of_same_run_is_rejected(self):
        with operative._RESUME_LOCKS_GUARD:
            lock = operative._RESUME_LOCKS.setdefault("run_busy", threading.Lock())
        lock.acquire()
        try:
            result = operative.resume_task("run_busy")
        finally:
            lock.release()

        assert result["stop_reason"] == "resume_already_in_progress"

    def test_process_run_lock_excludes_a_second_worker(self):
        first = operative._try_acquire_process_run_lock("run_cross_process")
        try:
            assert first is not None
            assert operative._try_acquire_process_run_lock("run_cross_process") is None
        finally:
            operative._release_process_run_lock(first)

        second = operative._try_acquire_process_run_lock("run_cross_process")
        try:
            assert second is not None
        finally:
            operative._release_process_run_lock(second)
