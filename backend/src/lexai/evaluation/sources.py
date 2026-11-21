"""Read the indexed text of a provision label or judgment doc_id (for validating golden items)."""

from __future__ import annotations

from qdrant_client import models

from lexai.evaluation.dataset import parse_label
from lexai.generation.context import join_pieces
from lexai.retrieval.qdrant_store import QdrantStore


class IndexTextSource:
    def __init__(self, store: QdrantStore, collection: str) -> None:
        self.store = store
        self.collection = collection
        self._cache: dict[str, str | None] = {}

    async def __call__(self, ref: str) -> str | None:
        if ref not in self._cache:
            self._cache[ref] = await self._read(ref)
        return self._cache[ref]

    async def _read(self, ref: str) -> str | None:
        if ref.startswith("sc-"):
            flt = models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=ref))]
            )
        else:
            act, unit_label, unit = parse_label(ref)
            flt = models.Filter(
                must=[
                    models.FieldCondition(key="short_name", match=models.MatchValue(value=act)),
                    models.FieldCondition(key="unit_label", match=models.MatchValue(value=unit_label)),
                    models.FieldCondition(key="unit", match=models.MatchValue(value=unit)),
                ]
            )
        chunks = await self.store.scroll_all(self.collection, flt)
        if not chunks:
            return None
        chunks.sort(key=lambda c: (c.metadata.parent_id, c.metadata.part))
        return join_pieces([c.text for c in chunks])
