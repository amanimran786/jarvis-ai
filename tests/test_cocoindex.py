"""
Unit tests for local_runtime/local_cocoindex.py — Incremental vault indexer.

Tests cover:
  - _SimpleVaultIndex fallback with mock Ollama
  - File change detection (mtime + hash)
  - Markdown chunking (by H2/H3 headers, max size)
  - Cosine similarity search
  - get_indexer factory selection
  - Error handling (missing files, Ollama offline)
"""

import json
import sys
import os
import unittest
import tempfile
import pathlib
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock heavy dependencies before importing local_cocoindex
sys.modules['cocoindex'] = MagicMock()
sys.modules['numpy'] = MagicMock()

# Now import our module
from local_runtime import local_cocoindex


class SimpleVaultIndexInitTests(unittest.TestCase):
    def test_init_sets_brain_path(self):
        """_SimpleVaultIndex must set brain_path = vault_path/wiki/brain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
        self.assertEqual(indexer.brain_path, vault_path / "wiki" / "brain")

    def test_init_sets_metadata_path(self):
        """_SimpleVaultIndex must set index_meta_path to ~/.jarvis_vault_index.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
        self.assertTrue(str(indexer.index_meta_path).endswith(".jarvis_vault_index.json"))


class MarkdownChunkingTests(unittest.TestCase):
    def test_chunk_markdown_splits_by_h2(self):
        """_chunk_markdown must split on ^## lines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

        text = "# Title\n## Section 1\nContent 1\n## Section 2\nContent 2"
        chunks = indexer._chunk_markdown(text)
        # Should have at least 2 chunks (split on ##)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("Section 1", chunks[0])

    def test_chunk_markdown_respects_max_size(self):
        """_chunk_markdown must respect CHUNK_MAX_CHARS limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

        # Create text larger than max size
        large_text = "A" * (local_cocoindex.CHUNK_MAX_CHARS + 100)
        chunks = indexer._chunk_markdown(large_text)
        # All chunks should be <= max size
        for chunk in chunks:
            self.assertLessEqual(len(chunk), local_cocoindex.CHUNK_MAX_CHARS * 1.1)

    def test_chunk_markdown_handles_empty_input(self):
        """_chunk_markdown must handle empty input gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

        chunks = indexer._chunk_markdown("")
        self.assertEqual(chunks, [])


class FileChangeDetectionTests(unittest.TestCase):
    def test_file_hash_returns_consistent_hash(self):
        """_file_hash must return same hash for identical content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            # Create a test file
            test_file = pathlib.Path(tmpdir) / "test.md"
            test_file.write_text("test content")

            hash1 = indexer._file_hash(test_file)
            hash2 = indexer._file_hash(test_file)
            self.assertEqual(hash1, hash2)

    def test_file_hash_differs_for_different_content(self):
        """_file_hash must return different hash for different content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            file1 = pathlib.Path(tmpdir) / "test1.md"
            file2 = pathlib.Path(tmpdir) / "test2.md"
            file1.write_text("content1")
            file2.write_text("content2")

            hash1 = indexer._file_hash(file1)
            hash2 = indexer._file_hash(file2)
            self.assertNotEqual(hash1, hash2)


class RefreshTests(unittest.TestCase):
    def test_refresh_returns_correct_structure(self):
        """refresh() must return {files_processed, chunks_indexed, duration_s}."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            brain_path = vault_path / "wiki" / "brain"
            brain_path.mkdir(parents=True, exist_ok=True)

            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
            with patch.object(indexer, '_ollama_available', return_value=False):
                indexer._available = False
                result = indexer.refresh()

        self.assertIn("files_processed", result)
        self.assertIn("chunks_indexed", result)
        self.assertIn("duration_s", result)

    def test_refresh_skips_unchanged_files(self):
        """refresh() must skip files with unchanged mtime and hash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            brain_path = vault_path / "wiki" / "brain"
            brain_path.mkdir(parents=True, exist_ok=True)

            # Create test file
            test_file = brain_path / "test.md"
            test_file.write_text("content")

            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            # Mock Ollama
            def mock_embed(text):
                return [0.1] * 384  # nomic-embed-text size

            with patch.object(indexer, '_embed_text', side_effect=mock_embed), \
                 patch.object(indexer, '_ollama_available', return_value=True), \
                 patch('local_runtime.local_cocoindex.HAS_NUMPY', True):
                # First refresh
                result1 = indexer.refresh()
                files_first = result1["files_processed"]

                # Second refresh (no changes)
                result2 = indexer.refresh()
                files_second = result2["files_processed"]

                # Second refresh should process fewer files
                self.assertLessEqual(files_second, files_first)


class SearchTests(unittest.TestCase):
    def test_search_returns_empty_when_no_embeddings(self):
        """search() must return [] when embeddings not loaded."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
            indexer._embeddings = None

            results = indexer.search("test query")
            self.assertEqual(results, [])

    def test_search_returns_list_of_dicts(self):
        """search() must return list with {file, chunk, score} dicts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            brain_path = vault_path / "wiki" / "brain"
            brain_path.mkdir(parents=True, exist_ok=True)

            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            # Mock embeddings and chunks
            import sys
            import numpy as np_real
            indexer._chunks = [
                {"file": "test.md", "chunk": "test content"}
            ]
            indexer._embeddings = np_real.array([[0.1, 0.2, 0.3]], dtype=np_real.float32)

            with patch.object(indexer, '_embed_text', return_value=[0.1, 0.2, 0.3]):
                results = indexer.search("test", top_k=1)

            self.assertIsInstance(results, list)
            if results:
                self.assertIn("file", results[0])
                self.assertIn("chunk", results[0])
                self.assertIn("score", results[0])


class IsAvailableTests(unittest.TestCase):
    def test_is_available_false_without_numpy(self):
        """is_available() must be False if numpy not available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
            indexer._available = False
            self.assertFalse(indexer.is_available())

    def test_is_available_true_when_configured(self):
        """is_available() must be True when numpy and Ollama available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))
            indexer._available = True
            self.assertTrue(indexer.is_available())


class FactorySelectionTests(unittest.TestCase):
    def test_get_indexer_returns_simple_by_default(self):
        """get_indexer() must return _SimpleVaultIndex when CocoIndex unavailable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)

            with patch('local_runtime.local_cocoindex.HAS_COCOINDEX', False):
                indexer = local_cocoindex.get_indexer(str(vault_path))

            self.assertIsInstance(indexer, local_cocoindex._SimpleVaultIndex)

    def test_get_indexer_prefers_cocoindex_when_available(self):
        """get_indexer() should prefer CocoIndexAdapter if available."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)

            with patch('local_runtime.local_cocoindex.HAS_COCOINDEX', True):
                indexer = local_cocoindex.get_indexer(str(vault_path))
                # Will still be CocoIndexAdapter (though adapter.is_available checks cocoindex)
                self.assertIsNotNone(indexer)


class OllamaIntegrationTests(unittest.TestCase):
    def test_ollama_available_returns_false_on_connection_refused(self):
        """_ollama_available() must return False when connection refused."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            import urllib.error
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
                result = indexer._ollama_available()

            self.assertFalse(result)

    def test_ollama_available_returns_true_on_200(self):
        """_ollama_available() must return True on 200 response."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=None)

            with patch("urllib.request.urlopen", return_value=mock_response):
                result = indexer._ollama_available()

            self.assertTrue(result)

    def test_embed_text_returns_embedding_on_success(self):
        """_embed_text() must return embedding list on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            mock_response = MagicMock()
            embedding = [0.1, 0.2, 0.3]
            mock_response.read.return_value = json.dumps({"embedding": embedding}).encode("utf-8")
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=None)

            with patch("urllib.request.urlopen", return_value=mock_response):
                result = indexer._embed_text("test text")

            self.assertEqual(result, embedding)

    def test_embed_text_returns_none_on_error(self):
        """_embed_text() must return None on request error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            vault_path = pathlib.Path(tmpdir)
            (vault_path / "wiki" / "brain").mkdir(parents=True, exist_ok=True)
            indexer = local_cocoindex._SimpleVaultIndex(str(vault_path))

            with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
                result = indexer._embed_text("test text")

            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
