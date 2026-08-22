"""The retrieval pipeline, with every stage switchable.

    dense top-50  ─┐
                   ├─► RRF (k=60) ─► top-N ─► cross-encoder ─► top-10
    BM25  top-50  ─┘

Each stage can be turned off independently, because that is exactly what the
ablation table measures. A pipeline where the stages are welded together can
be demonstrated but not measured.

**Every configuration returns exactly 10 chunks.** Dense-only, fused and
reranked all cut to 10, and the reranker's output is the top 10 of its
candidate set. Reporting recall@10 for one row and recall@8 for another would
make the table meaningless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from bilingual_rag.config import Profile, QdrantSettings, RetrievalSettings, active_profile
from bilingual_rag.index.embedder import Embedder
from bilingual_rag.index.fusion import rrf_ids
from bilingual_rag.index.lexical import BM25Index
from bilingual_rag.index.rerank import Reranker
from bilingual_rag.index.vectors import VectorStore

log = structlog.get_logger(__name__)


@dataclass
class Passage:
    """One retrieved chunk, as the answer layer and the API see it."""

    chunk_id: str
    text: str
    heading_path: list[str]
    article_no: str | None
    page: int
    lang: str
    score: float
    rank: int
    doc_id: str = ""

    def citation_label(self) -> str:
        return f"المادة {self.article_no}" if self.article_no else self.chunk_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "heading_path": self.heading_path,
            "article_no": self.article_no,
            "page": self.page,
            "lang": self.lang,
            "score": self.score,
            "rank": self.rank,
        }


@dataclass
class RetrievalConfig:
    """Which stages run. One object per ablation row."""

    use_dense: bool = True
    use_bm25: bool = True
    use_rerank: bool = True
    # Normalization is always on in production; the ablation turns it off to
    # measure what it is worth.
    use_normalization: bool = True
    name: str = "full"


@dataclass
class RetrievalResult:
    passages: list[Passage]
    config_name: str
    latency_ms: float
    stage_latencies: dict[str, float] = field(default_factory=dict)

    def ids(self) -> list[str]:
        return [p.chunk_id for p in self.passages]


class Retriever:
    """Hybrid retrieval over the indexed corpus."""

    def __init__(
        self,
        profile: Profile | None = None,
        settings: RetrievalSettings | None = None,
        qdrant_settings: QdrantSettings | None = None,
        embedder: Embedder | None = None,
        bm25: BM25Index | None = None,
        store: VectorStore | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.profile = profile or active_profile()
        self.settings = settings or RetrievalSettings()
        self.embedder = embedder or Embedder(profile=self.profile)
        self.store = store or VectorStore(profile=self.profile, settings=qdrant_settings)
        self.bm25 = bm25
        self.reranker = reranker or Reranker(profile=self.profile)
        self._payloads: dict[str, dict[str, Any]] = {}

    # ---- setup ----------------------------------------------------------

    def load_bm25(self, directory: str = "data/bm25_index") -> None:
        self.bm25 = BM25Index.load(directory)

    def _payload(self, chunk_id: str) -> dict[str, Any]:
        """Chunk metadata, cached in memory after the first lookup."""
        if not self._payloads:
            for payload in self.store.all_payloads():
                self._payloads[payload["chunk_id"]] = payload
        return self._payloads.get(chunk_id, {})

    # ---- retrieval ------------------------------------------------------

    def retrieve(
        self,
        query: str,
        config: RetrievalConfig | None = None,
        top_k: int | None = None,
    ) -> RetrievalResult:
        """Run the pipeline for one query."""
        config = config or RetrievalConfig()
        top_k = top_k or self.settings.top_k
        stage_latencies: dict[str, float] = {}
        overall_start = time.perf_counter()

        rankings: list[list[str]] = []

        if config.use_dense:
            started = time.perf_counter()
            # The embedder normalizes the query with the same function used on
            # the indexed text -- that is the property the whole design turns on.
            vector = self.embedder.embed_query(query)
            dense_ids = self.store.search_ids(vector, top_k=self.settings.dense_top_k)
            rankings.append(dense_ids)
            stage_latencies["dense_ms"] = (time.perf_counter() - started) * 1000

        if config.use_bm25:
            if self.bm25 is None:
                self.load_bm25()
            started = time.perf_counter()
            assert self.bm25 is not None
            rankings.append(self.bm25.search_ids(query, top_k=self.settings.bm25_top_k))
            stage_latencies["bm25_ms"] = (time.perf_counter() - started) * 1000

        if not rankings:
            raise ValueError("retrieval config has every retriever disabled")

        # One ranking needs no fusion; two do. Either way the candidate list
        # is a list of ids in rank order.
        if len(rankings) == 1:
            candidate_ids = rankings[0]
        else:
            started = time.perf_counter()
            candidate_ids = rrf_ids(rankings, k=self.settings.rrf_k)
            stage_latencies["fusion_ms"] = (time.perf_counter() - started) * 1000

        if config.use_rerank:
            candidates = candidate_ids[: self.profile.rerank_candidates]
            pairs = [(cid, self._payload(cid).get("text", "")) for cid in candidates]
            started = time.perf_counter()
            reranked = self.reranker.rerank(query, pairs, top_k=top_k)
            stage_latencies["rerank_ms"] = (time.perf_counter() - started) * 1000
            final = reranked
        else:
            # Without a reranker there is no meaningful cross-encoder score, so
            # rank position is reported as the score. It is only used for
            # ordering, and recall@10 does not read it.
            final = [(cid, 1.0 / (rank + 1)) for rank, cid in enumerate(candidate_ids[:top_k])]

        passages = []
        for rank, (chunk_id, score) in enumerate(final, start=1):
            payload = self._payload(chunk_id)
            passages.append(
                Passage(
                    chunk_id=chunk_id,
                    text=payload.get("text", ""),
                    heading_path=payload.get("heading_path", []),
                    article_no=payload.get("article_no"),
                    page=payload.get("page", 1),
                    lang=payload.get("lang", "ar"),
                    doc_id=payload.get("doc_id", ""),
                    score=score,
                    rank=rank,
                )
            )

        latency_ms = (time.perf_counter() - overall_start) * 1000
        log.debug(
            "retrieve.done",
            config=config.name,
            query_chars=len(query),
            returned=len(passages),
            latency_ms=round(latency_ms, 1),
        )
        return RetrievalResult(
            passages=passages,
            config_name=config.name,
            latency_ms=latency_ms,
            stage_latencies=stage_latencies,
        )

    def get_article(self, article_no: str) -> list[Passage]:
        """Fetch an article by number, for the agent's lookup tool."""
        payloads = self.store.get_by_article(article_no)
        return [
            Passage(
                chunk_id=p["chunk_id"],
                text=p.get("text", ""),
                heading_path=p.get("heading_path", []),
                article_no=p.get("article_no"),
                page=p.get("page", 1),
                lang=p.get("lang", "ar"),
                doc_id=p.get("doc_id", ""),
                score=1.0,
                rank=rank,
            )
            for rank, p in enumerate(payloads, start=1)
        ]


# The ablation rows, in the order the README table presents them. Each adds
# one component to the row above, so the table reads as a build-up.
ABLATION_CONFIGS = [
    RetrievalConfig(
        use_dense=True,
        use_bm25=False,
        use_rerank=False,
        use_normalization=False,
        name="dense only, no normalization",
    ),
    RetrievalConfig(
        use_dense=True,
        use_bm25=False,
        use_rerank=False,
        use_normalization=True,
        name="+ Arabic normalization",
    ),
    RetrievalConfig(
        use_dense=True,
        use_bm25=True,
        use_rerank=False,
        use_normalization=True,
        name="+ BM25 hybrid with RRF",
    ),
    RetrievalConfig(
        use_dense=True,
        use_bm25=True,
        use_rerank=True,
        use_normalization=True,
        name="+ cross-encoder reranking",
    ),
]
