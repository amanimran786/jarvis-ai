"""
local_cocoindex.py — Incremental vault indexer for Jarvis.

Maintains a live semantic index over vault/wiki/brain/ markdown files using CocoIndex
(if available) or a pure-Python fallback. Only re-processes changed files on refresh.

CocoIndex (Rust-core incremental pipeline library) is preferred but optional.
Fallback: local embeddings via Ollama HTTP API + numpy cosine similarity search.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.error
import urllib.request
from typing import Any, Optional
import hashlib
import re

# Optional imports with graceful fallback
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import cocoindex
    HAS_COCOINDEX = True
except ImportError:
    HAS_COCOINDEX = False

VAULT_PATH = os.path.expanduser("~/jarvis-ai/vault")
OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_TIMEOUT = 30
EMBEDDING_MODEL = "nomic-embed-text"
CHUNK_MAX_CHARS = 500


class VaultIndexer:
    """
    Abstract base for vault indexing. Subclasses implement CocoIndex or fallback.
    """

    def refresh(self) -> dict[str, Any]:
        """
        Run incremental index update. Return stats:
        {
            "files_processed": int,
            "chunks_indexed": int,
            "duration_s": float,
        }
        """
        raise NotImplementedError

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Semantic search. Return list of dicts:
        [
            {"file": str, "chunk": str, "score": float},
            ...
        ]
        """
        raise NotImplementedError

    def is_available(self) -> bool:
        """True if indexer is configured and ready."""
        raise NotImplementedError


