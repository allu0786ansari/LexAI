"""Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank_d), rank starting at 1."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FusedHit:
    position: int
    score: float
    matched_dense: bool = False
    matched_sparse: bool = False


def reciprocal_rank_fusion(
    dense_lists: list[list[int]],
    sparse_list: list[int],
    k: int,
) -> list[FusedHit]:
    """Fuse ranked lists of corpus positions. Lists are passed separately, so their kind is explicit."""
    hits: dict[int, FusedHit] = {}
    for ranked in dense_lists:
        for rank, pos in enumerate(ranked, start=1):
            hit = hits.setdefault(pos, FusedHit(position=pos, score=0.0))
            hit.score += 1.0 / (k + rank)
            hit.matched_dense = True
    for rank, pos in enumerate(sparse_list, start=1):
        hit = hits.setdefault(pos, FusedHit(position=pos, score=0.0))
        hit.score += 1.0 / (k + rank)
        hit.matched_sparse = True
    return sorted(hits.values(), key=lambda h: h.score, reverse=True)
