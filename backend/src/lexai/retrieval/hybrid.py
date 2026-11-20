"""Async hybrid retrieval: dense (FAISS) + sparse (BM25), fused with RRF."""

from __future__ import annotations

import asyncio

from lexai.clients.embedder import EmbeddingClient
from lexai.core.settings import RetrievalSettings
from lexai.domain.models import ScoredChunk
from lexai.retrieval.fusion import reciprocal_rank_fusion
from lexai.retrieval.index import LocalIndex


class HybridRetriever:
    def __init__(self, index: LocalIndex, embedder: EmbeddingClient, settings: RetrievalSettings) -> None:
        self.index = index
        self.embedder = embedder
        self.settings = settings

    async def retrieve(self, query: str, extra_dense_queries: list[str] | None = None) -> list[ScoredChunk]:
        """
        `extra_dense_queries` (e.g. a HyDE passage) add their own dense ranked
        list to the fusion; they never replace the user's query.
        """
        hyde = [t for t in (extra_dense_queries or []) if t.strip()]
        query_vec_task = asyncio.create_task(self.embedder.embed_query(query))
        hyde_vec_task = asyncio.create_task(self.embedder.embed_documents(hyde)) if hyde else None
        sparse = await asyncio.to_thread(self.index.sparse_search, query, self.settings.k_sparse)
        # HyDE passages are document-like, so they get the document prefix, not the query one.
        vectors = [await query_vec_task] + (await hyde_vec_task if hyde_vec_task else [])

        dense_lists = []
        for vector in vectors:
            hits = await asyncio.to_thread(self.index.dense_search, vector, self.settings.k_dense)
            dense_lists.append([pos for pos, _ in hits])

        fused = reciprocal_rank_fusion(dense_lists, [pos for pos, _ in sparse], k=self.settings.rrf_k)
        return [
            ScoredChunk(
                chunk=self.index.chunks[h.position],
                score=h.score,
                matched_dense=h.matched_dense,
                matched_sparse=h.matched_sparse,
            )
            for h in fused[: self.settings.fused_top_n]
        ]
