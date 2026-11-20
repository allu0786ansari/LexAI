from __future__ import annotations

from pathlib import Path

import pytest

from lexai.core.errors import IndexMismatchError, IndexNotReady
from lexai.core.settings import RetrievalSettings
from lexai.retrieval.bm25 import BM25Index, tokenize
from lexai.retrieval.fusion import reciprocal_rank_fusion
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.index import LocalIndex
from tests.conftest import DIM, EMBED_MODEL, FakeEmbedder


def test_tokenize_drops_stopwords_keeps_numbers() -> None:
    assert tokenize("What is the punishment under Section 302?") == ["punishment", "section", "302"]


def test_bm25_ranks_exact_section_match_first() -> None:
    bm25 = BM25Index(["section 420 cheating", "section 302 murder punishment", "article 21 liberty"])
    assert bm25.search("punishment for murder 302", k=2)[0][0] == 1


def test_rrf_rewards_agreement_between_lists() -> None:
    fused = reciprocal_rank_fusion(dense_lists=[[1, 2, 3]], sparse_list=[2, 4], k=60)
    assert fused[0].position == 2
    assert fused[0].matched_dense and fused[0].matched_sparse


def test_index_round_trip_without_pickle(tmp_path: Path, index: LocalIndex) -> None:
    index.save(tmp_path / "idx")
    assert not list((tmp_path / "idx").glob("*.pkl"))
    loaded = LocalIndex.load(tmp_path / "idx", expected_model=EMBED_MODEL, expected_dimension=DIM)
    assert [c.chunk_id for c in loaded.chunks] == [c.chunk_id for c in index.chunks]


def test_index_refuses_a_different_embedding_model(tmp_path: Path, index: LocalIndex) -> None:
    index.save(tmp_path / "idx")
    with pytest.raises(IndexMismatchError):
        LocalIndex.load(tmp_path / "idx", expected_model="another-model", expected_dimension=DIM)
    with pytest.raises(IndexMismatchError):
        LocalIndex.load(tmp_path / "idx", expected_model=EMBED_MODEL, expected_dimension=DIM * 2)


def test_missing_index_is_reported(tmp_path: Path) -> None:
    with pytest.raises(IndexNotReady):
        LocalIndex.load(tmp_path / "nothing", expected_model=EMBED_MODEL, expected_dimension=DIM)


def test_dense_search_rejects_wrong_sized_query(index: LocalIndex) -> None:
    with pytest.raises(IndexMismatchError):
        index.dense_search([0.1] * (DIM + 1), k=3)


async def test_hybrid_retriever_finds_the_murder_section(index: LocalIndex, tmp_path: Path) -> None:
    retriever = HybridRetriever(index, FakeEmbedder(), RetrievalSettings(index_dir=tmp_path))  # type: ignore[arg-type]
    results = await retriever.retrieve("punishment for murder")
    assert results[0].chunk.chunk_id == "c0"
    assert results[0].matched_dense and results[0].matched_sparse
