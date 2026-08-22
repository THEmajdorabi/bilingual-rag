"""Reciprocal rank fusion.

Combines the dense and BM25 rankings into one. RRF uses only the *rank* a
document achieved in each list, never its score, which is what makes it work
across retrievers whose scores are not comparable -- a cosine similarity of
0.82 and a BM25 score of 14.3 have no common scale.

    score(d) = sum over rankings of  1 / (k + rank(d))

**One value of k, in one place.** Qdrant can also fuse server-side, but its
built-in RRF uses a different default constant. Mixing the two paths would
silently change the fusion maths between ablation rows and make the table
wrong. Fusion happens here, client-side, at k=60. See DECISIONS.md.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_K = 60


def rrf(rankings: Sequence[Sequence[str]], k: int = DEFAULT_K) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one ranking.

    Args:
        rankings: each inner sequence is ids in rank order, best first.
        k: the RRF constant. Larger flattens the contribution of top ranks.

    Returns:
        (chunk_id, score) sorted by score descending. Ties break on the id, so
        the output is deterministic -- an ablation table that shuffles between
        runs is not measuring anything.
    """
    scores: dict[str, float] = {}

    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def rrf_ids(
    rankings: Sequence[Sequence[str]], k: int = DEFAULT_K, limit: int | None = None
) -> list[str]:
    """`rrf`, returning just the ids."""
    fused = [chunk_id for chunk_id, _ in rrf(rankings, k)]
    return fused[:limit] if limit else fused
