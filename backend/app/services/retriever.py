"""
Hybrid dense + sparse retrieval, fused with Reciprocal Rank Fusion (RRF).

Per proposal §4.1 / §8: FAISS handles semantic similarity, BM25 handles
exact keyword matches (IPC section numbers, case citation names), and the
two ranked lists are merged with RRF: RRF(d) = sum(1 / (k + rank(d))).

Supports multiple dense query embeddings in one call (not just one) so the
HyDE hypothetical-answer embedding and the original query embedding can
both contribute their own ranked list to the fusion — this is what the
proposal means by "the hypothetical answer is embedded and used *alongside*
the original query embedding for retrieval" (§4.1), rather than replacing
it.

Gemini's embedding model is asymmetric: it expects a different
`task_type` for queries ("retrieval_query") than for the documents it was
used to index ("retrieval_document", set at ingestion time in
ingestion/ingest.py). Using the wrong task_type silently degrades
retrieval quality rather than erroring, so this is easy to get wrong
without noticing — the query-side embeddings client below is deliberately
a separate instance configured for "retrieval_query".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.documents import Document

from app.config.logging import get_logger
from app.config.settings import Settings
from app.services.bm25_store import Bm25Store
from app.services.providers import build_embeddings_client
from app.services.vector_store import FaissVectorStore

logger = get_logger(__name__)


def _chunk_identity(doc: Document) -> tuple:
    """
    Stable identity for a chunk across independently-ranked result lists,
    so the same chunk retrieved by both FAISS and BM25 is recognised as
    one document during fusion rather than double-counted. Falls back to
    a content hash if the expected ingestion metadata is missing (e.g.
    hand-added documents that bypassed the ingestion pipeline).
    """
    source = doc.metadata.get("source_file")
    chunk_index = doc.metadata.get("chunk_index")
    if source is not None and chunk_index is not None:
        return (source, chunk_index)
    return ("__content_hash__", hash(doc.page_content))


@dataclass
class FusedDocument:
    document: Document
    rrf_score: float
    matched_dense: bool = False
    matched_sparse: bool = False


@dataclass
class RetrievalTrace:
    """Diagnostic info attached to a retrieval call, useful for logging and for the Phase 3 ablation study."""

    dense_query_count: int = 0
    dense_candidates: int = 0
    sparse_candidates: int = 0
    fused_candidates: int = 0
    law_type_filter: str | None = None
    results: list[FusedDocument] = field(default_factory=list)


def reciprocal_rank_fusion(
    ranked_lists: list[list[Document]],
    k: int,
) -> list[FusedDocument]:
    """
    Standard RRF: for each document, sum 1/(k + rank) across every list it
    appears in (rank is 1-indexed), then sort descending. A document that
    ranks well in multiple lists outscores one that ranks #1 in only one —
    this is the intended behaviour (agreement across dense+sparse signals
    is a stronger relevance signal than either alone).
    """
    scores: dict[tuple, float] = {}
    doc_by_identity: dict[tuple, Document] = {}
    dense_hit: dict[tuple, bool] = {}
    sparse_hit: dict[tuple, bool] = {}

    for list_index, ranked_list in enumerate(ranked_lists):
        is_sparse_list = list_index == len(ranked_lists) - 1  # convention: caller passes sparse list last
        for rank, doc in enumerate(ranked_list, start=1):
            identity = _chunk_identity(doc)
            doc_by_identity[identity] = doc
            scores[identity] = scores.get(identity, 0.0) + 1.0 / (k + rank)
            if is_sparse_list:
                sparse_hit[identity] = True
            else:
                dense_hit[identity] = True

    fused = [
        FusedDocument(
            document=doc_by_identity[identity],
            rrf_score=score,
            matched_dense=dense_hit.get(identity, False),
            matched_sparse=sparse_hit.get(identity, False),
        )
        for identity, score in scores.items()
    ]
    fused.sort(key=lambda f: f.rrf_score, reverse=True)
    return fused


class HybridRetriever:
    def __init__(self, settings: Settings):
        self.settings = settings
        logger.info("loading_vector_store", directory=str(settings.database_dir))
        self.vector_store = FaissVectorStore.load_local(settings.database_dir)
        self.bm25_store = Bm25Store.load_local(settings.database_dir)
        self._query_embeddings = build_embeddings_client(settings, task_type="retrieval_query")
        logger.info(
            "hybrid_retriever_ready",
            faiss_documents=len(self.vector_store),
            bm25_documents=len(self.bm25_store),
        )

    def retrieve(
        self,
        query: str,
        extra_query_texts: list[str] | None = None,
        law_type: str | None = None,
    ) -> tuple[list[FusedDocument], RetrievalTrace]:
        dense_query_texts = [query] + [t for t in (extra_query_texts or []) if t and t.strip()]
        dense_embeddings = self._query_embeddings.embed_documents(dense_query_texts)

        dense_lists: list[list[Document]] = []
        for embedding in dense_embeddings:
            scored = self.vector_store.similarity_search_by_vector(embedding, k=self.settings.retrieval_k_dense)
            dense_lists.append([s.document for s in scored])

        sparse_results = self.bm25_store.search(query, k=self.settings.retrieval_k_sparse)
        sparse_list = [s.document for s in sparse_results]

        all_lists = dense_lists + [sparse_list]  # sparse MUST be last — reciprocal_rank_fusion relies on this order
        fused = reciprocal_rank_fusion(all_lists, k=self.settings.retrieval_rrf_k)

        if law_type:
            before = len(fused)
            fused = [f for f in fused if f.document.metadata.get("law_type") == law_type]
            logger.debug("law_type_filter_applied", law_type=law_type, before=before, after=len(fused))

        top_fused = fused[: self.settings.retrieval_fused_top_n]

        trace = RetrievalTrace(
            dense_query_count=len(dense_query_texts),
            dense_candidates=sum(len(lst) for lst in dense_lists),
            sparse_candidates=len(sparse_list),
            fused_candidates=len(fused),
            law_type_filter=law_type,
            results=top_fused,
        )
        return top_fused, trace

    def health_snapshot(self) -> dict:
        return {
            "faiss_documents": len(self.vector_store),
            "bm25_documents": len(self.bm25_store),
        }
