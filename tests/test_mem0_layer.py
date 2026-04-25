import unittest
from unittest.mock import patch

import mem0_layer


class Mem0LayerTests(unittest.TestCase):
    def tearDown(self):
        mem0_layer._last_error = ""

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


if __name__ == "__main__":
    unittest.main()
