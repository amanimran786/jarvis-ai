"""
tests/test_prompt_optimizer.py

Tests for the autonomous prompt engineering loop.
All LLM calls are mocked — no real inference required.
"""
import json
import unittest
from unittest.mock import MagicMock, patch, call

from local_runtime.prompt_optimizer import (
    EvalCase,
    OptimizationResult,
    PromptTemplate,
    _check_grounding,
    _revise_prompt,
    _summarize_failures,
    CaseResult,
    optimize_prompt,
)


# ---------------------------------------------------------------------------
# PromptTemplate
# ---------------------------------------------------------------------------

class PromptTemplateRenderTests(unittest.TestCase):
    def test_render_substitutes_variables(self):
        t = PromptTemplate(
            role_context="You are {{ROLE}}.",
            instructions="Do {{TASK}}.",
            output_format="Return text.",
        )
        result = t.render({"ROLE": "an assistant", "TASK": "the work"})
        self.assertIn("You are an assistant.", result)
        self.assertIn("Do the work.", result)

    def test_render_omits_empty_components(self):
        t = PromptTemplate(
            role_context="You are an assistant.",
            instructions="Be helpful.",
            output_format="",
            tone_context="",
        )
        result = t.render()
        self.assertNotIn("output_format", result)
        self.assertNotIn("tone_context", result)
        self.assertIn("Be helpful.", result)

    def test_render_wraps_examples_in_tags(self):
        t = PromptTemplate(
            role_context="You are a bot.",
            instructions="Answer questions.",
            output_format="One sentence.",
            examples="Q: hi\nA: hello",
        )
        result = t.render()
        self.assertIn("<examples>", result)
        self.assertIn("Q: hi", result)
        self.assertIn("</examples>", result)

    def test_render_ordering(self):
        t = PromptTemplate(
            role_context="ROLE",
            tone_context="TONE",
            instructions="INSTRUCTIONS",
            output_format="FORMAT",
        )
        result = t.render()
        self.assertLess(result.index("ROLE"), result.index("TONE"))
        self.assertLess(result.index("TONE"), result.index("INSTRUCTIONS"))
        self.assertLess(result.index("INSTRUCTIONS"), result.index("FORMAT"))

    def test_copy_produces_independent_instance(self):
        t = PromptTemplate(
            role_context="original",
            instructions="do things",
            output_format="",
        )
        copy = t.copy()
        copy.role_context = "changed"
        self.assertEqual(t.role_context, "original")


# ---------------------------------------------------------------------------
# _check_grounding
# ---------------------------------------------------------------------------

class GroundingCheckTests(unittest.TestCase):
    def test_grounded_response_returns_empty_list(self):
        with patch("brains.brain_ollama.ask_local", return_value="GROUNDED") as _al, \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            result = _check_grounding("The sky is blue.", "The sky is blue.", allow_cloud=False)
        self.assertEqual(result, [])

    def test_ungrounded_response_returns_claims(self):
        with patch("brains.brain_ollama.ask_local", return_value="The sun is made of cheese.\nThe moon is pink.") as _al, \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            result = _check_grounding("some output", "some reference", allow_cloud=False)
        self.assertEqual(len(result), 2)
        self.assertIn("The sun is made of cheese.", result)

    def test_grounding_fails_open_on_model_error(self):
        with patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("no ollama")), \
             patch("brains.brain_ollama.get_best_available", return_value="test-model"):
            result = _check_grounding("output", "grounding", allow_cloud=False)
        self.assertEqual(result, [])

    def test_grounding_uses_ask_with_priority_when_cloud_enabled(self):
        with patch("provider_priority.ask_with_priority", return_value="GROUNDED") as mock_cloud:
            result = _check_grounding("output", "grounding", allow_cloud=True)
        mock_cloud.assert_called_once()
        self.assertEqual(result, [])

    def test_grounded_prefix_match_is_case_insensitive(self):
        with patch("brains.brain_ollama.ask_local", return_value="grounded — all good") as _al, \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _check_grounding("output", "ref", allow_cloud=False)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _revise_prompt
