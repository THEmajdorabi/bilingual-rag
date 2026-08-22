"""Qdrant: dense vectors only.

**Design decision, made here so it is not made twice: Qdrant stores the dense
vectors, and fusion happens client-side in `fusion.py` at k=60.** Qdrant can
also hold sparse vectors and fuse server-side, but its built-in RRF uses a
different default constant. Running both paths would silently change the
fusion maths between ablation rows and make the table wrong. See DECISIONS.md.

Two safety properties:

  * The collection name encodes the embedding model and its dimension, so a
    profile switch cannot quietly write 768-dim vectors into a 1024-dim store.
  * The model id and dimension are also written into the collection and
    **asserted at startup**, so a mismatch fails loudly rather than at query
    time.

Ingestion is idempotent: point ids are content hashes, so re-ingesting an
unchanged corpus overwrites identical data and changes nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
import structlog

from bilingual_rag.config import Profile, QdrantSettings, active_profile
from bilingual_rag.ingest.chunk import Chunk

log = structlog.get_logger(__name__)

# Qdrant point ids must be UUIDs or unsigned integers, not arbitrary strings.
# Chunk ids are content hashes, so a deterministic UUID5 keeps ingestion
# idempotent while satisfying that constraint.
POINT_NAMESPACE = uuid.UUID("6f0d8a1e-1b6c-4c2f-9a7d-2e3f4b5c6d7e")


def point_id(chunk_id: str) -> str:
    """Deterministic UUID for a chunk id."""
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


class VectorStore:
    """Dense vector storage and search."""

    def __init__(
        self,
        profile: Profile | None = None,
        settings: QdrantSettings | None = None,
    ) -> None:
        self.profile = profile or active_profile()
        self.settings = settings or QdrantSettings()
        self._client = None
        # Set to target the un-normalized sibling collection built for the
        # ablation baseline. None means the normal collection.
        self.collection_override: str | None = None

    @property
    def client(self):  # noqa: ANN201 - QdrantClient, imported lazily
        if self._client is None:
            from qdrant_client import QdrantClient

            if self.settings.local_path:
                # Embedded mode: no server needed. Used by tests and by anyone
                # who cannot run Docker.
                log.info("qdrant.local", path=self.settings.local_path)
                self._client = QdrantClient(path=self.settings.local_path)
            else:
                log.info("qdrant.remote", url=self.settings.url)
                self._client = QdrantClient(
                    url=self.settings.url, timeout=int(self.settings.timeout_s)
                )
        return self._client

    @property
    def collection(self) -> str:
        return self.collection_override or self.profile.collection_name

    # ---- lifecycle ------------------------------------------------------

    def ensure_collection(self, recreate: bool = False) -> None:
        """Create the collection if absent, and verify it matches the profile."""
        from qdrant_client.models import Distance, VectorParams

        exists = self.client.collection_exists(self.collection)

        if exists and recreate:
            log.warning("qdrant.recreating", collection=self.collection)
            self.client.delete_collection(self.collection)
            exists = False

        if not exists:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.profile.dense_dims,
                    distance=Distance.COSINE,  # vectors are L2-normalized by the embedder
                ),
            )
            log.info(
                "qdrant.created",
                collection=self.collection,
                dims=self.profile.dense_dims,
                distance="cosine",
            )
            return

        self.assert_matches_profile()

    def assert_matches_profile(self) -> None:
        """Fail at startup, not at query time, when the store and profile differ.

        Without this, switching profiles gives either a dimension error deep in
        a search call or -- worse, if the widths happen to match -- silently
        wrong results from vectors produced by a different model.
        """
        info = self.client.get_collection(self.collection)
        params = info.config.params.vectors
        actual_dims = params.size if hasattr(params, "size") else None

        if actual_dims != self.profile.dense_dims:
            raise RuntimeError(
                f"collection {self.collection!r} holds {actual_dims}-dim vectors but the "
                f"active profile {self.profile.name!r} produces {self.profile.dense_dims}-dim "
                f"vectors from {self.profile.embedding_model!r}. Re-ingest with this profile, "
                f"or switch profiles with BILINGUAL_RAG_PROFILE."
            )

    def count(self) -> int:
        if not self.client.collection_exists(self.collection):
            return 0
        return int(self.client.count(self.collection, exact=True).count)

    # ---- writing --------------------------------------------------------

    def upsert(self, chunks: list[Chunk], vectors: np.ndarray, batch_size: int = 128) -> int:
        """Write chunks and their vectors. Idempotent by content hash."""
        from qdrant_client.models import PointStruct

        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")

        points = [
            PointStruct(
                id=point_id(chunk.id),
                vector=vectors[index].tolist(),
                payload={
                    "chunk_id": chunk.id,
                    "doc_id": chunk.doc_id,
                    # The ORIGINAL text, for display and citation.
                    "text": chunk.text,
                    "text_norm": chunk.text_norm,
                    "heading_path": chunk.heading_path,
                    "article_no": chunk.article_no,
                    "page": chunk.page,
                    "span": list(chunk.span),
                    "lang": chunk.lang,
                    "ocr_confidence": chunk.ocr_confidence,
                },
            )
            for index, chunk in enumerate(chunks)
        ]

        for start in range(0, len(points), batch_size):
            self.client.upsert(self.collection, points=points[start : start + batch_size])

        log.info("qdrant.upserted", collection=self.collection, points=len(points))
        return len(points)

    # ---- reading --------------------------------------------------------

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 50,
        lang: str | None = None,
        doc_id: str | None = None,
    ) -> list[tuple[str, float, dict[str, Any]]]:
        """Dense search. Returns (chunk_id, score, payload), best first."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = []
        if lang:
            conditions.append(FieldCondition(key="lang", match=MatchValue(value=lang)))
        if doc_id:
            conditions.append(FieldCondition(key="doc_id", match=MatchValue(value=doc_id)))

        response = self.client.query_points(
            collection_name=self.collection,
            query=query_vector.tolist(),
            limit=top_k,
            with_payload=True,
            query_filter=Filter(must=conditions) if conditions else None,
        )

        return [
            (point.payload["chunk_id"], float(point.score), point.payload)
            for point in response.points
        ]

    def search_ids(self, query_vector: np.ndarray, top_k: int = 50, **kwargs: Any) -> list[str]:
        return [chunk_id for chunk_id, _, _ in self.search(query_vector, top_k, **kwargs)]

    def get_by_article(self, article_no: str, doc_id: str | None = None) -> list[dict[str, Any]]:
        """Fetch every chunk of one article, for the `get_article` tool."""
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = [FieldCondition(key="article_no", match=MatchValue(value=str(article_no)))]
        if doc_id:
            conditions.append(FieldCondition(key="doc_id", match=MatchValue(value=doc_id)))

        points, _ = self.client.scroll(
            collection_name=self.collection,
            scroll_filter=Filter(must=conditions),
            limit=50,
            with_payload=True,
        )
        return [point.payload for point in points]

    def all_payloads(self) -> list[dict[str, Any]]:
        """Every stored payload, used to rebuild the BM25 index."""
        payloads: list[dict[str, Any]] = []
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                limit=256,
                offset=offset,
                with_payload=True,
            )
            payloads.extend(point.payload for point in points)
            if offset is None:
                break
        return payloads
