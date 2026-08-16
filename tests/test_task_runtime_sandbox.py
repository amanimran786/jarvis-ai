"""Seatbelt sandbox envelope for agent-executed bash commands."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import task_runtime


class SandboxProfileTests(unittest.TestCase):
    def test_profile_denies_network_and_writes_by_default(self):
        profile = task_runtime._sandbox_profile([])
        self.assertIn("(deny network*)", profile)
        self.assertIn("(deny file-write*)", profile)
        self.assertIn('(subpath "/private/tmp")', profile)
        self.assertIn('(subpath "/private/var/folders")', profile)

    def test_profile_allows_write_root_subpath(self):
        profile = task_runtime._sandbox_profile([Path("/private/tmp")])
        self.assertIn('(allow file-write* ', profile)
        self.assertIn('(subpath "/private/tmp")', profile)

    def test_profile_escapes_quotes_in_paths(self):
        profile = task_runtime._sandbox_profile([Path('/tmp/we"ird')])
        self.assertNotIn('"we"ird"', profile)


class SandboxWrapTests(unittest.TestCase):
    def test_wrap_prepends_sandbox_exec_on_darwin(self):
        with patch.object(task_runtime, "_sandbox_available", return_value=True):
            wrapped = task_runtime._sandbox_wrap(["ls", "-1"], [])
        self.assertEqual(wrapped[0], task_runtime._SANDBOX_EXEC)
        self.assertEqual(wrapped[1], "-p")
        self.assertEqual(wrapped[3:], ["ls", "-1"])

    def test_wrap_is_noop_when_unavailable(self):
        with patch.object(task_runtime, "_sandbox_available", return_value=False):
            self.assertEqual(task_runtime._sandbox_wrap(["ls"], []), ["ls"])

    def test_disable_env_var_bypasses_sandbox(self):
        with patch.dict(os.environ, {"JARVIS_DISABLE_SANDBOX": "1"}):
            self.assertFalse(task_runtime._sandbox_available())


@unittest.skipUnless(
    sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"),
    "requires macOS sandbox-exec",
)
@unittest.skipIf(
    os.getenv("JARVIS_VERIFIER_SANDBOX") == "1",
    "Seatbelt cannot be nested inside the verifier sandbox",
)
class SandboxLiveEnforcementTests(unittest.TestCase):
    def test_repo_root_commands_are_kernel_read_only(self):
        # cat is allowlisted and sandboxed; reads succeed under the profile
        out = task_runtime._tool_bash("cat README.md | head -1")
        self.assertNotIn("[bash error", out)
        self.assertNotIn("Operation not permitted", out)

    def test_worktree_root_added_to_write_roots(self):
        captured = {}
        real_wrap = task_runtime._sandbox_wrap

        def spy(argv, write_roots):
            captured["roots"] = list(write_roots)
            return real_wrap(argv, write_roots)

        with patch.object(task_runtime, "_sandbox_wrap", side_effect=spy):
            task_runtime._tool_bash("ls", root=Path("/private/tmp"))
        self.assertEqual(captured["roots"], [Path("/private/tmp")])

        with patch.object(task_runtime, "_sandbox_wrap", side_effect=spy):
            task_runtime._tool_bash("ls")
        self.assertEqual(captured["roots"], [])


if __name__ == "__main__":
    unittest.main()