# ---------------------------------------------------------------------------

class RevisionTests(unittest.TestCase):
    def _template(self):
        return PromptTemplate(
            role_context="You are a travel bot.",
            instructions="Suggest destinations.",
            output_format="Use bullet points.",
            examples="",
            tone_context="Be friendly.",
        )

    def _mock_revision_response(self, component: str, revision: str, rationale: str) -> str:
        return (
            f"<component>{component}</component>\n"
            f"<revision>{revision}</revision>\n"
            f"<rationale>{rationale}</rationale>"
        )

    def test_revision_updates_instructions_component(self):
        response = self._mock_revision_response(
            "instructions",
            "Suggest destinations based on budget and season.",
            "Added specificity to fix vague outputs",
        )
        with patch("brains.brain_ollama.ask_local", return_value=response), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _revise_prompt(self._template(), "Case 1 failed: too vague")

        self.assertIsNotNone(result)
        self.assertEqual(result.component, "instructions")
        self.assertIn("budget and season", result.prompt.instructions)
        self.assertEqual(result.prompt.role_context, "You are a travel bot.")  # unchanged

    def test_revision_updates_role_context_component(self):
        response = self._mock_revision_response(
            "role_context",
            "You are an expert travel agent with 10 years experience.",
            "Added authority markers",
        )
        with patch("brains.brain_ollama.ask_local", return_value=response), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _revise_prompt(self._template(), "failures here")

        self.assertIsNotNone(result)
        self.assertIn("expert travel agent", result.prompt.role_context)
        self.assertEqual(result.prompt.instructions, "Suggest destinations.")  # unchanged

    def test_revision_returns_none_when_no_xml_tags(self):
        with patch("brains.brain_ollama.ask_local", return_value="I suggest rewriting everything"), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _revise_prompt(self._template(), "failures")
        self.assertIsNone(result)

    def test_revision_returns_none_on_unknown_component(self):
        response = self._mock_revision_response("system_prompt", "something", "rationale")
        with patch("brains.brain_ollama.ask_local", return_value=response), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _revise_prompt(self._template(), "failures")
        self.assertIsNone(result)

    def test_revision_returns_none_on_model_error(self):
        with patch("brains.brain_ollama.ask_local", side_effect=RuntimeError("down")), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = _revise_prompt(self._template(), "failures")
        self.assertIsNone(result)

    def test_revision_does_not_mutate_original_template(self):
        original_instructions = "Suggest destinations."
        t = self._template()
        response = self._mock_revision_response("instructions", "New instructions.", "reason")
        with patch("brains.brain_ollama.ask_local", return_value=response), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            _revise_prompt(t, "failures")
        self.assertEqual(t.instructions, original_instructions)


# ---------------------------------------------------------------------------
# optimize_prompt
# ---------------------------------------------------------------------------

def _make_judge_response(passed: bool, score: float, rationale: str) -> str:
    return json.dumps({"pass": passed, "score": score, "rationale": rationale})


def _revision_xml(component: str, text: str) -> str:
    return (
        f"<component>{component}</component>"
        f"<revision>{text}</revision>"
        f"<rationale>fixed because of failure</rationale>"
    )


