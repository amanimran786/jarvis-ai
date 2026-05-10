import unittest
from unittest.mock import patch
import os

import mem0_layer


class Mem0LayerTests(unittest.TestCase):
    def tearDown(self):
        mem0_layer._last_error = ""
        mem0_layer._memory_instance = None
        mem0_layer._init_attempted = False
        mem0_layer._available = False

    def test_build_config_preserves_local_first_defaults(self):
        config = mem0_layer._build_config()

        self.assertEqual(config["llm"]["provider"], "ollama")
        self.assertEqual(config["embedder"]["provider"], "ollama")
        self.assertEqual(config["embedder"]["config"]["model"], "nomic-embed-text:latest")
        self.assertEqual(config["vector_store"]["provider"], "qdrant")
        self.assertEqual(config["vector_store"]["config"]["collection_name"], "jarvis_nomic_768_v2")
        self.assertEqual(config["vector_store"]["config"]["embedding_model_dims"], 768)
        self.assertIn("path", config["vector_store"]["config"])
        self.assertNotIn("host", config["vector_store"]["config"])
        self.assertNotIn("url", config["vector_store"]["config"])
        self.assertNotIn("api_key", config["vector_store"]["config"])

    def test_collection_name_can_be_overridden_explicitly(self):
        with patch.dict(os.environ, {"JARVIS_MEM0_COLLECTION": "jarvis_nomic_768_hybrid"}):
            config = mem0_layer._build_config()

        self.assertEqual(config["vector_store"]["config"]["collection_name"], "jarvis_nomic_768_hybrid")

    def test_mem0_telemetry_disabled_by_default(self):
        self.assertEqual(os.environ.get("MEM0_TELEMETRY"), "False")

    def test_status_reports_semantic_only_when_fastembed_missing(self):
        with patch("mem0_layer._get_instance", return_value=None), \
             patch("mem0_layer.importlib.util.find_spec", return_value=None):
            status = mem0_layer.status()

        self.assertFalse(status["available"])
        self.assertFalse(status["fastembed_available"])
        self.assertEqual(status["search_mode"], "semantic_only")

    def test_search_uses_filters_for_current_mem0_api(self):
        calls = []

        class _Memory:
            def search(self, query, **kwargs):
                calls.append((query, kwargs))
                return {"results": [{"memory": "stored fact", "score": 0.9}]}

        with patch("mem0_layer._get_instance", return_value=_Memory()):
            results = mem0_layer.search("stored", user_id="aman", top_k=3)

        self.assertEqual(results[0]["memory"], "stored fact")
        self.assertEqual(calls, [("stored", {"top_k": 3, "filters": {"user_id": "aman"}})])

    def test_add_stores_raw_turn_without_llm_inference(self):
        calls = []

        class _Memory:
            def add(self, text, **kwargs):
                calls.append((text, kwargs))
                return {"results": [{"memory": text}]}

        with patch("mem0_layer._get_instance", return_value=_Memory()):
            ok = mem0_layer.add("User: hi\nJarvis: hello", user_id="aman", metadata={"source": "test"})

        self.assertTrue(ok)
        self.assertEqual(
            calls,
            [
                (
                    "User: hi\nJarvis: hello",
                    {"user_id": "aman", "metadata": {"source": "test"}, "infer": False},
                )
            ],
        )

    def test_search_falls_back_to_legacy_mem0_signature(self):
        calls = []

        class _Memory:
            def search(self, query, **kwargs):
                calls.append((query, kwargs))
                if "filters" in kwargs:
                    raise TypeError("legacy signature")
                return [{"memory": "legacy fact", "score": 0.9}]

        with patch("mem0_layer._get_instance", return_value=_Memory()):
            results = mem0_layer.search("legacy", user_id="aman", top_k=2)

        self.assertEqual(results[0]["memory"], "legacy fact")
        self.assertEqual(
            calls,
            [
                ("legacy", {"top_k": 2, "filters": {"user_id": "aman"}}),
                ("legacy", {"user_id": "aman", "limit": 2}),
            ],
        )

    def test_get_all_uses_filters_for_current_mem0_api(self):
        calls = []

        class _Memory:
            def get_all(self, **kwargs):
                calls.append(kwargs)
                return {"results": [{"memory": "stored fact"}]}

        with patch("mem0_layer._get_instance", return_value=_Memory()):
            results = mem0_layer.get_all(user_id="aman")

        self.assertEqual(results[0]["memory"], "stored fact")
        self.assertEqual(calls, [{"filters": {"user_id": "aman"}, "top_k": 1000}])

    def test_close_closes_embedded_qdrant_client_before_shutdown(self):
        calls = []

        class _Client:
            def close(self):
                calls.append("client")

        class _Memory:
            vector_store = type("_VectorStore", (), {"client": _Client()})()

            def close(self):
                calls.append("memory")

        mem0_layer._memory_instance = _Memory()

        mem0_layer.close()

        self.assertEqual(calls, ["client", "memory"])
        self.assertIsNone(mem0_layer._memory_instance)


if __name__ == "__main__":
    unittest.main()
