"""
Retrieval driven by the query plan.

1. Exact provisions named in the question ("302 IPC", "Art. 21") are fetched by
   payload filter and pinned first, whatever their search rank. A section given
   without its act is looked up in the codes that apply (BNS/BNSS/BSA, or
   IPC/CrPC/IEA for older events).
2. A section of the old or new criminal codes brings its counterpart from the
   other code (derived correspondence), so the answer can say "IPC s.302, now
   BNS s.103".
3. Hybrid search (dense + BM25, RRF) supplies the rest. Where it returns a
   section of the code that does not apply, the counterpart in the code that
   does is inserted just before it.
4. With a reranker, the searched candidates are reranked (pinned ones are not).

The best dense similarity (or rerank score) is returned for the abstention check.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from lexai.clients.embedder import EmbeddingClient
from lexai.clients.reranker import Reranker
from lexai.core.settings import QuerySettings, RetrievalSettings
from lexai.domain.models import Chunk, ScoredChunk
from lexai.query.references import NEW_CODES, OLD_CODES
from lexai.query.understand import QueryPlan
from lexai.retrieval.correspondence import Correspondence, Match
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.qdrant_store import QdrantStore, SearchFilter

PINNED_SCORE = 1.0  # displayed relevance of exact lookups


@dataclass
class Retrieved:
    pinned: list[ScoredChunk] = field(default_factory=list)  # exact provisions and their counterparts
    ranked: list[ScoredChunk] = field(default_factory=list)  # searched (and reranked) passages
    best_dense: float | None = None
    best_rerank: float | None = None
    notes: list[str] = field(default_factory=list)  # facts for the prompt (correspondences, misses)

    @property
    def found_exact(self) -> bool:
        return bool(self.pinned)


def codes_for(era: str) -> list[str]:
    return list(NEW_CODES) if era == "new" else list(OLD_CODES)


def describe(match: Match) -> str:
    title = f" ({match.title})" if match.title else ""
    return f"{match.label}{title}"


def correspondence_note(act: str, unit: str, match: Match) -> str:
    return (
        f"{act} s.{unit} corresponds to {describe(match)} (matched by title and text, similarity "
        f"{match.score:.2f}; a derived correspondence, not an official table)."
    )


class Router:
    def __init__(
        self,
        store: QdrantStore,
        collection: str,
        embedder: EmbeddingClient,
        hybrid: HybridRetriever,
        correspondence: Correspondence,
        retrieval: RetrievalSettings,
        query: QuerySettings,
        reranker: Reranker | None,
        rerank_top_n: int,
    ) -> None:
        self.store = store
        self.collection = collection
        self.embedder = embedder
        self.hybrid = hybrid
        self.correspondence = correspondence
        self.retrieval = retrieval
        self.query = query
        self.reranker = reranker
        self.rerank_top_n = rerank_top_n

    async def _lookup(self, act: str, unit: str) -> list[Chunk]:
        return await self.store.lookup_unit(self.collection, act, unit)

    async def _exact(self, plan: QueryPlan, out: Retrieved) -> None:
        for ref in plan.references[: self.query.max_references]:
            acts = (
                [ref.act]
                if ref.act
                else (["Constitution"] if ref.unit_label == "Article" else codes_for(plan.era.era))
            )
            found: list[tuple[str, list[Chunk]]] = []
            for act in acts:
                pieces = await self._lookup(act, ref.unit)
                if pieces:
                    found.append((act, pieces))
            if not found:
                out.notes.append(f"{ref.label} was not found in the indexed text.")
                continue
            if ref.act is None and len(found) > 1:
                names = ", ".join(f"{a} s.{ref.unit}" for a, _ in found)
                out.notes.append(f"The question does not name the act for section {ref.unit}; found {names}.")
            for act, pieces in found:
                out.pinned.extend(ScoredChunk(chunk=c, score=PINNED_SCORE) for c in pieces)
                await self._counterparts(act, ref.unit, out, top=2)

    async def _counterparts(self, act: str, unit: str, out: Retrieved, *, top: int) -> None:
        if act not in OLD_CODES and act not in NEW_CODES:
            return
        matches = self.correspondence.equivalents(act, unit)[:top]
        if not matches:
            out.notes.append(f"No confident counterpart of {act} s.{unit} was found in the other code.")
            return
        for m in matches:
            pieces = await self._lookup(m.act, m.unit)
            if not pieces:
                continue
            out.notes.append(correspondence_note(act, unit, m))
            out.pinned.extend(ScoredChunk(chunk=c, score=PINNED_SCORE) for c in pieces)

    async def _prefer_applicable_code(
        self, plan: QueryPlan, ranked: list[ScoredChunk], out: Retrieved
    ) -> list[ScoredChunk]:
        """
        Put the counterparts in the applicable code (the best match and a close runner-up,
        e.g. IPC 302 -> BNS 103 and 104) just before a section of the code that does not
        apply: moved up if retrieved further down, fetched if not.
        """
        wrong = OLD_CODES if plan.era.era == "new" else NEW_CODES
        pinned = {c.chunk.metadata.parent_id for c in out.pinned}
        remaining = list(ranked)
        result: list[ScoredChunk] = []
        handled = 0
        while remaining:
            item = remaining.pop(0)
            meta = item.chunk.metadata
            if handled < 3 and meta.short_name in wrong and meta.unit and meta.unit_label == "Section":
                matches = self.correspondence.equivalents(meta.short_name, meta.unit)[:2]
                for best in matches:
                    placed = {c.chunk.metadata.parent_id for c in result}
                    later = [
                        c for c in remaining
                        if c.chunk.metadata.short_name == best.act and c.chunk.metadata.unit == best.unit
                    ]  # fmt: skip
                    if later:
                        remaining = [c for c in remaining if c not in later]
                        result.extend(later)
                    else:
                        pieces = await self._lookup(best.act, best.unit)
                        parent = pieces[0].metadata.parent_id if pieces else None
                        if pieces and parent not in pinned and parent not in placed:
                            result.extend(ScoredChunk(chunk=c, score=item.score) for c in pieces)
                    out.notes.append(correspondence_note(meta.short_name, meta.unit, best))
                if matches:
                    handled += 1
            result.append(item)
        return result

    async def retrieve(
        self, plan: QueryPlan, extra_dense: list[str] | None = None, flt: SearchFilter | None = None
    ) -> Retrieved:
        out = Retrieved()
        vector = await self.embedder.embed_query(plan.standalone)
        search = asyncio.create_task(
            self.hybrid.retrieve(plan.standalone, extra_dense, flt, query_vector_dense=vector)
        )
        best = asyncio.create_task(self.store.best_dense_score(self.collection, vector, flt))
        await self._exact(plan, out)
        candidates = await search
        out.best_dense = await best

        if self.reranker is not None and candidates:
            ranked_idx = await self.reranker.rerank(
                plan.standalone, [c.chunk.text for c in candidates], self.rerank_top_n
            )
            ranked = [ScoredChunk(chunk=candidates[i].chunk, score=s) for i, s in ranked_idx]
            out.best_rerank = ranked[0].score if ranked else None
        else:
            ranked = candidates[: self.rerank_top_n]
        out.ranked = await self._prefer_applicable_code(plan, ranked, out)
        out.notes = list(dict.fromkeys(out.notes))  # each note once, in order
        return out