class OptimizePromptTests(unittest.TestCase):
    def setUp(self):
        self.template = PromptTemplate(
            role_context="You are a helpful assistant.",
            instructions="Answer the user's question clearly.",
            output_format="One paragraph.",
        )
        self.cases = [
            EvalCase(
                user_message="What is Python?",
                expected="A clear explanation of the Python programming language.",
            )
        ]

    def _patch_all(
        self,
        *,
        model_output: str = "Python is a programming language.",
        judge_output: str | None = None,
        grounding_output: str = "GROUNDED",
        revision_output: str | None = None,
        judge_side_effect=None,
    ):
        """Return a context manager that mocks ask_local for all internal calls."""
        if judge_output is None:
            judge_output = _make_judge_response(True, 4.5, "good answer")

        # ask_local is called by: _run_case (model), _judge_output (judge), _check_grounding
        # We differentiate by prompt content
        def side_effect(prompt, **kwargs):
            # Judge prompts start with "Score this response"
            if prompt.startswith("Score this response"):
                if judge_side_effect:
                    raise judge_side_effect
                return judge_output
            # Grounding check prompts start with "You are a fact-checker"
            if "fact-checker" in (kwargs.get("system_extra", "") or prompt[:80]):
                return grounding_output
            # Revision prompts start with "You are a prompt engineer"
            if "prompt engineer" in (kwargs.get("system_extra", "") or prompt[:80]):
                return revision_output or _revision_xml("instructions", "clearer instructions")
            # Default: model output
            return model_output

        return patch("brains.brain_ollama.ask_local", side_effect=side_effect)

    def test_raises_on_empty_cases(self):
        with self.assertRaises(ValueError):
            optimize_prompt(self.template, [])

    def test_returns_commit_ready_when_threshold_met_first_iteration(self):
        # Judge returns passing score immediately
        with patch("brains.brain_ollama.ask_local") as mock_local, \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            mock_local.side_effect = lambda prompt, **kw: (
                _make_judge_response(True, 5.0, "perfect")
                if "expert evaluator" in kw.get("system_extra", "") else "Python is a language."
            )
            result = optimize_prompt(
                self.template, self.cases,
                pass_threshold=0.8, max_iterations=3, allow_cloud=False,
            )

        self.assertTrue(result.commit_ready)
        self.assertEqual(result.iterations, 1)
        self.assertGreaterEqual(result.final_score, 0.8)

    def test_loops_and_revises_until_threshold_met(self):
        call_counts = {"judge": 0}

        def side_effect(prompt, **kw):
            system = kw.get("system_extra", "")
            if "expert evaluator" in system:
                call_counts["judge"] += 1
                score = 5.0 if call_counts["judge"] > 1 else 1.0
                passed = call_counts["judge"] > 1
                return _make_judge_response(passed, score, "rationale")
            if "prompt engineer" in system:
                return _revision_xml("instructions", "Better instructions.")
            return "Python is a language."

        with patch("brains.brain_ollama.ask_local", side_effect=side_effect), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = optimize_prompt(
                self.template, self.cases,
                pass_threshold=0.8, max_iterations=5, allow_cloud=False,
            )

        self.assertTrue(result.commit_ready)
        self.assertGreater(result.iterations, 1)
        self.assertGreater(len(result.revision_log), 0)

    def test_stops_at_max_iterations_when_threshold_never_met(self):
        with patch("brains.brain_ollama.ask_local") as mock_local, \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            mock_local.side_effect = lambda prompt, **kw: (
                _make_judge_response(False, 1.0, "always fails")
                if "expert evaluator" in kw.get("system_extra", "") else (
                    _revision_xml("instructions", "Attempt.")
                    if "prompt engineer" in kw.get("system_extra", "")
                    else "bad output"
                )
            )
            result = optimize_prompt(
                self.template, self.cases,
                pass_threshold=0.9, max_iterations=3, allow_cloud=False,
            )

        self.assertEqual(result.iterations, 3)
        self.assertFalse(result.commit_ready)

    def test_commit_ready_false_when_grounding_check_fails(self):
        def side_effect(prompt, **kw):
            system = kw.get("system_extra", "")
            if "expert evaluator" in system:
                return _make_judge_response(True, 5.0, "great")
            if "fact-checker" in system:
                return "Claim: Python was invented in 1970 — not in reference data."
            return "Python is a language."

        cases_with_grounding = [
            EvalCase(
                user_message="What is Python?",
                expected="Accurate Python description.",
                grounding="Python was created by Guido van Rossum in 1991.",
            )
        ]
        with patch("brains.brain_ollama.ask_local", side_effect=side_effect), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = optimize_prompt(
                self.template, cases_with_grounding,
                pass_threshold=0.8, max_iterations=2, allow_cloud=False,
            )

        self.assertFalse(result.commit_ready)
        grounding_failures = [f for r in result.case_results for f in r.grounding_failures]
        self.assertGreater(len(grounding_failures), 0)

    def test_revision_log_records_component_and_rationale(self):
        call_counts = {"judge": 0}

        def side_effect(prompt, **kw):
            system = kw.get("system_extra", "")
            if "expert evaluator" in system:
                call_counts["judge"] += 1
                passed = call_counts["judge"] > 1
                score = 5.0 if passed else 1.0
                return _make_judge_response(passed, score, "rationale")
            if "prompt engineer" in system:
                return _revision_xml("output_format", "Be concise.")
            return "Some output."

        with patch("brains.brain_ollama.ask_local", side_effect=side_effect), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = optimize_prompt(
                self.template, self.cases,
                pass_threshold=0.8, max_iterations=5, allow_cloud=False,
            )

        self.assertGreater(len(result.revision_log), 0)
        self.assertIn("output_format", result.revision_log[0])

    def test_case_results_populated_on_failure(self):
        with patch("brains.brain_ollama.ask_local") as mock_local, \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            mock_local.side_effect = lambda prompt, **kw: (
                _make_judge_response(False, 1.0, "wrong")
                if "expert evaluator" in kw.get("system_extra", "") else "bad"
            )
            result = optimize_prompt(
                self.template, self.cases,
                pass_threshold=0.9, max_iterations=1, allow_cloud=False,
            )

        self.assertEqual(len(result.case_results), 1)
        self.assertFalse(result.case_results[0].passed)
        self.assertLess(result.case_results[0].score, 0.5)

    def test_commit_ready_true_requires_both_score_and_grounding(self):
        # Score above threshold but grounding fails → NOT commit ready
        def side_effect(prompt, **kw):
            system = kw.get("system_extra", "")
            if "expert evaluator" in system:
                return _make_judge_response(True, 5.0, "perfect")
            if "fact-checker" in system:
                return "Hallucinated claim found."
            return "some output"

        cases_with_grounding = [
            EvalCase(
                user_message="Q?", expected="A.", grounding="Real data here."
            )
        ]
        with patch("brains.brain_ollama.ask_local", side_effect=side_effect), \
             patch("brains.brain_ollama.get_best_available", return_value="m"):
            result = optimize_prompt(
                self.template, cases_with_grounding,
                pass_threshold=0.8, max_iterations=1, allow_cloud=False,
            )
        self.assertFalse(result.commit_ready)


# ---------------------------------------------------------------------------
# _summarize_failures
# ---------------------------------------------------------------------------

class SummarizeFailuresTests(unittest.TestCase):
    def test_no_failures_returns_no_failures(self):
        case = EvalCase(user_message="hi", expected="hello")
        results = [CaseResult(case=case, output="hello", score=1.0, passed=True, grounding_failures=[], rationale="good")]
        text = _summarize_failures(results)
        self.assertEqual(text, "No failures.")

    def test_failed_case_included_in_summary(self):
        case = EvalCase(user_message="What is X?", expected="A definition of X")
        results = [CaseResult(
            case=case, output="I don't know", score=0.2,
            passed=False, grounding_failures=[], rationale="unhelpful"
        )]
        text = _summarize_failures(results)
        self.assertIn("I don't know", text)
        self.assertIn("0.20", text)

    def test_grounding_failures_included_in_summary(self):
        case = EvalCase(user_message="Q?", expected="A.")
        results = [CaseResult(
            case=case, output="output", score=0.3,
            passed=False, grounding_failures=["Claim X is unsupported"],
            rationale="bad"
        )]
        text = _summarize_failures(results)
        self.assertIn("Claim X is unsupported", text)


if __name__ == "__main__":
    unittest.main()
