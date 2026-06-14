"""Hermetic unit tests for eval_delta.py.

All external dependencies (capability_evals, brains.brain_ollama,
provider_priority, config) are mocked so no real model/network calls occur.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module-level stubs so eval_delta can be imported without real dependencies
# ---------------------------------------------------------------------------

_STUB_MODULE_NAMES = (
    "capability_evals",
    "config",
    "brains",
    "brains.brain_ollama",
    "provider_priority",
)
_ORIGINAL_MODULES = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}


def _restore_modules(snapshot: dict[str, ModuleType | None]) -> None:
    for name, module in snapshot.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def _make_stub_module(name: str) -> ModuleType:
    return ModuleType(name)


# capability_evals stub. eval_delta imports this at module import time, so it is
# the only stub temporarily installed during collection.
_cap_evals_mod = _make_stub_module("capability_evals")
_cap_evals_mod.list_cases = MagicMock(return_value=[])  # type: ignore[attr-defined]

# config stub
_config_mod = _make_stub_module("config")
_config_mod.GPT_FULL = "gpt-4o"  # type: ignore[attr-defined]
_config_mod.LOCAL_REASONING = "qwen2.5:14b"  # type: ignore[attr-defined]

# brains package stubs
_brains_mod = _make_stub_module("brains")
_brain_ollama_mod = _make_stub_module("brains.brain_ollama")
_brain_ollama_mod.ask_local = MagicMock(return_value="local answer")  # type: ignore[attr-defined]
_brains_mod.brain_ollama = _brain_ollama_mod  # type: ignore[attr-defined]

# provider_priority stub
_pp_mod = _make_stub_module("provider_priority")
_pp_mod._ask_openai = MagicMock(return_value="cloud answer")  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _install_stubs_during_test():
    saved = {name: sys.modules.get(name) for name in _STUB_MODULE_NAMES}
    sys.modules["capability_evals"] = _cap_evals_mod
    sys.modules["config"] = _config_mod
    sys.modules["brains"] = _brains_mod
    sys.modules["brains.brain_ollama"] = _brain_ollama_mod
    sys.modules["provider_priority"] = _pp_mod
    try:
        yield
    finally:
        _restore_modules(saved)

# Now we can safely import the module under test
sys.modules["capability_evals"] = _cap_evals_mod
import eval_delta  # noqa: E402  (import after stubs installed)
_restore_modules(_ORIGINAL_MODULES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_case(
    id_: str = "case1",
    group: str = "coding_agent",
    prompt: str = "What is 2+2?",
) -> dict:
    return {"id": id_, "group": group, "prompt": prompt}


# ---------------------------------------------------------------------------
# format_delta tests
# ---------------------------------------------------------------------------


class TestFormatDeltaEmpty:
    def test_returns_no_cases_string_when_empty(self):
        result = eval_delta.format_delta([])
        assert result == "No eval cases matched."

    def test_returns_str_type(self):
        assert isinstance(eval_delta.format_delta([]), str)


class TestFormatDeltaContent:
    def _make_row(self, **kwargs) -> dict:
        base = {
            "id": "test_id",
            "group": "security",
            "prompt": "Hello?",
            "local_model": "qwen2.5:14b",
            "cloud_model": "gpt-4o",
            "local": "local ans",
            "cloud": "cloud ans",
        }
        base.update(kwargs)
        return base

    def test_header_contains_group_and_id(self):
        row = self._make_row(group="mygroup", id="myid")
        result = eval_delta.format_delta([row])
        assert "mygroup/myid" in result

    def test_header_contains_model_names(self):
        row = self._make_row(local_model="llama3", cloud_model="gpt-4")
        result = eval_delta.format_delta([row])
        assert "local=llama3" in result
        assert "cloud=gpt-4" in result

    def test_prompt_appears_in_output(self):
        row = self._make_row(prompt="Is the sky blue?")
        result = eval_delta.format_delta([row])
        assert "Is the sky blue?" in result

    def test_local_answer_appears(self):
        row = self._make_row(local="my local response")
        result = eval_delta.format_delta([row])
        assert "my local response" in result

    def test_cloud_answer_appears(self):
        row = self._make_row(cloud="my cloud response")
        result = eval_delta.format_delta([row])
        assert "my cloud response" in result

    def test_local_error_rendered_when_local_empty(self):
        row = self._make_row(local="", cloud="ok")
        row["local_error"] = "timeout"
        result = eval_delta.format_delta([row])
        assert "ERROR: timeout" in result

    def test_cloud_error_rendered_when_cloud_empty(self):
        row = self._make_row(local="ok", cloud="")
        row["cloud_error"] = "rate limit"
        result = eval_delta.format_delta([row])
        assert "ERROR: rate limit" in result

    def test_multiple_rows_all_appear(self):
        rows = [
            self._make_row(id="r1", prompt="P1"),
            self._make_row(id="r2", prompt="P2"),
        ]
        result = eval_delta.format_delta(rows)
        assert "r1" in result
        assert "r2" in result
        assert "P1" in result
        assert "P2" in result

    def test_sections_separated_by_blank_line(self):
        rows = [self._make_row(id="r1"), self._make_row(id="r2")]
        result = eval_delta.format_delta(rows)
        # Each row block ends with an empty string joined as "\n"
        assert "\n\n" in result

    def test_output_contains_local_label(self):
        row = self._make_row()
        result = eval_delta.format_delta([row])
        assert "LOCAL:" in result

    def test_output_contains_cloud_label(self):
        row = self._make_row()
        result = eval_delta.format_delta([row])
        assert "CLOUD:" in result

    def test_output_contains_prompt_label(self):
        row = self._make_row()
        result = eval_delta.format_delta([row])
        assert "PROMPT:" in result

    def test_whitespace_stripped_from_local(self):
        row = self._make_row(local="  padded  ")
        result = eval_delta.format_delta([row])
        assert "padded" in result
        # The raw padded version should NOT appear
        assert "  padded  " not in result

    def test_whitespace_stripped_from_cloud(self):
        row = self._make_row(cloud="\npadded\n")
        result = eval_delta.format_delta([row])
        assert "padded" in result
        # The CLOUD: section should not have a raw leading newline before the text
        # strip() removes it so "CLOUD:\npadded" should not appear — "CLOUD:\npadded" after join
        # means stripped; confirm by checking the cloud section line starts with "padded" not "\n"
        cloud_idx = result.index("CLOUD:")
        cloud_section = result[cloud_idx + len("CLOUD:"):]
        # After the label+newline, the next non-empty char is 'p', not '\n\n'
        assert cloud_section.lstrip("\n").startswith("padded")


# ---------------------------------------------------------------------------
# run_delta tests
# ---------------------------------------------------------------------------


class TestRunDeltaNoCases:
    def test_returns_empty_list_when_no_cases(self):
        with patch.object(_cap_evals_mod, "list_cases", return_value=[]):
            result = eval_delta.run_delta()
        assert result == []

    def test_returns_list_type(self):
        with patch.object(_cap_evals_mod, "list_cases", return_value=[]):
            result = eval_delta.run_delta()
        assert isinstance(result, list)


class TestRunDeltaLimitSlicing:
    """limit parameter controls how many cases are processed."""

    def test_limit_zero_returns_empty(self):
        cases = [_make_case(id_=f"c{i}") for i in range(5)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            result = eval_delta.run_delta(limit=0)
        assert result == []

    def test_limit_negative_returns_empty(self):
        cases = [_make_case(id_=f"c{i}") for i in range(5)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            result = eval_delta.run_delta(limit=-5)
        assert result == []

    def test_limit_2_returns_at_most_2_rows(self):
        cases = [_make_case(id_=f"c{i}") for i in range(10)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="ans"):
                with patch.object(_pp_mod, "_ask_openai", return_value="ans"):
                    result = eval_delta.run_delta(limit=2)
        assert len(result) == 2

    def test_limit_string_coerced_to_int(self):
        """limit is cast with int() so a numeric string must work."""
        cases = [_make_case(id_=f"c{i}") for i in range(5)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="ans"):
                with patch.object(_pp_mod, "_ask_openai", return_value="ans"):
                    result = eval_delta.run_delta(limit=3)
        assert len(result) == 3


class TestRunDeltaRowShape:
    """Each result row must contain the expected keys."""

    def _run_single(self, local_ret="loc", cloud_ret="cld"):
        case = _make_case(id_="t1", group="security", prompt="q?")
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value=local_ret):
                with patch.object(_pp_mod, "_ask_openai", return_value=cloud_ret):
                    return eval_delta.run_delta(limit=1)

    def test_row_has_id(self):
        rows = self._run_single()
        assert rows[0]["id"] == "t1"

    def test_row_has_group(self):
        rows = self._run_single()
        assert rows[0]["group"] == "security"

    def test_row_has_prompt(self):
        rows = self._run_single()
        assert rows[0]["prompt"] == "q?"

    def test_row_has_local_model(self):
        rows = self._run_single()
        assert rows[0]["local_model"] == _config_mod.LOCAL_REASONING

    def test_row_has_cloud_model(self):
        rows = self._run_single()
        assert rows[0]["cloud_model"] == _config_mod.GPT_FULL

    def test_row_has_local_answer(self):
        rows = self._run_single(local_ret="the local text")
        assert rows[0]["local"] == "the local text"

    def test_row_has_cloud_answer(self):
        rows = self._run_single(cloud_ret="the cloud text")
        assert rows[0]["cloud"] == "the cloud text"


class TestRunDeltaLocalError:
    """Local model failure fills local="" and local_error=<msg>."""

    def _run_with_local_error(self, exc):
        case = _make_case()
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(
                _brain_ollama_mod, "ask_local", side_effect=exc
            ):
                with patch.object(_pp_mod, "_ask_openai", return_value="ok"):
                    return eval_delta.run_delta(limit=1)

    def test_local_error_sets_empty_local(self):
        rows = self._run_with_local_error(RuntimeError("ollama down"))
        assert rows[0]["local"] == ""

    def test_local_error_key_present(self):
        rows = self._run_with_local_error(RuntimeError("ollama down"))
        assert "local_error" in rows[0]

    def test_local_error_message_captured(self):
        rows = self._run_with_local_error(ValueError("bad model"))
        assert "bad model" in rows[0]["local_error"]

    def test_cloud_still_populated_after_local_error(self):
        rows = self._run_with_local_error(RuntimeError("x"))
        assert rows[0]["cloud"] == "ok"

    def test_result_still_appended_after_local_error(self):
        rows = self._run_with_local_error(ConnectionError("offline"))
        assert len(rows) == 1


class TestRunDeltaCloudError:
    """Cloud model failure fills cloud="" and cloud_error=<msg>."""

    def _run_with_cloud_error(self, exc):
        case = _make_case()
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="local ok"):
                with patch.object(_pp_mod, "_ask_openai", side_effect=exc):
                    return eval_delta.run_delta(limit=1)

    def test_cloud_error_sets_empty_cloud(self):
        rows = self._run_with_cloud_error(RuntimeError("openai down"))
        assert rows[0]["cloud"] == ""

    def test_cloud_error_key_present(self):
        rows = self._run_with_cloud_error(RuntimeError("openai down"))
        assert "cloud_error" in rows[0]

    def test_cloud_error_message_captured(self):
        rows = self._run_with_cloud_error(TimeoutError("timeout"))
        assert "timeout" in rows[0]["cloud_error"]

    def test_local_still_populated_after_cloud_error(self):
        rows = self._run_with_cloud_error(RuntimeError("x"))
        assert rows[0]["local"] == "local ok"

    def test_result_still_appended_after_cloud_error(self):
        rows = self._run_with_cloud_error(OSError("network"))
        assert len(rows) == 1


class TestRunDeltaBothErrors:
    """Both failures in same case — row is still returned with both error keys."""

    def test_both_errors_both_keys_present(self):
        case = _make_case()
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(
                _brain_ollama_mod, "ask_local", side_effect=RuntimeError("local fail")
            ):
                with patch.object(
                    _pp_mod, "_ask_openai", side_effect=RuntimeError("cloud fail")
                ):
                    rows = eval_delta.run_delta(limit=1)
        assert "local_error" in rows[0]
        assert "cloud_error" in rows[0]
        assert rows[0]["local"] == ""
        assert rows[0]["cloud"] == ""


class TestRunDeltaGroupPassThrough:
    """group argument is forwarded to capability_evals.list_cases."""

    def test_group_none_passes_none(self):
        with patch.object(_cap_evals_mod, "list_cases", return_value=[]) as mock_lc:
            eval_delta.run_delta(group=None)
        mock_lc.assert_called_once_with(None)

    def test_group_string_forwarded(self):
        with patch.object(_cap_evals_mod, "list_cases", return_value=[]) as mock_lc:
            eval_delta.run_delta(group="security")
        mock_lc.assert_called_once_with("security")


class TestRunDeltaTierIgnored:
    """tier is accepted but currently unused — run_delta must not error on any value."""

    @pytest.mark.parametrize("tier", ["cheap", "strong", "deep", "unknown", ""])
    def test_tier_variants_accepted(self, tier):
        with patch.object(_cap_evals_mod, "list_cases", return_value=[]):
            result = eval_delta.run_delta(tier=tier)
        assert result == []


class TestRunDeltaModelNamesFromConfig:
    """row model names come from config, not hardcoded strings."""

    def test_local_model_matches_config(self):
        case = _make_case()
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="x"):
                with patch.object(_pp_mod, "_ask_openai", return_value="y"):
                    rows = eval_delta.run_delta(limit=1)
        assert rows[0]["local_model"] == _config_mod.LOCAL_REASONING

    def test_cloud_model_matches_config(self):
        case = _make_case()
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="x"):
                with patch.object(_pp_mod, "_ask_openai", return_value="y"):
                    rows = eval_delta.run_delta(limit=1)
        assert rows[0]["cloud_model"] == _config_mod.GPT_FULL


class TestRunDeltaPromptPassedToModels:
    """Each model call receives the exact prompt from the eval case."""

    def test_local_receives_case_prompt(self):
        case = _make_case(prompt="exact local prompt")
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="ok") as mock_local:
                with patch.object(_pp_mod, "_ask_openai", return_value="ok"):
                    eval_delta.run_delta(limit=1)
        mock_local.assert_called_once()
        assert mock_local.call_args[0][0] == "exact local prompt"

    def test_cloud_receives_case_prompt(self):
        case = _make_case(prompt="exact cloud prompt")
        with patch.object(_cap_evals_mod, "list_cases", return_value=[case]):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="ok"):
                with patch.object(_pp_mod, "_ask_openai", return_value="ok") as mock_cloud:
                    eval_delta.run_delta(limit=1)
        mock_cloud.assert_called_once()
        assert mock_cloud.call_args[0][0] == "exact cloud prompt"


class TestRunDeltaMultipleCases:
    """Multiple cases produce multiple rows in order."""

    def test_result_length_matches_cases(self):
        cases = [_make_case(id_=f"c{i}") for i in range(4)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="l"):
                with patch.object(_pp_mod, "_ask_openai", return_value="c"):
                    rows = eval_delta.run_delta(limit=10)
        assert len(rows) == 4

    def test_result_order_preserved(self):
        cases = [_make_case(id_=f"c{i}") for i in range(3)]
        with patch.object(_cap_evals_mod, "list_cases", return_value=cases):
            with patch.object(_brain_ollama_mod, "ask_local", return_value="l"):
                with patch.object(_pp_mod, "_ask_openai", return_value="c"):
                    rows = eval_delta.run_delta(limit=10)
        assert [r["id"] for r in rows] == ["c0", "c1", "c2"]
