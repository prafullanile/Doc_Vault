"""Reciprocal Rank Fusion (Cormack et al., 2009).

Keyword scores (ts_rank) and vector distances live on unrelated scales, so normalising and
adding them is fragile. RRF uses only each result's *rank* in each list:

    score(d) = Σ_lists 1 / (k + rank_list(d))

A chunk found by both retrievers outranks one found by only one. k=60 is the standard choice:
it damps the advantage of rank 1 over rank 2 without flattening the list.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

RRF_K = 60


@dataclass
class FusedHit:
    chunk_id: uuid.UUID
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)  # retriever name → 1-based rank


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[uuid.UUID]], k: int = RRF_K
) -> list[FusedHit]:
    hits: dict[uuid.UUID, FusedHit] = {}
    for name, ids in ranked_lists.items():
        for rank, chunk_id in enumerate(ids, start=1):
            hit = hits.setdefault(chunk_id, FusedHit(chunk_id))
            hit.score += 1.0 / (k + rank)
            hit.ranks[name] = rank
    # Ties (same score) are broken by best single rank, then id, for a stable order.
    return sorted(hits.values(), key=lambda h: (-h.score, min(h.ranks.values()), str(h.chunk_id)))
