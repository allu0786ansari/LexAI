from __future__ import annotations

import zlib

import pytest

from lexai.api.container import check_compatible
from lexai.core.errors import IndexMismatchError
from lexai.core.settings import Settings
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore, SearchFilter
from lexai.retrieval.sparse import doc_vector, query_vector, term_index, tokenize
from tests.conftest import FakeEmbedder, make_chunks, publish

Live = tuple[QdrantStore, str, BuildMeta]


def test_tokenize_drops_stopwords_keeps_numbers() -> None:
    assert tokenize("What is the punishment under Section 302?") == ["punishment", "section", "302"]


def test_term_index_is_crc32_so_it_is_stable_across_processes() -> None:
    assert term_index("murder") == zlib.crc32(b"murder") & 0x7FFFFFFF
    assert term_index("murder") != term_index("bail")


def test_bm25_doc_weights_saturate_with_term_frequency() -> None:
    idx1, w1 = doc_vector("murder", avg_len=1)
    idx3, w3 = doc_vector("murder murder murder", avg_len=1)
    assert idx1 == idx3 and w1[0] < w3[0] < 3 * w1[0]
    assert query_vector("murder murder")[1] == [1.0]


async def test_hybrid_search_finds_the_murder_sections(live_store: Live, settings: Settings) -> None:
    store, name, _ = live_store
    retriever = HybridRetriever(store, name, FakeEmbedder(), settings.retrieval)  # type: ignore[arg-type]
    labels = [r.chunk.metadata.label for r in await retriever.retrieve("punishment for murder")]
    assert set(labels[:2]) == {"IPC s.302", "BNS s.103"}


async def test_in_force_filter_excludes_repealed_codes(live_store: Live, settings: Settings) -> None:
    store, name, _ = live_store
    retriever = HybridRetriever(store, name, FakeEmbedder(), settings.retrieval)  # type: ignore[arg-type]
    hits = await retriever.retrieve("punishment for murder", flt=SearchFilter(in_force_only=True))
    assert hits and all(h.chunk.metadata.repealed_on is None for h in hits)
    assert hits[0].chunk.metadata.label == "BNS s.103"


async def test_exact_lookup_by_act_and_section(live_store: Live) -> None:
    store, name, _ = live_store
    found = await store.lookup_unit(name, "Constitution", "21")
    assert [c.metadata.label for c in found] == ["Constitution Art.21"]
    assert found[0].metadata.unit_title == "Protection of life and personal liberty"
    assert await store.lookup_unit(name, "IPC", "999") == []


async def test_new_build_goes_live_and_old_builds_are_pruned(live_store: Live, settings: Settings) -> None:
    store, first, _ = live_store
    second, _ = await publish(store, make_chunks()[:2], settings)
    assert await store.live_collection() == second
    third, _ = await publish(store, make_chunks()[:1], settings)
    pruned = await store.prune(keep=third)
    assert pruned == [first]  # keep_previous=1 keeps `second` for rollback
    assert await store.build_collections() == [second, third]
    assert await store.read_meta(first) is None


async def test_api_refuses_an_index_built_with_another_model(live_store: Live, settings: Settings) -> None:
    _, _, meta = live_store
    check_compatible(meta, settings)  # matches: no error
    settings.embed.model = "another-embedder"
    with pytest.raises(IndexMismatchError, match="embedding model"):
        check_compatible(meta, settings)


def test_chunk_labels() -> None:
    labels = [c.metadata.label for c in make_chunks()]
    assert labels[:4] == ["IPC s.302", "IPC s.420", "BNS s.103", "Constitution Art.21"]


def test_status_is_only_given_for_legislation() -> None:
    from lexai.generation.prompts import status_text

    by_label = {c.metadata.label: c.metadata for c in make_chunks()}
    assert status_text(by_label["IPC s.302"]).startswith("repealed 2024-07-01; replaced by BNS")
    assert status_text(by_label["BNS s.103"]) == "in force"
    judgment = by_label["BNS s.103"].model_copy(update={"doc_type": "judgment"})
    assert status_text(judgment) == ""
