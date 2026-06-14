"""Unit tests for brains/_postprocess.py public functions.

Covers strip_think_blocks() and strip_markdown(). Assertions match the
module's real runtime behavior (verified empirically) — these tests document
the contract, they do not propose changes to the source.
"""

import pytest

from brains._postprocess import strip_markdown, strip_think_blocks


# ---------------------------------------------------------------------------
# strip_think_blocks()
# ---------------------------------------------------------------------------


class TestStripThinkBlocks:
    def test_none_returns_none(self):
        # Guard short-circuits on falsy input before any regex runs.
        assert strip_think_blocks(None) is None

    def test_empty_string_returns_empty(self):
        assert strip_think_blocks("") == ""

    def test_text_without_think_block_unchanged(self):
        text = "Just a normal sentence with no tags."
        assert strip_think_blocks(text) == text

    def test_text_with_angle_brackets_but_no_think_unchanged(self):
        # Substring guard requires the literal "<think>"; lone "<" is safe.
        text = "a < b and c > d"
        assert strip_think_blocks(text) == text

    def test_single_think_block_removed(self):
        assert strip_think_blocks("a<think>x</think>b") == "ab"

    def test_single_block_leaves_surrounding_text(self):
        result = strip_think_blocks("Answer: <think>reasoning</think>42")
        assert result == "Answer: 42"

    def test_multiline_think_block_removed(self):
        text = "before\n<think>line1\nline2</think>\nafter"
        # DOTALL lets the block span newlines; leftover blank line collapses.
        assert strip_think_blocks(text) == "before\n\nafter"

    def test_multiple_think_blocks_all_removed(self):
        assert strip_think_blocks("a<think>1</think>b<think>2</think>c") == "abc"

    def test_non_greedy_does_not_over_consume(self):
        # Two adjacent blocks must not be merged into one greedy match.
        text = "x<think>A</think>keep<think>B</think>y"
        assert strip_think_blocks(text) == "xkeepy"

    def test_blank_lines_from_removed_block_collapsed(self):
        text = "head\n\n<think>\nthinking\n</think>\n\ntail"
        result = strip_think_blocks(text)
        assert "<think>" not in result and "</think>" not in result
        assert "thinking" not in result
        assert "head" in result and "tail" in result


# ---------------------------------------------------------------------------
# strip_markdown()
# ---------------------------------------------------------------------------


class TestStripMarkdown:
    def test_empty_string_returns_empty(self):
        assert strip_markdown("") == ""

    def test_none_raises_type_error(self):
        # strip_think_blocks passes None through; the first re.sub then fails.
        with pytest.raises(TypeError):
            strip_markdown(None)

    def test_plain_text_only_trimmed(self):
        assert strip_markdown("  hello world  ") == "hello world"

    def test_bold_markers_removed(self):
        assert strip_markdown("this is **bold** text") == "this is bold text"

    def test_italic_markers_removed(self):
        assert strip_markdown("this is *italic* text") == "this is italic text"

    def test_bold_and_italic_combined(self):
        assert strip_markdown("**a** and *b*") == "a and b"

    def test_header_prefix_removed(self):
        assert strip_markdown("# Heading\nbody") == "Heading\nbody"

    def test_all_header_levels_removed(self):
        text = "# H1\n## H2\n### H3\n#### H4\n##### H5\n###### H6"
        assert strip_markdown(text) == "H1\nH2\nH3\nH4\nH5\nH6"

    def test_dash_bullets_removed(self):
        assert strip_markdown("- one\n- two") == "one\ntwo"

    def test_mixed_bullet_markers_removed(self):
        assert strip_markdown("- one\n* two\n• three") == "one\ntwo\nthree"

    def test_numbered_list_dot_and_paren_removed(self):
        assert strip_markdown("1. first\n2) second") == "first\nsecond"

    def test_code_fence_removed_inner_kept(self):
        assert strip_markdown("```python\ncode here\n```") == "code here"

    def test_bare_code_fence_removed(self):
        assert strip_markdown("```\nplain code\n```") == "plain code"

    def test_inline_code_backticks_removed(self):
        assert strip_markdown("use `code` now") == "use code now"

    def test_also_strips_think_blocks(self):
        # strip_markdown delegates to strip_think_blocks first.
        assert strip_markdown("a<think>secret</think>b") == "ab"

    def test_think_block_with_markdown_inside_fully_removed(self):
        text = "Reply: <think>**internal** reasoning</think> done"
        result = strip_markdown(text)
        assert "internal" not in result
        assert "<think>" not in result
        assert "done" in result and "Reply:" in result

    def test_excess_blank_lines_collapsed(self):
        assert strip_markdown("x\n\n\n\ny") == "x\n\ny"

    def test_combined_markdown_document(self):
        text = (
            "# Title\n"
            "Some **bold** and *italic* and `inline`.\n"
            "- bullet one\n"
            "1. step one\n"
            "```js\nlet x = 1;\n```\n"
        )
        result = strip_markdown(text)
        assert "#" not in result
        assert "**" not in result and "`" not in result
        assert "bold" in result and "italic" in result and "inline" in result
        assert "bullet one" in result and "step one" in result
        assert "let x = 1;" in result
        # Output is trimmed of leading/trailing whitespace.
        assert result == result.strip()
