"""Cross-encoder reranking.

A bi-encoder embeds the query and the document independently, so it never sees
them together. A cross-encoder scores the pair jointly and is markedly more
accurate, and markedly slower, which is why it runs over a small candidate
set rather than the corpus.

**The model is `BAAI/bge-reranker-v2-m3` on every hardware profile.** This is
the one thing that must not vary. `bge-reranker-base` is English and Chinese
only; on Arabic it reranks close to randomly, and because reranking never
errors, that would silently poison every row of the ablation table beneath it.
Weaker hardware shrinks the candidate set instead: a latency choice, not a
quality one.

Reranking is usually the largest single latency cost in the pipeline. Its cost
is measured and published, because being able to state the accuracy-for-latency
trade is the point of having measured it.
"""

from __future__ import annotations

import hashlib
import time

import structlog

from bilingual_rag.config import Profile, active_profile

log = structlog.get_logger(__name__)


def _pair_key(query: str, chunk_id: str) -> str:
    return hashlib.sha256(f"{query}\x00{chunk_id}".encode()).hexdigest()[:32]


class Reranker:
    """Cross-encoder over (query, passage) pairs, with a per-pair cache."""

    def __init__(self, profile: Profile | None = None) -> None:
        self.profile = profile or active_profile()
        self._model = None
        self._cache: dict[str, float] = {}
        self.last_latency_ms: float = 0.0

    @property
    def model(self):  # noqa: ANN201 - CrossEncoder, imported lazily
        """Loaded on first use.

        Lazy because on the 8 GB profile the embedder and the reranker cannot
        both be resident, and the pipeline unloads one before loading the other.
        """
        if self._model is None:
            from sentence_transformers import CrossEncoder

            device = "cuda" if self.profile.name.startswith("cuda") else "cpu"
            log.info("reranker.loading", model=self.profile.reranker_model, device=device)
            self._model = CrossEncoder(self.profile.reranker_model, device=device, max_length=1024)
        return self._model

    def unload(self) -> None:
        if self._model is not None:
            self._model = None
            try:
                import torch

                torch.cuda.empty_cache()
            except ImportError:
                pass
            log.info("reranker.unloaded")

    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Score (chunk_id, text) candidates against the query.

        Returns the top `top_k` as (chunk_id, score), best first. `top_k` is
        10 everywhere, not 8: recall@10 has to mean the same thing on every
        row of the ablation table.
        """
        if not candidates:
            return []

        keys = [_pair_key(query, chunk_id) for chunk_id, _ in candidates]
        missing = [i for i, key in enumerate(keys) if key not in self._cache]

        started = time.perf_counter()
        if missing:
            pairs = [[query, candidates[i][1]] for i in missing]
            scores = self.model.predict(
                pairs,
                batch_size=self.profile.rerank_batch_size,
                show_progress_bar=False,
            )
            for position, index in enumerate(missing):
                self._cache[keys[index]] = float(scores[position])
        self.last_latency_ms = (time.perf_counter() - started) * 1000

        scored = [(candidates[i][0], self._cache[keys[i]]) for i in range(len(candidates))]
        scored.sort(key=lambda item: (-item[1], item[0]))

        log.debug(
            "reranker.done",
            candidates=len(candidates),
            scored=len(missing),
            latency_ms=round(self.last_latency_ms, 1),
        )
        return scored[:top_k]
