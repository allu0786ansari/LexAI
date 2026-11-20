"""
Turn retrieved passages into the numbered context the model sees.

- Exact provisions (pinned) come first, then the searched passages, each once.
- A section is shown whole when it was looked up exactly or when two or more of
  its pieces were retrieved: the pieces are re-joined (without the overlap the
  splitter added) instead of appearing as fragments under separate numbers.
- Named sections and their counterparts are all kept, each cut to an equal
  share of the character budget if needed (a long CrPC section must not crowd
  out its BNSS counterpart). Searched passages then fill what is left, in order.
"""

from __future__ import annotations

from lexai.domain.models import Chunk, ScoredChunk
from lexai.retrieval.qdrant_store import QdrantStore
from lexai.retrieval.router import Retrieved

HEADER_ALLOWANCE = 120  # characters per passage header in the prompt
MIN_SHARE = 900  # never cut an exact provision shorter than this
# A unit of more pieces than this (a schedule, a long judgment) is never fetched
# whole for a search hit: only the retrieved pieces are shown.
MAX_WHOLE_PARTS = 4
TRUNCATED = "\n[… section continues; shortened to fit]"


def join_pieces(texts: list[str], max_overlap: int = 400) -> str:
    """Join consecutive pieces, dropping text repeated at the start of the next piece."""
    if not texts:
        return ""
    out = texts[0]
    for nxt in texts[1:]:
        cut = 0
        for k in range(min(max_overlap, len(nxt), len(out)), 19, -1):
            if out.endswith(nxt[:k]):
                cut = k
                break
        out = out.rstrip() + "\n" + nxt[cut:].lstrip() if cut else out.rstrip() + "\n" + nxt.lstrip()
    return out


def merge(pieces: list[Chunk]) -> Chunk:
    pieces = sorted(pieces, key=lambda c: c.metadata.part)
    first, last = pieces[0], pieces[-1]
    if len(pieces) == 1 and first.metadata.parts == 1:
        return first
    meta = first.metadata.model_copy(update={"part": 1, "parts": 1, "page_end": last.metadata.page_end})
    return Chunk(
        chunk_id=first.chunk_id,
        text=join_pieces([p.text for p in pieces]),
        embed_text=first.embed_text,
        metadata=meta,
    )


async def assemble(
    store: QdrantStore,
    collection: str,
    retrieved: Retrieved,
    *,
    char_budget: int,
    max_passages: int,
) -> list[ScoredChunk]:
    pinned_parents = {p.chunk.metadata.parent_id for p in retrieved.pinned}
    order: list[str] = []
    groups: dict[str, list[ScoredChunk]] = {}
    seen: set[str] = set()
    for item in [*retrieved.pinned, *retrieved.ranked]:
        if item.chunk.chunk_id in seen:
            continue
        seen.add(item.chunk.chunk_id)
        parent = item.chunk.metadata.parent_id
        if parent not in groups:
            order.append(parent)
            groups[parent] = []
        groups[parent].append(item)

    passages: list[ScoredChunk] = []
    pinned_count = 0
    for parent in order:
        items = groups[parent]
        meta = items[0].chunk.metadata
        whole = parent in pinned_parents or (len(items) >= 2 and meta.parts <= MAX_WHOLE_PARTS)
        score = max(i.score for i in items)
        if whole and meta.parts > 1:
            pieces = [i.chunk for i in items]
            if len(pieces) < meta.parts:
                pieces = await store.parts_of(collection, parent) or pieces
            passages.append(ScoredChunk(chunk=merge(pieces), score=score))
        else:
            passages.extend(items)
        if parent in pinned_parents:
            pinned_count = len(passages)
    return fit(passages, char_budget=char_budget, max_passages=max_passages, pinned=pinned_count)


def _cut(item: ScoredChunk, room: int) -> ScoredChunk:
    if len(item.chunk.text) <= room:
        return item
    room = max(200, room - len(TRUNCATED))
    text = item.chunk.text[:room].rsplit(" ", 1)[0] + TRUNCATED
    return ScoredChunk(chunk=item.chunk.model_copy(update={"text": text}), score=item.score)


def fit(
    passages: list[ScoredChunk], *, char_budget: int, max_passages: int, pinned: int = 0
) -> list[ScoredChunk]:
    """
    The first `pinned` passages are exact provisions: all kept (budget permitting), each
    cut to an equal share if needed. The rest fill the remaining budget in order; the
    first passage overall is always kept.
    """
    kept: list[ScoredChunk] = []
    used = 0
    if pinned:
        share = max(MIN_SHARE, char_budget // pinned)
        for item in passages[:pinned]:
            item = _cut(item, share - HEADER_ALLOWANCE)
            size = len(item.chunk.text) + HEADER_ALLOWANCE
            if kept and used + size > char_budget:
                break
            kept.append(item)
            used += size
    for item in passages[pinned:]:
        if len(kept) >= max_passages:
            break
        size = len(item.chunk.text) + HEADER_ALLOWANCE
        if not kept and size > char_budget:
            # an oversized first search hit gets half the budget, leaving room for others
            item = _cut(item, char_budget // 2 - HEADER_ALLOWANCE)
            kept.append(item)
            used = len(item.chunk.text) + HEADER_ALLOWANCE
            continue
        if used + size > char_budget:
            continue  # a smaller passage further down may still fit
        kept.append(item)
        used += size
    return kept
