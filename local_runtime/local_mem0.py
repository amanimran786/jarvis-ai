"""Compatibility helpers for Jarvis's local mem0 runtime."""

from __future__ import annotations

from types import MethodType
from typing import Any


def install_qdrant_search_compat(memory: Any) -> bool:
    """Bridge legacy mem0 Qdrant calls to the current query_points API."""
    vector_store = getattr(memory, "vector_store", None)
    client = getattr(vector_store, "client", None)
    if client is None or callable(getattr(client, "search", None)):
        return False
    if not callable(getattr(client, "query_points", None)):
        return False

    def search(
        self,
        collection_name: str,
        query_vector: Any,
        query_filter: Any = None,
        search_params: Any = None,
        limit: int = 10,
        offset: int | None = None,
        with_payload: Any = True,
        with_vectors: Any = False,
        score_threshold: float | None = None,
        append_payload: bool = True,
        consistency: Any = None,
        shard_key_selector: Any = None,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> list[Any]:
        del append_payload
        query = query_vector
        using = None
        if (
            isinstance(query_vector, tuple)
            and len(query_vector) == 2
            and isinstance(query_vector[0], str)
        ):
            using, query = query_vector
        elif getattr(query_vector, "name", None) is not None:
            using = query_vector.name
            query = query_vector.vector

        response = self.query_points(
            collection_name=collection_name,
            query=query,
            using=using,
            query_filter=query_filter,
            search_params=search_params,
            limit=limit,
            offset=offset,
            with_payload=with_payload,
            with_vectors=with_vectors,
            score_threshold=score_threshold,
            consistency=consistency,
            shard_key_selector=shard_key_selector,
            timeout=timeout,
            **kwargs,
        )
        return response.points

    client.search = MethodType(search, client)
    return True