class _SimpleVaultIndex(VaultIndexer):
    """
    Pure-Python fallback: file mtime tracking + Ollama embeddings + cosine search.
    """

    def __init__(self, vault_path: str):
        self.vault_path = pathlib.Path(vault_path)
        self.brain_path = self.vault_path / "wiki" / "brain"
        self.index_meta_path = pathlib.Path.home() / ".jarvis_vault_index.json"
        self.embeddings_path = pathlib.Path.home() / ".jarvis_vault_embeddings.npz"
        self._available = HAS_NUMPY and self._ollama_available()
        self._chunks = []  # list of {"file": str, "chunk": str}
        self._embeddings = None  # numpy array, shape (N, embedding_dim)
        self._metadata = {}  # {filepath: {"mtime": float, "hash": str}}
        self._load_index()

    def is_available(self) -> bool:
        return self._available

    def _ollama_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            req = urllib.request.Request(f"{OLLAMA_BASE_URL}/api/tags")
            with urllib.request.urlopen(req, timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

    def _load_index(self) -> None:
        """Load metadata and embeddings from disk."""
        if self.index_meta_path.exists():
            try:
                with open(self.index_meta_path, "r") as f:
                    data = json.load(f)
                self._metadata = data.get("metadata", {})
                self._chunks = data.get("chunks", [])
            except Exception:
                pass

        if self._embeddings is None and self.embeddings_path.exists() and HAS_NUMPY:
            try:
                with np.load(self.embeddings_path) as npz:
                    self._embeddings = npz["embeddings"]
            except Exception:
                pass

    def _save_index(self) -> None:
        """Save metadata and embeddings to disk."""
        meta_data = {"metadata": self._metadata, "chunks": self._chunks}
        try:
            with open(self.index_meta_path, "w") as f:
                json.dump(meta_data, f, indent=2)
        except Exception:
            pass

        if self._embeddings is not None and HAS_NUMPY:
            try:
                np.savez(self.embeddings_path, embeddings=self._embeddings)
            except Exception:
                pass

    def _chunk_markdown(self, text: str) -> list[str]:
        """
        Split markdown by H2/H3 headers (^## and ^###).
        Each chunk is max CHUNK_MAX_CHARS.
        """
        chunks = []
        lines = text.split("\n")
        current = []
        current_size = 0

        for line in lines:
            line_size = len(line) + 1  # +1 for newline
            # If we hit a header and have accumulated text, flush current
            if (line.startswith("##") or line.startswith("###")) and current:
                chunk_text = "\n".join(current).strip()
                if chunk_text:
                    chunks.append(chunk_text)
                current = [line]
                current_size = line_size
            else:
                # Add line; flush if would exceed limit
                if current_size + line_size > CHUNK_MAX_CHARS and current:
                    chunk_text = "\n".join(current).strip()
                    if chunk_text:
                        chunks.append(chunk_text)
                    current = [line]
                    current_size = line_size
                else:
                    current.append(line)
                    current_size += line_size

        # Flush remainder
        if current:
            chunk_text = "\n".join(current).strip()
            if chunk_text:
                chunks.append(chunk_text)

        return chunks

    def _file_hash(self, filepath: pathlib.Path) -> str:
        """Return SHA256 of file contents."""
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
        except Exception:
            return ""

    def _embed_text(self, text: str) -> Optional[list[float]]:
        """Call Ollama to embed text. Return list of floats or None."""
        try:
            payload = {"model": EMBEDDING_MODEL, "prompt": text}
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{OLLAMA_BASE_URL}/api/embeddings",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
                return result.get("embedding", None)
        except Exception:
            return None

    def refresh(self) -> dict[str, Any]:
        """
        Scan brain_path for changed .md files. Re-embed only changed ones.
        Return {files_processed, chunks_indexed, duration_s}.
        """
        start_time = time.time()
        if not self.brain_path.exists():
            return {
                "files_processed": 0,
                "chunks_indexed": 0,
                "duration_s": time.time() - start_time,
            }

        files_processed = 0
        new_chunks = []
        new_embeddings = []

        for md_file in sorted(self.brain_path.glob("**/*.md")):
            filepath_str = str(md_file)
            try:
                stat = md_file.stat()
                mtime = stat.st_mtime
                file_hash = self._file_hash(md_file)

                # Check if file changed
                meta = self._metadata.get(filepath_str, {})
                if meta.get("mtime") == mtime and meta.get("hash") == file_hash:
                    # File unchanged; keep old chunks + embeddings
                    for i, chunk_info in enumerate(self._chunks):
                        if chunk_info.get("file") == filepath_str:
                            if self._embeddings is not None and i < len(self._embeddings):
                                new_embeddings.append(self._embeddings[i])
                            new_chunks.append(chunk_info)
                    continue

                # File changed; re-chunk and re-embed
                with open(md_file, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()

                chunks = self._chunk_markdown(text)
                for chunk in chunks:
                    embedding = self._embed_text(chunk)
                    if embedding:
                        new_chunks.append({"file": filepath_str, "chunk": chunk})
                        new_embeddings.append(embedding)

                self._metadata[filepath_str] = {"mtime": mtime, "hash": file_hash}
                files_processed += 1

            except Exception:
                continue

        # Update in-memory state
        self._chunks = new_chunks
        if HAS_NUMPY and new_embeddings:
            self._embeddings = np.array(new_embeddings, dtype=np.float32)
        else:
            self._embeddings = None

        self._save_index()

        duration = time.time() - start_time
        return {
            "files_processed": files_processed,
            "chunks_indexed": len(self._chunks),
            "duration_s": duration,
        }

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Semantic search via cosine similarity.
        Return top_k results: [{file, chunk, score}, ...].
        """
        if not self._chunks or self._embeddings is None or not HAS_NUMPY:
            return []

        query_embedding = self._embed_text(query)
        if query_embedding is None:
            return []

        query_vec = np.array(query_embedding, dtype=np.float32)

        # Cosine similarity: (A·B) / (||A|| ||B||)
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms = np.maximum(norms, 1e-8)  # Avoid division by zero
        normalized = self._embeddings / norms

        query_norm = np.linalg.norm(query_vec)
        if query_norm < 1e-8:
            return []
        query_normalized = query_vec / query_norm

        scores = np.dot(normalized, query_normalized)
        top_indices = np.argsort(-scores)[:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0:
                results.append(
                    {
                        "file": self._chunks[idx]["file"],
                        "chunk": self._chunks[idx]["chunk"],
                        "score": float(scores[idx]),
                    }
                )

        return results


class _CocoIndexAdapter(VaultIndexer):
    """
    Adapter for CocoIndex (when available and configured).
    Placeholder; would integrate CocoIndex flow here.
    """

    def __init__(self, vault_path: str):
        self.vault_path = pathlib.Path(vault_path)
        self.brain_path = self.vault_path / "wiki" / "brain"
        self._available = HAS_COCOINDEX

    def is_available(self) -> bool:
        return self._available

    def refresh(self) -> dict[str, Any]:
        """Placeholder: would call cocoindex.flow.run()."""
        return {
            "files_processed": 0,
            "chunks_indexed": 0,
            "duration_s": 0.0,
        }

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Placeholder."""
        return []


def get_indexer(vault_path: str = VAULT_PATH) -> VaultIndexer:
    """
    Factory: returns appropriate indexer.
    - If CocoIndex + postgres available: _CocoIndexAdapter
    - Otherwise: _SimpleVaultIndex (fallback)
    """
    if HAS_COCOINDEX:
        adapter = _CocoIndexAdapter(vault_path)
        if adapter.is_available():
            return adapter

    return _SimpleVaultIndex(vault_path)
