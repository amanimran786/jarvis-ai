import importlib
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class VaultRuntimePathTests(unittest.TestCase):
    def test_frozen_app_uses_writable_vault_copy(self):
        previous_data_dir = os.environ.get("JARVIS_DATA_DIR")
        had_frozen = hasattr(sys, "frozen")
        previous_frozen = getattr(sys, "frozen", None)
        previous_vault = sys.modules.pop("vault", None)

        try:
            with TemporaryDirectory() as tmp:
                os.environ["JARVIS_DATA_DIR"] = tmp
                sys.frozen = True

                vault = importlib.import_module("vault")

                self.assertEqual(vault.VAULT_ROOT, Path(tmp) / "vault")
                self.assertTrue(vault.VAULT_ROOT.exists())
                self.assertTrue(str(vault.INDEX_FILE).startswith(tmp))
        finally:
            sys.modules.pop("vault", None)
            if previous_vault is not None:
                sys.modules["vault"] = previous_vault
            if previous_data_dir is None:
                os.environ.pop("JARVIS_DATA_DIR", None)
            else:
                os.environ["JARVIS_DATA_DIR"] = previous_data_dir
            if had_frozen:
                sys.frozen = previous_frozen
            elif hasattr(sys, "frozen"):
                delattr(sys, "frozen")


if __name__ == "__main__":
    unittest.main()
