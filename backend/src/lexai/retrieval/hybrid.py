"""
Async hybrid retrieval over Qdrant: a dense query (and optional HyDE passages)
plus a BM25 sparse query, fused server-side with Reciprocal Rank Fusion.
"""

from __future__ import annotations

import asyncio

from lexai.clients.embedder import EmbeddingClient
from lexai.core.settings import RetrievalSettings
from lexai.domain.models import ScoredChunk
from lexai.retrieval.qdrant_store import QdrantStore, SearchFilter
from lexai.retrieval.sparse import query_vector


class HybridRetriever:
    def __init__(
        self, store: QdrantStore, collection: str, embedder: EmbeddingClient, settings: RetrievalSettings
    ) -> None:
        self.store = store
        self.collection = collection
        self.embedder = embedder
        self.settings = settings

    async def retrieve(
        self,
        query: str,
        extra_dense_queries: list[str] | None = None,
        flt: SearchFilter | None = None,
        query_vector_dense: list[float] | None = None,
    ) -> list[ScoredChunk]:
        """
        `extra_dense_queries` (e.g. a HyDE passage) add their own dense ranked list
        to the fusion; they never replace the user's query. Pass `query_vector_dense`
        when the query is already embedded.
        """
        hyde = [t for t in (extra_dense_queries or []) if t.strip()]
        query_vec = (
            None if query_vector_dense is not None else asyncio.create_task(self.embedder.embed_query(query))
        )
        hyde_vecs = await self.embedder.embed_documents(hyde) if hyde else []
        dense: list[float] = await query_vec if query_vec is not None else (query_vector_dense or [])
        return await self.store.hybrid_search(
            self.collection,
            [dense, *hyde_vecs],
            query_vector(query),
            k_dense=self.settings.k_dense,
            k_sparse=self.settings.k_sparse,
            limit=self.settings.fused_top_n,
            flt=flt,
        )
