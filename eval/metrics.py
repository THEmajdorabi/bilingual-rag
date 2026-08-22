"""Retrieval metrics.

All computed against **article numbers**, not chunk ids. An article can be
split across several chunks, so a run that retrieves chunk 2 of article 75 has
found the right article; scoring it against a labelled chunk id would call
that a miss. Article numbers also survive re-chunking, which chunk ids do not,
so the eval set does not go stale every time the chunker changes.

Every metric here is deterministic and needs no model, so the retrieval half of
the ablation table can be measured without spending anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Fraction of relevant articles that appear in the top k.

    For a single-article question this is 1.0 or 0.0. For a multi-hop question
    it is partial credit, which is the honest reading: finding one of two
    required articles is genuinely better than finding neither and genuinely
    worse than finding both.
    """
    if not relevant:
        return 0.0
    top = set(retrieved[:k])
    return len(top & set(relevant)) / len(set(relevant))


def hit_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """1.0 if any relevant article is in the top k."""
    if not relevant:
        return 0.0
    return 1.0 if set(retrieved[:k]) & set(relevant) else 0.0


def reciprocal_rank(retrieved: list[str], relevant: list[str], k: int) -> float:
    """1 / rank of the first relevant article, or 0 if none in the top k."""
    relevant_set = set(relevant)
    for index, item in enumerate(retrieved[:k], start=1):
        if item in relevant_set:
            return 1.0 / index
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Normalized discounted cumulative gain, binary relevance.

    Rewards putting the right article near the top, not merely in the list.
    """
    if not relevant:
        return 0.0
    relevant_set = set(relevant)

    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, item in enumerate(retrieved[:k], start=1)
        if item in relevant_set
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant_set), k) + 1))
    return dcg / ideal if ideal else 0.0


@dataclass
class RetrievalMetrics:
    """Aggregated retrieval quality over a set of questions."""

    n: int
    recall_at_1: float
    recall_at_5: float
    recall_at_10: float
    hit_at_10: float
    mrr_at_10: float
    ndcg_at_10: float
    p50_latency_ms: float
    p95_latency_ms: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "n": self.n,
            "recall@1": round(self.recall_at_1, 4),
            "recall@5": round(self.recall_at_5, 4),
            "recall@10": round(self.recall_at_10, 4),
            "hit@10": round(self.hit_at_10, 4),
            "mrr@10": round(self.mrr_at_10, 4),
            "ndcg@10": round(self.ndcg_at_10, 4),
            "p50_latency_ms": round(self.p50_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
        }


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Explicit because numpy's default interpolates,
    and a p95 that interpolates between two samples is not a measured latency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def aggregate(
    per_question: list[tuple[list[str], list[str], float]],
) -> RetrievalMetrics:
    """Aggregate (retrieved_articles, relevant_articles, latency_ms) triples."""
    if not per_question:
        return RetrievalMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    latencies = [latency for _, _, latency in per_question]

    return RetrievalMetrics(
        n=len(per_question),
        recall_at_1=mean([recall_at_k(r, g, 1) for r, g, _ in per_question]),
        recall_at_5=mean([recall_at_k(r, g, 5) for r, g, _ in per_question]),
        recall_at_10=mean([recall_at_k(r, g, 10) for r, g, _ in per_question]),
        hit_at_10=mean([hit_at_k(r, g, 10) for r, g, _ in per_question]),
        mrr_at_10=mean([reciprocal_rank(r, g, 10) for r, g, _ in per_question]),
        ndcg_at_10=mean([ndcg_at_k(r, g, 10) for r, g, _ in per_question]),
        p50_latency_ms=percentile(latencies, 0.50),
        p95_latency_ms=percentile(latencies, 0.95),
    )
