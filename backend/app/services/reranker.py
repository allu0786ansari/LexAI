"""
CrossEncoder reranking — the final relevance-scoring stage before context
assembly.

Per proposal §4.1 / §8: the top ~20 RRF-fused candidates are re-scored by
a CrossEncoder (query and chunk encoded jointly, not independently like
FAISS/BM25), which is more accurate than either retrieval signal alone but
too slow to run over the full corpus — hence "retrieve broad, rerank
narrow" as a two-stage pipeline.

Operational note (worth knowing before deploying, not hidden): this pulls
in `sentence-transformers` + `torch`, a few hundred MB, and the model is
downloaded from Hugging Face on first load. On free-tier HF Spaces this
adds real cold-start time — loading the model once at process startup
(see app/main.py) rather than per-request is what keeps per-request
latency reasonable.
"""
from __future__ import annotations

from dataclasses import dataclass

from langchain_core.documents import Document

from app.config.logging import get_logger
from app.config.settings import Settings

logger = get_logger(__name__)


@dataclass
class RerankedDocument:
    document: Document
    rerank_score: float


class CrossEncoderReranker:
    def __init__(self, settings: Settings):
        # Imported lazily so importing this module doesn't require torch
        # to be installed (e.g. when `reranker_enabled=False` and the
        # caller never constructs this class — see qa_service.py).
        from sentence_transformers import CrossEncoder

        logger.info("loading_reranker_model", model=settings.reranker_model)
        self._model = CrossEncoder(settings.reranker_model)
        logger.info("reranker_ready", model=settings.reranker_model)

    def rerank(self, query: str, documents: list[Document], top_n: int) -> list[RerankedDocument]:
        if not documents:
            return []
        texts = [doc.page_content for doc in documents]
        ranked = self._model.rank(query, texts, top_k=min(top_n, len(documents)), return_documents=False)
        return [
            RerankedDocument(document=documents[entry["corpus_id"]], rerank_score=float(entry["score"]))
            for entry in ranked
        ]
