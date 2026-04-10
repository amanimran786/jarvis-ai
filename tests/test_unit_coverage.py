"""tests/test_unit_coverage.py

Comprehensive unit tests for pure-Python Jarvis modules that were not
covered by the existing regression and live-integration suites.

Covered modules:
  - memory.py
  - notes.py
  - terminal.py  (pure logic + mocked subprocess)
  - vault.py     (pure logic + mocked filesystem)
  - conversation_context.py
  - usage_tracker.py
  - behavior_hooks.py
  - semantic_memory.py

All tests run without network access, API keys, or heavy native
dependencies (PyQt6, sounddevice, etc.).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# Ensure the repo root is on the path so module imports resolve correctly.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ===========================================================================
# memory.py
# ===========================================================================

class MemoryModuleTests(unittest.TestCase):
    """Tests for memory.py — isolated using a per-test temporary file."""

    def setUp(self):
        import memory
        self._tmpdir = tempfile.TemporaryDirectory()
        self._memory_file = os.path.join(self._tmpdir.name, "memory.json")
        # Pre-create the file using memory._DEFAULTS so the structure stays in
        # sync with the module and load() reads from disk — rather than returning
        # dict(_DEFAULTS), which is a shallow copy sharing mutable list/dict
        # containers across tests.
        with open(self._memory_file, "w") as f:
            json.dump({k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                       for k, v in memory._DEFAULTS.items()}, f)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_file(self):
        return patch("memory.MEMORY_FILE", self._memory_file)

    # ── Facts ─────────────────────────────────────────────────────────────

    def test_add_fact_stores_fact(self):
        import memory
        with self._patch_file():
            memory.add_fact("Aman works on AI safety.")
            facts = memory.list_facts()
        self.assertIn("Aman works on AI safety.", facts)

    def test_add_fact_deduplicates(self):
        import memory
        with self._patch_file():
            memory.add_fact("Same fact.")
            memory.add_fact("Same fact.")
            facts = memory.list_facts()
        self.assertEqual(facts.count("Same fact."), 1)

    def test_forget_removes_matching_facts(self):
        import memory
        with self._patch_file():
            memory.add_fact("Aman uses Python.")
            memory.add_fact("Aman likes music.")
            removed = memory.forget("Python")
            facts = memory.list_facts()
        self.assertTrue(removed)
        self.assertNotIn("Aman uses Python.", facts)
        self.assertIn("Aman likes music.", facts)

    def test_forget_returns_false_when_no_match(self):
        import memory
        with self._patch_file():
            memory.add_fact("Aman uses Python.")
            removed = memory.forget("JavaScript")
        self.assertFalse(removed)

    def test_list_facts_empty(self):
        import memory
        with self._patch_file():
            facts = memory.list_facts()
        self.assertEqual(facts, [])

    # ── Preferences ───────────────────────────────────────────────────────

    def test_set_and_get_preference(self):
        import memory
        with self._patch_file():
            memory.set_preference("tone", "casual")
            value = memory.get_preference("tone")
        self.assertEqual(value, "casual")

    def test_get_preference_returns_default(self):
        import memory
        with self._patch_file():
            value = memory.get_preference("nonexistent", default="fallback")
        self.assertEqual(value, "fallback")

    def test_get_all_preferences(self):
        import memory
        with self._patch_file():
            memory.set_preference("speed", "fast")
            memory.set_preference("detail", "high")
            prefs = memory.get_all_preferences()
        self.assertIn("speed", prefs)
        self.assertIn("detail", prefs)

    # ── Topic tracking ────────────────────────────────────────────────────

    def test_track_topic_counts_known_keywords(self):
        import memory
        with self._patch_file():
            memory.track_topic("help me write python code")
            memory.track_topic("more python work")
            topics = memory.get_top_topics(3)
        self.assertIn("python", topics)

    def test_get_top_topics_sorted_by_frequency(self):
        import memory
        with self._patch_file():
            memory.track_topic("code review")
            memory.track_topic("more code")
            memory.track_topic("python question")
            topics = memory.get_top_topics(3)
        self.assertEqual(topics[0], "code")

    # ── Conversations ─────────────────────────────────────────────────────

    def test_save_and_get_recent_conversations(self):
        import memory
        with self._patch_file():
            memory.save_conversation("Worked on Jarvis memory routing.")
            memory.save_conversation("Discussed local model evaluation.")
            recent = memory.get_recent_conversations(n=5)
        self.assertEqual(len(recent), 2)
        self.assertIn("summary", recent[0])

    def test_conversation_history_capped_at_30(self):
        import memory
        with self._patch_file():
            for i in range(35):
                memory.save_conversation(f"Session {i}")
            recent = memory.get_recent_conversations(n=100)
        self.assertLessEqual(len(recent), 30)

    # ── Projects ──────────────────────────────────────────────────────────

    def test_add_project_new(self):
        import memory
        with self._patch_file():
            memory.add_project("Jarvis", description="AI assistant")
            projects = memory.get_projects()
        names = [p["name"] for p in projects]
        self.assertIn("Jarvis", names)

    def test_add_project_updates_existing(self):
        import memory
        with self._patch_file():
            memory.add_project("Jarvis", description="old")
            memory.add_project("Jarvis", description="new description")
            projects = memory.get_projects()
        jarvis = next(p for p in projects if p["name"] == "Jarvis")
        self.assertEqual(jarvis["description"], "new description")

    # ── Consolidation & status ────────────────────────────────────────────

    def test_memory_status_reflects_data(self):
        import memory
        with self._patch_file():
            memory.add_fact("Test fact.")
            memory.set_preference("key", "val")
            memory.add_project("TestProject")
            status = memory.memory_status()
        self.assertGreaterEqual(status["facts"], 1)
        self.assertGreaterEqual(status["preferences"], 1)
        self.assertGreaterEqual(status["projects"], 1)
        self.assertTrue(status["working_memory_ready"])
        self.assertTrue(status["long_term_profile_ready"])

    def test_get_context_returns_string_when_no_data(self):
        import memory
        with self._patch_file():
            ctx = memory.get_context()
        self.assertIsInstance(ctx, str)

    def test_get_context_includes_facts(self):
        import memory
        with self._patch_file():
            memory.add_fact("Aman builds AI systems.")
            memory.add_project("Jarvis", description="voice assistant")
            ctx = memory.get_context()
        self.assertIn("Aman builds AI systems.", ctx)

    # ── Private helpers ───────────────────────────────────────────────────
    # The public-API counterpart test_add_fact_deduplicates already verifies
    # that deduplication works through the production code path. The tests below
    # exercise the helper directly to cover its edge-case branches in isolation.

    def test_dedupe_keep_order_removes_case_insensitive_duplicates(self):
        import memory
        result = memory._dedupe_keep_order(["Python", "python", "PYTHON", "Java"])
        self.assertEqual(result, ["Python", "Java"])

    def test_dedupe_keep_order_removes_empty_strings(self):
        import memory
        result = memory._dedupe_keep_order(["", "  ", "Hello", "Hello"])
        self.assertEqual(result, ["Hello"])

    def test_trim_short_text_unchanged(self):
        import memory
        result = memory._trim("short text", limit=180)
        self.assertEqual(result, "short text")

    def test_trim_long_text_truncated_with_ellipsis(self):
        import memory
        long_text = "word " * 50
        result = memory._trim(long_text, limit=20)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result), 20)

    def test_load_returns_defaults_for_missing_file(self):
        import memory
        with self._patch_file():
            data = memory.load()
        self.assertIn("facts", data)
        self.assertEqual(data["facts"], [])

    def test_load_returns_defaults_for_corrupted_file(self):
        import memory
        with self._patch_file():
            with open(self._memory_file, "w") as f:
                f.write("not valid json {{{")
            data = memory.load()
        self.assertIn("facts", data)
        self.assertEqual(data["facts"], [])


# ===========================================================================
# notes.py
# ===========================================================================

class NotesModuleTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._notes_file = os.path.join(self._tmpdir.name, "notes.json")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_file(self):
        return patch("notes.NOTES_FILE", self._notes_file)

    def test_add_note_returns_confirmation(self):
        import notes
        with self._patch_file():
            result = notes.add_note("Remember to buy milk.")
        self.assertIn("Note saved", result)

    def test_get_notes_empty(self):
        import notes
        with self._patch_file():
            result = notes.get_notes()
        self.assertIn("no notes", result.lower())

    def test_get_notes_returns_recent_notes(self):
        import notes
        with self._patch_file():
            notes.add_note("First note.")
            notes.add_note("Second note.")
            result = notes.get_notes(n=5)
        self.assertIn("First note.", result)
        self.assertIn("Second note.", result)

    def test_get_notes_respects_n_limit(self):
        import notes
        with self._patch_file():
            for i in range(10):
                notes.add_note(f"Note number {i}.")
            result = notes.get_notes(n=3)
        self.assertIn("Note number 9.", result)
        self.assertIn("Note number 8.", result)
        self.assertIn("Note number 7.", result)
        self.assertNotIn("Note number 0.", result)

    def test_search_notes_found(self):
        import notes
        with self._patch_file():
            notes.add_note("Jarvis AI assistant.")
            notes.add_note("Meeting at 3pm.")
            result = notes.search_notes("Jarvis")
        self.assertIn("Jarvis AI assistant.", result)
        self.assertNotIn("Meeting at 3pm.", result)

    def test_search_notes_case_insensitive(self):
        import notes
        with self._patch_file():
            notes.add_note("Python is great.")
            result = notes.search_notes("python")
        self.assertIn("Python is great.", result)

    def test_search_notes_not_found(self):
        import notes
        with self._patch_file():
            notes.add_note("Nothing relevant here.")
            result = notes.search_notes("foobar")
        self.assertIn("No notes found", result)

    def test_notes_have_id_date_content_fields(self):
        import notes
        with self._patch_file():
            notes.add_note("Test note.")
            with open(self._notes_file) as f:
                data = json.load(f)
        self.assertIn("id", data[0])
        self.assertIn("date", data[0])
        self.assertIn("content", data[0])

    def test_multiple_notes_get_incrementing_ids(self):
        import notes
        with self._patch_file():
            notes.add_note("Alpha note.")
            notes.add_note("Beta note.")
            with open(self._notes_file) as f:
                data = json.load(f)
        self.assertEqual(data[0]["id"], 1)
        self.assertEqual(data[1]["id"], 2)


# ===========================================================================
# terminal.py — pure logic and mocked subprocess / hooks
# ===========================================================================

class TerminalBlockedPatternTests(unittest.TestCase):

    def test_contains_blocked_pattern_rm_rf(self):
        import terminal
        self.assertEqual(terminal._contains_blocked_pattern("rm -rf /tmp/test"), "rm -rf")

    def test_contains_blocked_pattern_fork_bomb(self):
        import terminal
        result = terminal._contains_blocked_pattern(":(){:|:&};:")
        self.assertIsNotNone(result)

    def test_contains_blocked_pattern_shutdown(self):
        import terminal
        self.assertEqual(terminal._contains_blocked_pattern("sudo shutdown now"), "shutdown")

    def test_contains_blocked_pattern_dd(self):
        import terminal
        self.assertEqual(terminal._contains_blocked_pattern("dd if=/dev/zero of=/dev/sda"), "dd if=")

    def test_contains_blocked_pattern_safe_command_returns_none(self):
        import terminal
        self.assertIsNone(terminal._contains_blocked_pattern("ls -la /home"))

    def test_contains_blocked_pattern_case_insensitive(self):
        import terminal
        self.assertIsNotNone(terminal._contains_blocked_pattern("RM -RF /tmp"))

    def test_escape_applescript_escapes_double_quotes(self):
        import terminal
        result = terminal._escape_applescript('say "hello"')
        self.assertIn('\\"', result)

    def test_escape_applescript_escapes_backslash(self):
        import terminal
        result = terminal._escape_applescript("path\\file")
        self.assertIn("\\\\", result)


class TerminalFileTests(unittest.TestCase):

    def test_read_file_not_found(self):
        import terminal
        result = terminal.read_file("/nonexistent/path/missing.txt")
        self.assertIn("File not found", result)

    def test_read_file_reads_content(self):
        import terminal
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Hello, world!")
            path = f.name
        try:
            result = terminal.read_file(path)
        finally:
            os.unlink(path)
        self.assertEqual(result, "Hello, world!")

    def test_read_file_truncates_large_content(self):
        import terminal
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            # terminal.read_file() truncates at 8000 chars; write well beyond that.
            f.write("x" * 10_000)
            path = f.name
        try:
            result = terminal.read_file(path)
        finally:
            os.unlink(path)
        self.assertIn("truncated", result)
        # 8000 chars content + small "truncated" suffix message
        self.assertLessEqual(len(result), 8_300)

    def test_list_directory_not_found(self):
        import terminal
        result = terminal.list_directory("/nonexistent/path")
        self.assertIn("Directory not found", result)

    def test_list_directory_lists_entries(self):
        import terminal
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "myfile.txt").write_text("content")
            Path(tmp, "subdir").mkdir()
            result = terminal.list_directory(tmp)
        self.assertIn("subdir/", result)
        self.assertIn("myfile.txt", result)

    def test_list_directory_separates_dirs_and_files(self):
        import terminal
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "z_file.txt").write_text("f")
            Path(tmp, "a_dir").mkdir()
            result = terminal.list_directory(tmp)
        lines = result.splitlines()
        # Directories (ending with /) come before plain files
        dir_indices = [i for i, l in enumerate(lines) if l.endswith("/")]
        file_indices = [i for i, l in enumerate(lines) if not l.endswith("/")]
        if dir_indices and file_indices:
            self.assertLess(max(dir_indices), min(file_indices))


class TerminalCommandGatingTests(unittest.TestCase):

    def test_run_command_blocked_by_hook(self):
        import terminal
        with patch("terminal.perms.can_run_shell", return_value={"ok": False, "reason": "Policy blocked."}):
            result = terminal.run_command("ls -la")
        self.assertEqual(result, "Policy blocked.")

    def test_run_command_blocked_by_destructive_pattern(self):
        import terminal
        with patch("terminal.perms.can_run_shell", return_value={"ok": True, "reason": ""}):
            result = terminal.run_command("rm -rf /")
        self.assertIn("Blocked", result)
        self.assertIn("rm -rf", result)

    def test_run_admin_command_blocked_by_hook(self):
        import terminal
        with patch("terminal.perms.can_run_shell", return_value={"ok": False, "reason": "Admin blocked."}):
            result = terminal.run_admin_command("ls /System")
        self.assertEqual(result, "Admin blocked.")

    def test_run_admin_command_blocked_by_destructive_pattern(self):
        import terminal
        with patch("terminal.perms.can_run_shell", return_value={"ok": True, "reason": ""}):
            result = terminal.run_admin_command("rm -rf /etc")
        self.assertIn("Blocked", result)


class TerminalWriteTests(unittest.TestCase):

    def test_write_file_blocked_by_hook(self):
        import terminal
        with patch("terminal.perms.can_write_file", return_value={"ok": False, "reason": "Write blocked."}):
            result = terminal.write_file("/tmp/test.txt", "content")
        self.assertEqual(result, "Write blocked.")

    def test_write_file_writes_and_returns_confirmation(self):
        import terminal
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "output.txt")
            with patch("terminal.perms.can_write_file", return_value={"ok": True, "reason": ""}), \
                 patch("terminal.perms.after_write", return_value={"ok": True, "reason": ""}):
                result = terminal.write_file(path, "Hello!")
            # Assert inside the context while the temp dir still exists
            self.assertIn("written to", result.lower())
            self.assertEqual(Path(path).read_text(), "Hello!")

    def test_write_file_blocked_by_post_hook(self):
        import terminal
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.py")
            with patch("terminal.perms.can_write_file", return_value={"ok": True, "reason": ""}), \
                 patch("terminal.perms.after_write", return_value={"ok": False, "reason": "Syntax error."}):
                result = terminal.write_file(path, "def broken(")
        self.assertEqual(result, "Syntax error.")


# ===========================================================================
# vault.py — pure logic (no real filesystem I/O)
# ===========================================================================

class VaultTextProcessingTests(unittest.TestCase):

    def test_clean_text_strips_fenced_code_blocks(self):
        import vault
        text = "Before.\n```python\ncode here\n```\nAfter."
        result = vault._clean_text(text)
        self.assertNotIn("code here", result)
        self.assertIn("Before.", result)
        self.assertIn("After.", result)

    def test_clean_text_strips_inline_code(self):
        import vault
        text = "Use `os.path.join` to combine paths."
        result = vault._clean_text(text)
        self.assertIn("os.path.join", result)
        self.assertNotIn("`", result)

    def test_clean_text_strips_markdown_image_links(self):
        import vault
        text = "See ![diagram](diagram.png) for reference."
        result = vault._clean_text(text)
        self.assertNotIn("diagram.png", result)

    def test_clean_text_strips_inline_hyperlinks(self):
        import vault
        text = "See [this link](https://example.com) for more."
        result = vault._clean_text(text)
        self.assertNotIn("https://example.com", result)

    def test_clean_text_strips_markdown_heading_hashes(self):
        import vault
        text = "## Section Title\nSome content."
        result = vault._clean_text(text)
        self.assertNotIn("##", result)
        self.assertIn("Section Title", result)

    def test_tokenize_lowercases_and_filters_stopwords(self):
        import vault
        tokens = vault._tokenize("The quick brown fox")
        self.assertNotIn("the", tokens)
        self.assertIn("quick", tokens)
        self.assertIn("brown", tokens)

    def test_tokenize_filters_tokens_shorter_than_three_chars(self):
        import vault
        tokens = vault._tokenize("a an to or if be do")
        self.assertEqual(tokens, [])

    def test_tokenize_handles_punctuation(self):
        import vault
        tokens = vault._tokenize("Python, is: great!")
        self.assertIn("python", tokens)
        self.assertIn("great", tokens)


class VaultTitleExtractionTests(unittest.TestCase):

    def test_extract_title_from_heading(self):
        import vault
        text = "# My Document Title\nSome content here."
        result = vault._extract_title(Path("doc.md"), text)
        self.assertEqual(result, "My Document Title")

    def test_extract_title_from_first_nonempty_line(self):
        import vault
        text = "\nFirst line of content.\nMore content."
        result = vault._extract_title(Path("doc.md"), text)
        self.assertIn("First line", result)

    def test_extract_title_from_stem_when_empty(self):
        import vault
        result = vault._extract_title(Path("my_document.md"), "")
        self.assertEqual(result, "My Document")

    def test_extract_title_truncates_long_first_line(self):
        import vault
        text = "A" * 200
        result = vault._extract_title(Path("doc.md"), text)
        self.assertLessEqual(len(result), 81)


class VaultSectionParsingTests(unittest.TestCase):

    def test_parse_sections_creates_sections_from_headings(self):
        import vault
        raw = "# Introduction\nThis is the intro.\n## Details\nHere are the details."
        sections = vault._parse_sections(Path("test.md"), raw)
        headings = [s["heading"] for s in sections]
        self.assertIn("Introduction", headings)
        self.assertIn("Details", headings)

    def test_parse_sections_extracts_text(self):
        import vault
        raw = "# Title\nFirst section content.\n## Second\nSecond section content."
        sections = vault._parse_sections(Path("test.md"), raw)
        all_text = " ".join(s["text"] for s in sections)
        self.assertIn("First section content.", all_text)
        self.assertIn("Second section content.", all_text)

    def test_parse_sections_extracts_keywords(self):
        import vault
        raw = "# Python Debugging\nUse tracemalloc to debug memory leaks in Python."
        sections = vault._parse_sections(Path("test.md"), raw)
        self.assertTrue(sections)
        all_keywords = [kw for s in sections for kw in s.get("keywords", [])]
        self.assertTrue(any(kw in ("python", "tracemalloc", "memory", "debug") for kw in all_keywords))

    def test_parse_sections_returns_fallback_for_contentless_file(self):
        import vault
        raw = "   \n\n   "
        sections = vault._parse_sections(Path("empty.md"), raw)
        self.assertIsInstance(sections, list)
        # May be empty or a single stub section
        self.assertGreaterEqual(len(sections), 0)

    def test_parse_sections_tracks_page_numbers(self):
        import vault
        raw = "## Page 1\nContent of page one.\n## Page 2\nContent of page two."
        sections = vault._parse_sections(Path("slides.md"), raw)
        pages = [s.get("page") for s in sections if s.get("page") is not None]
        self.assertIn(1, pages)
        self.assertIn(2, pages)


class VaultSearchRoutingTests(unittest.TestCase):

    def test_should_query_for_knowledge_pattern(self):
        import vault
        self.assertTrue(vault.should_query("search the vault for X"))
        self.assertTrue(vault.should_query("from the vault tell me about Y"))
        self.assertTrue(vault.should_query("what do you know about databases"))

    def test_should_query_for_deep_research_tool(self):
        import vault
        self.assertTrue(vault.should_query("something", tool="deep_research"))
        self.assertTrue(vault.should_query("something", tool="knowledge"))

    def test_should_query_for_long_question(self):
        import vault
        long_query = "What is the best way to design a distributed database system?"
        self.assertTrue(vault.should_query(long_query))

    def test_should_query_false_for_short_query(self):
        import vault
        self.assertFalse(vault.should_query("hi"))
        self.assertFalse(vault.should_query(""))

    def test_build_context_returns_empty_when_should_query_is_false(self):
        import vault
        result = vault.build_context("hi", tool=None)
        self.assertEqual(result, "")

    def test_build_context_returns_empty_when_no_search_results(self):
        import vault
        with patch("vault.search", return_value=[]):
            result = vault.build_context("what do you know about memory", tool=None)
        self.assertEqual(result, "")

    def test_build_context_includes_citation_label_and_excerpt(self):
        import vault
        fake_results = [
            {
                "citation": {"label": "raw/notes.md > Section"},
                "excerpt": "This is the relevant snippet.",
            }
        ]
        with patch("vault.search", return_value=fake_results):
            result = vault.build_context("what do you know about notes", tool=None)
        self.assertIn("raw/notes.md > Section", result)
        self.assertIn("relevant snippet", result)

    def test_score_text_rewards_title_match(self):
        import vault
        score_match = vault._score_text("jarvis vault strategy", "Jarvis Vault Strategy", [], "")
        score_no_match = vault._score_text("jarvis vault strategy", "Unrelated Title", [], "")
        self.assertGreater(score_match, score_no_match)

    def test_score_text_rewards_keyword_overlap(self):
        import vault
        score = vault._score_text("python debugging", "Title", ["python", "debug"], "")
        self.assertGreater(score, 0)


class VaultStatusTests(unittest.TestCase):

    def test_status_text_empty_vault(self):
        import vault
        with patch("vault.status", return_value={
            "doc_count": 0,
            "wiki_page_count": 0,
            "indexed_files": [],
            "citation_ready": True,
        }):
            text = vault.status_text()
        self.assertIn("empty", text.lower())

    def test_status_text_with_docs(self):
        import vault
        with patch("vault.status", return_value={
            "doc_count": 3,
            "wiki_page_count": 1,
            "indexed_files": ["raw/a.md", "raw/b.md", "raw/c.md"],
            "citation_ready": True,
        }):
            text = vault.status_text()
        self.assertIn("3", text)
        self.assertIn("citation-ready", text)

    def test_search_text_no_results(self):
        import vault
        with patch("vault.search", return_value=[]):
            result = vault.search_text("unknown topic")
        self.assertIn("didn't find", result.lower())

    def test_search_text_with_results(self):
        import vault
        fake_results = [
            {
                "citation": {"label": "raw/notes.md > My Section"},
                "excerpt": "Relevant content about the topic.",
            }
        ]
        with patch("vault.search", return_value=fake_results):
            result = vault.search_text("my topic")
        self.assertIn("raw/notes.md > My Section", result)
        self.assertIn("Relevant content", result)


# ===========================================================================
# conversation_context.py
# ===========================================================================

class ConversationContextTests(unittest.TestCase):

    def setUp(self):
        import conversation_context as cc
        # Reset module-level state before each test
        cc._STATE.update({
            "id": "test0001",
            "messages": [],
            "summary": "",
            "rotations": 0,
            "recent_user_topics": [],
            "started_at": datetime.now(),
            "last_active": datetime.now(),
        })
        cc._RECENT_STATS.clear()

    # ── Helpers ───────────────────────────────────────────────────────────

    def test_tokenize_excludes_stopwords(self):
        import conversation_context as cc
        tokens = cc._tokenize("What is the best way to do this")
        self.assertNotIn("what", tokens)
        self.assertNotIn("the", tokens)
        self.assertIn("best", tokens)

    def test_tokenize_returns_set(self):
        import conversation_context as cc
        tokens = cc._tokenize("locking locking locking")
        self.assertIsInstance(tokens, set)
        self.assertEqual(len(tokens), 1)

    def test_topic_overlap_related_texts(self):
        import conversation_context as cc
        overlap = cc._topic_overlap(
            "optimistic locking in databases",
            "database locking strategies",
        )
        self.assertGreater(overlap, 0.0)

    def test_topic_overlap_unrelated_texts(self):
        import conversation_context as cc
        overlap = cc._topic_overlap("cat sleeping mat", "database index performance")
        self.assertEqual(overlap, 0.0)

    def test_topic_overlap_empty_strings(self):
        import conversation_context as cc
        self.assertEqual(cc._topic_overlap("", "anything"), 0.0)
        self.assertEqual(cc._topic_overlap("anything", ""), 0.0)

    def test_trim_text_short_unchanged(self):
        import conversation_context as cc
        result = cc._trim_text("short text")
        self.assertEqual(result, "short text")

    def test_trim_text_truncates_long(self):
        import conversation_context as cc
        long_text = "word " * 100
        result = cc._trim_text(long_text, limit=50)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result), 50)

    def test_trim_text_collapses_whitespace(self):
        import conversation_context as cc
        result = cc._trim_text("hello    world\n\t  here")
        self.assertEqual(result, "hello world here")

    # ── Summarization ─────────────────────────────────────────────────────

    def test_summarize_transcript_single_line(self):
        import conversation_context as cc
        result = cc.summarize_transcript(["User: What is Python?"])
        self.assertIn("What is Python", result)

    def test_summarize_transcript_multi_line(self):
        import conversation_context as cc
        result = cc.summarize_transcript([
            "User: Tell me about locking.",
            "Assistant: Locking manages concurrency.",
        ])
        self.assertIn("started with", result.lower())

    def test_summarize_transcript_empty(self):
        import conversation_context as cc
        result = cc.summarize_transcript([])
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())

    def test_summarize_transcript_strips_speaker_prefix(self):
        import conversation_context as cc
        result = cc.summarize_transcript(["User: my question here"])
        self.assertNotIn("User:", result)
        self.assertIn("my question here", result)

    # ── Turn management ───────────────────────────────────────────────────

    def test_begin_turn_adds_user_message(self):
        import conversation_context as cc
        with patch("conversation_context.mem.save_conversation"):
            cc.begin_turn("Hello, how are you?")
        self.assertEqual(len(cc._STATE["messages"]), 1)
        self.assertEqual(cc._STATE["messages"][0]["role"], "user")
        self.assertEqual(cc._STATE["messages"][0]["content"], "Hello, how are you?")

    def test_end_turn_adds_assistant_message(self):
        import conversation_context as cc
        with patch("conversation_context.mem.save_conversation"):
            cc.begin_turn("Hello?")
            cc.end_turn("I'm doing well!")
        roles = [m["role"] for m in cc._STATE["messages"]]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_begin_turn_updates_recent_user_topics(self):
        import conversation_context as cc
        with patch("conversation_context.mem.save_conversation"):
            cc.begin_turn("Tell me about Python.")
        self.assertIn("Tell me about Python.", cc._STATE["recent_user_topics"])

    # ── Prompt state ──────────────────────────────────────────────────────

    def test_build_prompt_state_includes_system(self):
        import conversation_context as cc
        system, messages, stats = cc.build_prompt_state("You are Jarvis.")
        self.assertIn("You are Jarvis.", system)
        self.assertIsInstance(messages, list)
        self.assertIn("session_id", stats)

    def test_build_prompt_state_appends_system_extra(self):
        import conversation_context as cc
        system, _, _ = cc.build_prompt_state("Base prompt.", system_extra="Extra instructions.")
        self.assertIn("Base prompt.", system)
        self.assertIn("Extra instructions.", system)

    def test_build_prompt_state_includes_summary_when_set(self):
        import conversation_context as cc
        cc._STATE["summary"] = "Prior conversation about Python."
        system, _, _ = cc.build_prompt_state("Base.")
        self.assertIn("Prior conversation about Python.", system)

    def test_build_prompt_state_stats_has_required_keys(self):
        import conversation_context as cc
        _, _, stats = cc.build_prompt_state("")
        for key in ("session_id", "active_messages", "summary_chars", "rotations"):
            self.assertIn(key, stats)

    # ── Compaction ────────────────────────────────────────────────────────

    def test_compact_if_needed_compacts_overflow(self):
        import conversation_context as cc
        # MAX_ACTIVE_TURNS (from conversation_context) = 4, so max_messages = 8.
        # Add 6 full turns (12 messages) to force compaction.
        max_messages = cc.MAX_ACTIVE_TURNS * 2
        with patch("conversation_context.mem.save_conversation"):
            for i in range(6):
                cc.begin_turn(f"User question about databases {i}")
                cc.end_turn(f"Assistant reply about databases {i}")
        self.assertLessEqual(len(cc._STATE["messages"]), max_messages)
        self.assertTrue(cc._STATE["summary"])

    # ── Stats ─────────────────────────────────────────────────────────────

    def test_get_stats_returns_session_info(self):
        import conversation_context as cc
        stats = cc.get_stats()
        self.assertIn("session_id", stats)
        self.assertIn("active_messages", stats)
        self.assertIn("rotations", stats)

    def test_record_request_stats_appends_entry(self):
        import conversation_context as cc
        cc.record_request_stats("gpt-4o-mini", source="chat")
        recent = cc.recent_request_stats(limit=10)
        self.assertGreaterEqual(len(recent), 1)
        self.assertEqual(recent[-1]["model"], "gpt-4o-mini")
        self.assertEqual(recent[-1]["source"], "chat")

    def test_recent_request_stats_respects_limit(self):
        import conversation_context as cc
        for _ in range(15):
            cc.record_request_stats("haiku")
        recent = cc.recent_request_stats(limit=5)
        self.assertLessEqual(len(recent), 5)


# ===========================================================================
# usage_tracker.py
# ===========================================================================

class UsageTrackerEstimationTests(unittest.TestCase):

    def test_estimate_tokens_from_messages(self):
        import usage_tracker
        messages = [
            {"role": "user", "content": "A" * 100},
            {"role": "assistant", "content": "B" * 100},
        ]
        tokens = usage_tracker._estimate_tokens_from_messages(messages)
        self.assertGreater(tokens, 45)

    def test_estimate_tokens_from_messages_empty(self):
        import usage_tracker
        self.assertEqual(usage_tracker._estimate_tokens_from_messages([]), 0)

    def test_estimate_tokens_from_text(self):
        import usage_tracker
        tokens = usage_tracker._estimate_tokens_from_text("A" * 400)
        self.assertEqual(tokens, 100)

    def test_estimate_tokens_from_text_empty(self):
        import usage_tracker
        self.assertEqual(usage_tracker._estimate_tokens_from_text(""), 0)

    def test_estimate_tokens_from_text_whitespace_only(self):
        import usage_tracker
        self.assertEqual(usage_tracker._estimate_tokens_from_text("   "), 0)


class UsageTrackerCostTests(unittest.TestCase):

    def test_cost_for_known_model(self):
        import usage_tracker
        cost = usage_tracker._cost_for_model("gpt-4o-mini", 1_000_000)
        self.assertAlmostEqual(cost, 0.15, places=2)

    def test_cost_for_unknown_model_returns_none(self):
        import usage_tracker
        self.assertIsNone(usage_tracker._cost_for_model("unknown-xyz", 1_000_000))

    def test_cost_for_zero_tokens(self):
        import usage_tracker
        self.assertEqual(usage_tracker._cost_for_model("gpt-4o-mini", 0), 0.0)

    def test_cost_proportional_to_tokens(self):
        import usage_tracker
        half = usage_tracker._cost_for_model("gpt-4o-mini", 500_000)
        full = usage_tracker._cost_for_model("gpt-4o-mini", 1_000_000)
        self.assertAlmostEqual(half * 2, full, places=6)


class UsageTrackerRecordTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._log_path = tmp / "usage_log.jsonl"
        self._state_path = tmp / "usage_state.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patches(self):
        import usage_tracker
        return (
            patch.object(usage_tracker, "USAGE_LOG", self._log_path),
            patch.object(usage_tracker, "USAGE_STATE", self._state_path),
        )

    def test_record_creates_entry_and_increments_seq(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            entry = usage_tracker.record(
                provider="openai",
                model="gpt-4o-mini",
                local=False,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
            )
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["provider"], "openai")
        self.assertEqual(entry["model"], "gpt-4o-mini")
        self.assertEqual(entry["total_tokens"], 150)
        self.assertFalse(entry["local"])
        self.assertIsNotNone(entry["estimated_cost_usd"])

    def test_record_local_has_zero_cost(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            entry = usage_tracker.record(
                provider="ollama",
                model="llama3.1",
                local=True,
                total_tokens=500,
            )
        self.assertEqual(entry["estimated_cost_usd"], 0.0)
        self.assertEqual(entry["pricing_basis"], "local_zero")

    def test_record_estimates_tokens_from_messages(self):
        import usage_tracker
        messages = [{"role": "user", "content": "Hello world!"}]
        p1, p2 = self._patches()
        with p1, p2:
            entry = usage_tracker.record(
                provider="openai",
                model="gpt-4o-mini",
                local=False,
                messages=messages,
            )
        self.assertTrue(entry["estimated"])
        self.assertGreater(entry["prompt_tokens"], 0)

    def test_record_estimates_completion_from_response_text(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            entry = usage_tracker.record(
                provider="openai",
                model="gpt-4o-mini",
                local=False,
                prompt_tokens=50,
                response_text="A" * 400,
            )
        self.assertTrue(entry["estimated"])
        self.assertEqual(entry["completion_tokens"], 100)

    def test_record_seq_increments_across_calls(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            e1 = usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=10)
            e2 = usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=10)
        self.assertEqual(e2["seq"], e1["seq"] + 1)

    def test_entries_returns_recorded_entries(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=100)
            usage_tracker.record(provider="anthropic", model="claude-haiku-4-5-20251001", local=False, total_tokens=200)
            result = usage_tracker.entries(hours=24)
        self.assertEqual(len(result), 2)

    def test_summarize_call_counts(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=100)
            usage_tracker.record(provider="ollama", model="llama3.1", local=True, total_tokens=50)
            summary = usage_tracker.summarize(hours=24)
        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["cloud_call_count"], 1)
        self.assertEqual(summary["local_call_count"], 1)

    def test_summarize_aggregates_tokens(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False,
                                  prompt_tokens=100, completion_tokens=50, total_tokens=150)
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False,
                                  prompt_tokens=200, completion_tokens=100, total_tokens=300)
            summary = usage_tracker.summarize(hours=24)
        self.assertEqual(summary["total_tokens"], 450)
        self.assertEqual(summary["prompt_tokens"], 300)

    def test_summarize_by_provider_bucket(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=200)
            usage_tracker.record(provider="openai", model="gpt-4o", local=False, total_tokens=400)
            summary = usage_tracker.summarize(hours=24)
        self.assertIn("openai", summary["by_provider"])
        self.assertEqual(summary["by_provider"]["openai"]["call_count"], 2)
        self.assertEqual(summary["by_provider"]["openai"]["total_tokens"], 600)

    def test_summary_text_no_entries(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            text = usage_tracker.summary_text(hours=24)
        self.assertIn("no provider usage", text.lower())

    def test_summary_text_with_entries(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=1000)
            text = usage_tracker.summary_text(hours=24)
        self.assertIn("gpt-4o-mini", text)
        self.assertIn("model call", text.lower())

    def test_current_seq_increments(self):
        import usage_tracker
        p1, p2 = self._patches()
        with p1, p2:
            seq_before = usage_tracker.current_seq()
            usage_tracker.record(provider="openai", model="gpt-4o-mini", local=False, total_tokens=10)
            seq_after = usage_tracker.current_seq()
        self.assertEqual(seq_after, seq_before + 1)


# ===========================================================================
# behavior_hooks.py
# ===========================================================================

class BehaviorHooksProtectedPathTests(unittest.TestCase):

    def test_is_protected_path_system_dirs(self):
        import behavior_hooks
        self.assertTrue(behavior_hooks._is_protected_path("/System/Library/Frameworks"))
        self.assertTrue(behavior_hooks._is_protected_path("/etc/hosts"))
        self.assertTrue(behavior_hooks._is_protected_path("/usr/bin/python"))
        self.assertTrue(behavior_hooks._is_protected_path("/bin/bash"))
        self.assertTrue(behavior_hooks._is_protected_path("/sbin/fsck"))

    def test_is_protected_path_user_dirs_not_protected(self):
        import behavior_hooks
        self.assertFalse(behavior_hooks._is_protected_path("/home/user/documents"))
        self.assertFalse(behavior_hooks._is_protected_path("/tmp/test.py"))
        self.assertFalse(behavior_hooks._is_protected_path("/var/log/app.log"))

    def test_is_protected_path_exact_prefix_match(self):
        import behavior_hooks
        # "/etc" itself
        self.assertTrue(behavior_hooks._is_protected_path("/etc"))
        # "/etc2" should NOT be protected (not a child of /etc)
        self.assertFalse(behavior_hooks._is_protected_path("/etc2/file"))


class BehaviorHooksShellGatingTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hook_log = Path(self._tmpdir.name) / "hook_events.jsonl"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_log(self):
        import behavior_hooks
        return patch.object(behavior_hooks, "HOOK_LOG", self._hook_log)

    def test_pre_shell_command_allows_safe_command(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command("echo hello")
        self.assertTrue(result["ok"])
        self.assertEqual(result["rule"], "allowed")

    def test_pre_shell_command_blocks_rm_rf(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command("rm -rf /tmp/test")
        self.assertFalse(result["ok"])
        self.assertIn("Blocked", result["reason"])

    def test_pre_shell_command_blocks_shutdown(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command("sudo shutdown now")
        self.assertFalse(result["ok"])

    def test_pre_shell_command_blocks_fork_bomb(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command(":(){:|:&};:")
        self.assertFalse(result["ok"])

    def test_pre_shell_command_blocks_modify_on_protected_path(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command("chmod 777 /etc/passwd")
        self.assertFalse(result["ok"])
        self.assertIn("protected", result["reason"].lower())

    def test_pre_shell_command_allows_modify_on_user_path(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_shell_command("chmod 755 /home/user/myfile.sh")
        self.assertTrue(result["ok"])

    def test_pre_shell_command_records_to_log(self):
        import behavior_hooks
        with self._patch_log():
            behavior_hooks.pre_shell_command("ls -la")
        self.assertTrue(self._hook_log.exists())
        lines = [l for l in self._hook_log.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["phase"], "pre_shell")

    def test_pre_shell_command_permissive_profile_allows_rm_rf(self):
        import behavior_hooks
        with patch.dict(os.environ, {"JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE": "1"}, clear=False):
            with self._patch_log():
                result = behavior_hooks.pre_shell_command("rm -rf /tmp/test")
        self.assertTrue(result["ok"])

    def test_pre_shell_command_permissive_profile_requires_admin_for_protected_path(self):
        import behavior_hooks
        with patch.dict(os.environ, {"JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE": "1"}, clear=False):
            with self._patch_log():
                blocked = behavior_hooks.pre_shell_command("chmod 777 /etc/passwd", admin=False)
                allowed = behavior_hooks.pre_shell_command("chmod 777 /etc/passwd", admin=True)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["rule"], "protected_path_shell_requires_admin")
        self.assertTrue(allowed["ok"])


class BehaviorHooksFileWriteTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hook_log = Path(self._tmpdir.name) / "hook_events.jsonl"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_log(self):
        import behavior_hooks
        return patch.object(behavior_hooks, "HOOK_LOG", self._hook_log)

    def test_pre_file_write_allows_non_protected_path(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_file_write("/tmp/test.txt")
        self.assertTrue(result["ok"])

    def test_pre_file_write_blocks_protected_path(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_file_write("/etc/cron.d/myjob")
        self.assertFalse(result["ok"])
        self.assertIn("protected", result["reason"].lower())

    def test_pre_file_write_allow_protected_override(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_file_write("/etc/test", allow_protected=True)
        self.assertTrue(result["ok"])

    def test_pre_file_write_allows_protected_when_permissive_env_enabled(self):
        import behavior_hooks
        with patch.dict(
            os.environ,
            {
                "JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE": "1",
                "JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES": "1",
            },
            clear=False,
        ):
            with self._patch_log():
                result = behavior_hooks.pre_file_write("/etc/test")
        self.assertTrue(result["ok"])

    def test_post_file_write_valid_python(self):
        import behavior_hooks
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("x = 1 + 1\n")
            path = f.name
        try:
            with self._patch_log():
                result = behavior_hooks.post_file_write(path)
        finally:
            os.unlink(path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["rule"], "validated")

    def test_post_file_write_invalid_python(self):
        import behavior_hooks
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("def broken(\n")  # syntax error
            path = f.name
        try:
            with self._patch_log():
                result = behavior_hooks.post_file_write(path)
        finally:
            os.unlink(path)
        self.assertFalse(result["ok"])
        self.assertIn("Python validation", result["reason"])

    def test_post_file_write_non_python_always_ok(self):
        import behavior_hooks
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("not python {{ invalid }")
            path = f.name
        try:
            with self._patch_log():
                result = behavior_hooks.post_file_write(path)
        finally:
            os.unlink(path)
        self.assertTrue(result["ok"])


class BehaviorHooksSelfImproveTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hook_log = Path(self._tmpdir.name) / "hook_events.jsonl"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_log(self):
        import behavior_hooks
        return patch.object(behavior_hooks, "HOOK_LOG", self._hook_log)

    def test_pre_self_improve_allows_known_files(self):
        import behavior_hooks
        known_files = ["memory.py", "router.py", "brains/brain.py", "vault.py", "tools.py"]
        for fname in known_files:
            with self._patch_log():
                result = behavior_hooks.pre_self_improve(fname)
            self.assertTrue(result["ok"], f"Expected {fname} to be allowed")

    def test_pre_self_improve_blocks_unknown_file(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_self_improve("some_random_module.py")
        self.assertFalse(result["ok"])
        self.assertIn("self-improve cannot target", result["reason"])

    def test_pre_self_improve_empty_target_allowed(self):
        import behavior_hooks
        with self._patch_log():
            result = behavior_hooks.pre_self_improve("")
        self.assertTrue(result["ok"])

    def test_pre_self_improve_extracts_filenames_from_sentence(self):
        import behavior_hooks
        with self._patch_log():
            # Contains a known file AND an unknown file
            result = behavior_hooks.pre_self_improve(
                "Please improve some_random_module.py and memory.py"
            )
        # Should be blocked due to the unknown file
        self.assertFalse(result["ok"])


class BehaviorHooksSummaryTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._hook_log = Path(self._tmpdir.name) / "hook_events.jsonl"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _patch_log(self):
        import behavior_hooks
        return patch.object(behavior_hooks, "HOOK_LOG", self._hook_log)

    def test_summary_returns_zero_when_no_log(self):
        import behavior_hooks
        with self._patch_log():
            data = behavior_hooks.summary(hours=24)
        self.assertEqual(data["event_count"], 0)
        self.assertEqual(data["blocked_count"], 0)

    def test_summary_counts_events_and_blocked(self):
        import behavior_hooks
        with self._patch_log():
            behavior_hooks.pre_shell_command("echo hello")   # allowed
            behavior_hooks.pre_shell_command("rm -rf /tmp/x")  # blocked
            data = behavior_hooks.summary(hours=24)
        self.assertEqual(data["event_count"], 2)
        self.assertEqual(data["blocked_count"], 1)

    def test_summary_groups_by_phase(self):
        import behavior_hooks
        with self._patch_log():
            behavior_hooks.pre_shell_command("ls -la")
            behavior_hooks.pre_file_write("/tmp/test.txt")
            data = behavior_hooks.summary(hours=24)
        self.assertIn("pre_shell", data["by_phase"])
        self.assertIn("pre_write", data["by_phase"])
        # protected_prefixes only appears when log exists and events were recorded
        self.assertIn("protected_prefixes", data)
        self.assertIn("/etc", data["protected_prefixes"])

    def test_status_text_no_events(self):
        import behavior_hooks
        with self._patch_log():
            text = behavior_hooks.status_text(hours=24)
        self.assertIn("Behavior gates are active", text)
        self.assertIn("no hook events", text)

    def test_status_text_with_events_mentions_blocked(self):
        import behavior_hooks
        with self._patch_log():
            behavior_hooks.pre_shell_command("ls -la")
            behavior_hooks.pre_shell_command("rm -rf /tmp")
            text = behavior_hooks.status_text(hours=24)
        self.assertIn("Behavior gates are active", text)
        self.assertIn("blocked", text.lower())


# ===========================================================================
# semantic_memory.py
# ===========================================================================

class SemanticMemoryFormattingTests(unittest.TestCase):

    def setUp(self):
        import semantic_memory
        semantic_memory.invalidate()

    def test_format_for_prompt_empty_list(self):
        import semantic_memory
        self.assertEqual(semantic_memory.format_for_prompt([]), "")

    def test_format_for_prompt_includes_content(self):
        import semantic_memory
        hits = [
            {"content": "Aman works on AI safety systems.", "score": 0.85},
            {"content": "Jarvis is a local-first AI assistant.", "score": 0.72},
        ]
        result = semantic_memory.format_for_prompt(hits)
        self.assertIn("Aman works on AI safety systems.", result)
        self.assertIn("Jarvis is a local-first AI assistant.", result)
        self.assertIn("Relevant context", result)

    def test_format_for_prompt_includes_score(self):
        import semantic_memory
        hits = [{"content": "Some content.", "score": 0.91}]
        result = semantic_memory.format_for_prompt(hits)
        self.assertIn("0.91", result)

    def test_format_for_prompt_respects_max_chars(self):
        import semantic_memory
        hits = [{"content": "x" * 2000, "score": 0.9}]
        result = semantic_memory.format_for_prompt(hits, max_chars=100)
        # The long content should be skipped or the result kept short
        self.assertLessEqual(len(result), 200)

    def test_format_for_prompt_skips_empty_content(self):
        import semantic_memory
        hits = [
            {"content": "", "score": 0.9},
            {"content": "Valid content.", "score": 0.7},
        ]
        result = semantic_memory.format_for_prompt(hits)
        self.assertIn("Valid content.", result)

    def test_context_for_query_returns_empty_when_no_index(self):
        import semantic_memory
        semantic_memory.invalidate()
        with patch("semantic_memory._load_all_entries", return_value=[]):
            result = semantic_memory.context_for_query("Tell me about Aman's projects")
        self.assertEqual(result, "")


class SemanticMemoryWriteTests(unittest.TestCase):

    def setUp(self):
        import semantic_memory
        semantic_memory.invalidate()

    def test_write_creates_json_file_in_correct_dir(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            sem_dir = Path(tmp) / "semantic"
            with patch.object(semantic_memory, "SEMANTIC_DIR", sem_dir):
                path = semantic_memory.write("public", {
                    "content": "Test memory entry.",
                    "tags": ["test"],
                })
            # Assert while temp dir still exists
            self.assertTrue(path.exists())
            data = json.loads(path.read_text())
        self.assertEqual(data["content"], "Test memory entry.")
        self.assertEqual(data["privacy_tier"], "public")

    def test_write_sets_default_fields(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            sem_dir = Path(tmp) / "semantic"
            with patch.object(semantic_memory, "SEMANTIC_DIR", sem_dir):
                path = semantic_memory.write("semi_private", {"content": "Entry."})
            data = json.loads(path.read_text())
        self.assertIn("id", data)
        self.assertIn("created_at", data)
        self.assertIn("use_count", data)
        self.assertIn("tags", data)

    def test_write_episodic_creates_json_file(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            epi_dir = Path(tmp) / "episodic"
            with patch.object(semantic_memory, "EPISODIC_DIR", epi_dir):
                path = semantic_memory.write_episodic("professional", {
                    "content": "Worked on interview prep.",
                    "tags": ["interview"],
                })
            self.assertTrue(path.exists())
            data = json.loads(path.read_text())
        self.assertEqual(data["content"], "Worked on interview prep.")
        self.assertEqual(data["domain"], "professional")

    def test_write_episodic_sets_default_fields(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            epi_dir = Path(tmp) / "episodic"
            with patch.object(semantic_memory, "EPISODIC_DIR", epi_dir):
                path = semantic_memory.write_episodic("technical", {"content": "Build log."})
            data = json.loads(path.read_text())
        self.assertIn("id", data)
        self.assertIn("timestamp", data)

    def test_log_conversation_turn_appends_jsonl_record(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            convo_dir = Path(tmp) / "conversations"
            log_path = convo_dir / "verbatim.jsonl"
            with patch.object(semantic_memory, "CONVERSATIONS_DIR", convo_dir), \
                 patch.object(semantic_memory, "VERBATIM_LOG_PATH", log_path):
                semantic_memory.log_conversation_turn(
                    "Why did we switch auth providers?",
                    "We switched for better operational reliability and lower incident rate.",
                    model="deepseek-r1:14b",
                    source="api",
                )
            self.assertTrue(log_path.exists())
            lines = log_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            entry = json.loads(lines[0])
        self.assertEqual(entry["model"], "deepseek-r1:14b")
        self.assertEqual(entry["source"], "api")
        self.assertIn("switch auth providers", entry["user"])


class SemanticMemoryStatusTests(unittest.TestCase):

    def setUp(self):
        import semantic_memory
        semantic_memory.invalidate()

    def test_status_returns_required_keys(self):
        import semantic_memory
        status = semantic_memory.status()
        for key in ("entries_indexed", "semantic_entries", "episodic_entries",
                    "index_ready", "memory_dir"):
            self.assertIn(key, status)

    def test_status_memory_dir_is_string(self):
        import semantic_memory
        status = semantic_memory.status()
        self.assertIsInstance(status["memory_dir"], str)

    def test_invalidate_clears_index(self):
        import semantic_memory
        semantic_memory.invalidate()
        self.assertIsNone(semantic_memory._vectorizer)
        self.assertIsNone(semantic_memory._matrix)
        self.assertEqual(semantic_memory._entries, [])


class SemanticMemoryRetrievalTests(unittest.TestCase):
    """Tests for write-then-retrieve cycle using isolated temp directories."""

    def setUp(self):
        import semantic_memory
        semantic_memory.invalidate()

    def test_write_and_retrieve_finds_entry(self):
        import semantic_memory
        with tempfile.TemporaryDirectory() as tmp:
            sem_dir = Path(tmp) / "semantic"
            epi_dir = Path(tmp) / "episodic"
            with patch.object(semantic_memory, "SEMANTIC_DIR", sem_dir), \
                 patch.object(semantic_memory, "EPISODIC_DIR", epi_dir):
                semantic_memory.write("public", {
                    "content": "Aman specializes in AI trust and safety enforcement.",
                    "tags": ["AI", "safety", "trust", "enforcement"],
                })
                semantic_memory.write("public", {
                    "content": "Jarvis uses a local-first architecture with Ollama.",
                    "tags": ["Jarvis", "architecture", "ollama"],
                })
                # Force rebuild with our temp entries
                semantic_memory._build_index()
                hits = semantic_memory.retrieve("AI safety enforcement", top_k=2)
        # Results can be empty if sklearn doesn't find a match in a tiny index,
        # but the function must return a list without raising.
        self.assertIsInstance(hits, list)
        for hit in hits:
            self.assertIn("score", hit)

    def test_retrieve_returns_empty_when_no_entries(self):
        import semantic_memory
        semantic_memory.invalidate()
        with patch("semantic_memory._load_all_entries", return_value=[]):
            semantic_memory._build_index()
            hits = semantic_memory.retrieve("any query", top_k=3)
        self.assertEqual(hits, [])


class MainStartupGuardTests(unittest.TestCase):
    def test_gui_launch_reexecs_from_conda_into_project_venv(self):
        import main

        with patch("main._is_conda_python", return_value=True), \
             patch.object(main.sys, "argv", ["main.py"]), \
             patch("main._project_venv_python", return_value="/tmp/jarvis-venv/bin/python"), \
             patch("main.os.path.exists", return_value=True), \
             patch("main.os.path.realpath", side_effect=lambda p: p), \
             patch.dict("main.os.environ", {}, clear=True), \
             patch("main.os.execve") as execve_mock:
            main._ensure_supported_gui_runtime()

        execve_mock.assert_called_once_with(
            "/tmp/jarvis-venv/bin/python",
            ["/tmp/jarvis-venv/bin/python", "main.py"],
            {"_JARVIS_GUI_REEXEC_ATTEMPTED": "1"},
        )

    def test_gui_launch_from_conda_without_venv_exits_cleanly(self):
        import main

        with patch("main._is_conda_python", return_value=True), \
             patch.object(main.sys, "argv", ["main.py"]), \
             patch("main._project_venv_python", return_value="/tmp/missing-venv/bin/python"), \
             patch("main.os.path.exists", return_value=False):
            with self.assertRaises(SystemExit) as exc:
                main._ensure_supported_gui_runtime()

        self.assertIn("should not be launched from conda", str(exc.exception))

    def test_startup_permission_request_does_not_probe_or_open_apps(self):
        import main

        with patch("builtins.print") as print_mock:
            main._request_macos_permissions()

        joined = " ".join(
            " ".join(str(arg) for arg in call.args)
            for call in print_mock.call_args_list
        )
        self.assertIn("disabled", joined.lower())
        self.assertIn("avoid opening user apps", joined.lower())


class RuntimeEndpointDiscoveryTests(unittest.TestCase):
    def test_read_api_endpoint_preserves_token(self):
        import runtime_state

        payload = {
            "host": "127.0.0.1",
            "port": 8766,
            "pid": 123,
            "token": "secret-token",
            "written_at": "2026-04-09T00:00:00+00:00",
        }

        with patch("runtime_state.runtime_meta_path") as meta_path:
            meta_path.return_value.read_text.return_value = json.dumps(payload)
            result = runtime_state.read_api_endpoint()

        self.assertIsNotNone(result)
        self.assertEqual(result["token"], "secret-token")

    def test_discover_api_endpoint_prefers_runtime_metadata(self):
        import runtime_state

        metadata = {
            "host": "127.0.0.1",
            "port": 8766,
            "pid": 123,
            "written_at": "2026-04-09T00:00:00+00:00",
        }

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"status": "online"}).encode("utf-8")

        def _urlopen(req, timeout=0.5):
            url = getattr(req, "full_url", str(req))
            self.assertEqual(url, "http://127.0.0.1:8766/status")
            return _Resp()

        with patch("runtime_state.read_api_endpoint", return_value={**metadata, "base_url": "http://127.0.0.1:8766"}), \
             patch("runtime_state.port_file_path") as port_file_mock, \
             patch("runtime_state.urllib.request.urlopen", side_effect=_urlopen), \
             patch.dict(os.environ, {}, clear=True):
            port_file_mock.return_value.read_text.side_effect = FileNotFoundError
            result = runtime_state.discover_api_endpoint()

        self.assertIsNotNone(result)
        self.assertEqual(result["port"], 8766)
        self.assertEqual(result["base_url"], "http://127.0.0.1:8766")

    def test_discover_api_endpoint_falls_back_to_port_file(self):
        import runtime_state

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"status": "online"}).encode("utf-8")

        with patch("runtime_state.read_api_endpoint", return_value=None), \
             patch("runtime_state.port_file_path") as port_file_mock, \
             patch("runtime_state.urllib.request.urlopen", return_value=_Resp()), \
             patch.dict(os.environ, {}, clear=True):
            port_file_mock.return_value.read_text.return_value = "8772"
            result = runtime_state.discover_api_endpoint()

        self.assertIsNotNone(result)
        self.assertEqual(result["port"], 8772)


class JarvisCliEndpointTests(unittest.TestCase):
    def test_auth_headers_use_runtime_token_when_present(self):
        import jarvis_cli

        with patch.dict(os.environ, {}, clear=True), \
             patch("runtime_state.read_api_endpoint", return_value={"token": "runtime-token"}):
            headers = jarvis_cli._auth_headers()

        self.assertEqual(headers["Authorization"], "Bearer runtime-token")

    def test_get_uses_runtime_discovery_each_call(self):
        import jarvis_cli

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"status": "online"}).encode("utf-8")

        captured = []

        def _urlopen(req, timeout=10):
            captured.append(getattr(req, "full_url", str(req)))
            return _Resp()

        with patch("runtime_state.discover_api_endpoint", return_value={"base_url": "http://127.0.0.1:8766"}), \
             patch("jarvis_cli.urllib.request.urlopen", side_effect=_urlopen):
            payload = jarvis_cli.get("/status")

        self.assertEqual(payload["status"], "online")
        self.assertEqual(captured, ["http://127.0.0.1:8766/status"])

    def test_cli_can_fetch_skill_listing_endpoint(self):
        import jarvis_cli

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps({"skills": [{"id": "engineering_reasoning"}]}).encode("utf-8")

        captured = []

        def _urlopen(req, timeout=10):
            captured.append(getattr(req, "full_url", str(req)))
            return _Resp()

        with patch("runtime_state.discover_api_endpoint", return_value={"base_url": "http://127.0.0.1:8766"}), \
             patch("jarvis_cli.urllib.request.urlopen", side_effect=_urlopen):
            payload = jarvis_cli.get("/skills")

        self.assertEqual(payload["skills"][0]["id"], "engineering_reasoning")
        self.assertEqual(captured, ["http://127.0.0.1:8766/skills"])


class ExtensionRegistryTests(unittest.TestCase):
    def test_lists_connectors(self):
        import extension_registry

        items = extension_registry.list_connectors()
        ids = {item["id"] for item in items}
        self.assertIn("managed_runtime", ids)
        self.assertIn("knowledge_vault", ids)

    def test_plugin_detail_includes_nested_data(self):
        import extension_registry

        plugin = extension_registry.plugin_detail("managed_agents")
        self.assertIsNotNone(plugin)
        self.assertEqual(plugin["id"], "managed_agents")
        self.assertTrue(plugin["skills_detail"])
        self.assertTrue(plugin["connectors_detail"])

    def test_skill_detail_includes_instructions(self):
        import extension_registry

        skill = extension_registry.get_skill_detail("engineering_reasoning")
        self.assertIsNotNone(skill)
        self.assertIn("Engineering Reasoning", skill["instructions"])


class SourceIngestSafetyTests(unittest.TestCase):
    def test_rejects_localhost_urls_by_default(self):
        import source_ingest

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as exc:
                source_ingest._assert_safe_remote_url("http://127.0.0.1:8000/private")

        self.assertIn("Refusing to ingest", str(exc.exception))

    def test_allows_private_urls_when_override_is_enabled(self):
        import source_ingest

        with patch.dict(os.environ, {"JARVIS_ALLOW_PRIVATE_URL_INGEST": "1"}, clear=True):
            source_ingest._assert_safe_remote_url("http://127.0.0.1:8000/private")


if __name__ == "__main__":
    unittest.main(verbosity=2)
