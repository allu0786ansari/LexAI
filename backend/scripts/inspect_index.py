"""
Inspect the live index after a build (Phase 2 verification).

1. Exact lookups: each provision must exist as its own chunk(s), with title and page.
2. Hybrid searches (no reranker yet): for each question, is one of the expected
   provisions in the top 10? At least SEARCH_PASS_MIN of the questions must hit.
   This is a smoke signal, not an evaluation; Phase 3 adds the reranker and the
   real evaluation set. Some questions need the dense model: CrPC s.438 never
   uses the word "anticipatory", so keyword search alone cannot find it.

Needs the embedding model (Ollama) for the searches. With embedded Qdrant the
API must not be running (one process at a time).

Usage (from backend/): python scripts/inspect_index.py
"""

from __future__ import annotations

import asyncio
import sys

from lexai.api.container import check_compatible
from lexai.clients.embedder import EmbeddingClient
from lexai.core.errors import LexAIError
from lexai.core.settings import get_settings
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.qdrant_store import QdrantStore

LOOKUPS = [
    ("BNS", "103", "punishment for murder"),
    ("IPC", "302", "punishment for murder"),
    ("BNSS", "173", "information in cognizable cases"),
    ("CrPC", "154", "information in cognizable cases"),
    ("BSA", "63", "admissibility of electronic records"),
    ("IEA", "65B", "admissibility of electronic records"),
    ("Constitution", "21", "protection of life and personal liberty"),
    ("IT Act", "66", "computer related offences"),
]

SEARCHES = [
    ("What is the punishment for murder?", {"BNS s.103", "IPC s.302"}),
    ("How is an FIR registered for a cognizable offence?", {"BNSS s.173", "CrPC s.154"}),
    ("When is an electronic record admissible as evidence?", {"BSA s.63", "IEA s.65B"}),
    ("Which article protects the right to life and personal liberty?", {"Constitution Art.21"}),
    ("When can a person get anticipatory bail?", {"BNSS s.482", "CrPC s.438"}),
    # IPC splits cheating over s.415 (definition), s.417 (punishment) and s.420; BNS s.318 has all three.
    ("What is the punishment for cheating?", {"BNS s.318", "IPC s.415", "IPC s.417", "IPC s.420"}),
]

SEARCH_TOP = 10
SEARCH_PASS_MIN = 4

failures: list[str] = []


def report(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    if not ok:
        failures.append(name)


async def main() -> None:
    settings = get_settings()
    store = QdrantStore.connect(settings.qdrant)
    embedder = EmbeddingClient(settings.embed)
    try:
        live = await store.live_collection()
        if not live:
            report("live index", False, "none; run the build step")
            return
        meta = await store.read_meta(live)
        total = await store.count(live)
        print(f"live index {live}: {total} chunks, {len(meta.docs) if meta else '?'} documents")
        if meta:
            print(
                f"embedding model {meta.embedding_model} ({meta.dimension}-dim),"
                f" chunker {meta.chunker_version}\n"
            )

        print("Exact lookups")
        for act, unit, title_words in LOOKUPS:
            found = await store.lookup_unit(live, act, unit)
            if not found:
                report(f"{act} {unit}", False, "not found")
                continue
            m = found[0].metadata
            ok = title_words in (m.unit_title or "").lower()
            report(
                f"{m.label}",
                ok,
                f"{m.unit_title!r}, p.{m.page_start}, {len(found)} piece(s), {m.hierarchy or '-'}",
            )

        print(f"\nHybrid search (expected provision in the top {SEARCH_TOP})")
        if meta is None:
            report("search quality", False, "index has no build metadata; rebuild it")
            return
        try:
            check_compatible(meta, settings)
        except LexAIError as error:
            report("search quality", False, str(error))
            return
        retriever = HybridRetriever(store, live, embedder, settings.retrieval)
        hits_ok = 0
        for question, expected in SEARCHES:
            try:
                hits = await retriever.retrieve(question)
            except LexAIError as error:
                report("search quality", False, str(error))
                return
            labels = [h.chunk.metadata.label for h in hits[:SEARCH_TOP]]
            found = [lab for lab in labels if lab in expected]
            hits_ok += bool(found)
            rank = labels.index(found[0]) + 1 if found else None
            print(
                f"[{'HIT ' if found else 'MISS'}] {question}"
                f" -> {f'{found[0]} at rank {rank}' if found else 'not in top ' + str(SEARCH_TOP)}"
                f"\n       top 5: {' | '.join(labels[:5])}",
                flush=True,
            )
        report(
            "search quality",
            hits_ok >= SEARCH_PASS_MIN,
            f"{hits_ok}/{len(SEARCHES)} questions hit (need {SEARCH_PASS_MIN})",
        )
    finally:
        await embedder.aclose()
        await store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LexAIError as error:  # e.g. the embedded store is locked by a running API
        report("index", False, str(error))
    print("\nSUMMARY:", "ALL PASSED" if not failures else f"FAILED ({len(failures)}): {failures}")
    sys.exit(1 if failures else 0)
