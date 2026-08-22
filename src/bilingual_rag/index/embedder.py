"""Dense embeddings, profile-aware and cached.

Two details here are silent-failure territory rather than crash territory:

  * **E5 prefixes.** `intfloat/multilingual-e5-base` was trained with `query: `
    and `passage: ` prefixes. Omitting them does not error; it quietly costs a
    large fraction of retrieval quality. The prefixes live on the profile, and
    `embed_queries` and `embed_passages` are separate methods so the two can
    never be confused at a call site.
  * **Vector normalization.** Cosine similarity on unnormalized vectors is not
    cosine similarity. Every vector is L2-normalized before it leaves here.

Embeddings are cached to disk by content hash, so re-ingesting an unchanged
corpus costs nothing and the ablation script can re-run cheaply.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import structlog

from bilingual_rag.config import Profile, active_profile

log = structlog.get_logger(__name__)

CACHE_DIR = Path("data/.embedding_cache")


def _cache_key(model_id: str, prefix: str, text: str) -> str:
    """Content hash including the model and prefix.

    The model is part of the key because two profiles produce different
    vectors; the prefix is part of it because `query: x` and `passage: x`
    embed differently and must not collide.
    """
    blob = f"{model_id}\x00{prefix}\x00{text}".encode()
    return hashlib.sha256(blob).hexdigest()


class Embedder:
    """Wraps a sentence-transformers model with profile settings and a cache."""

    def __init__(self, profile: Profile | None = None, cache_dir: Path | None = None) -> None:
        self.profile = profile or active_profile()
        self.cache_dir = cache_dir or CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = None  # loaded lazily: importing torch is slow
        self._cache: dict[str, np.ndarray] = {}
        self._cache_path = self.cache_dir / f"{self.profile.collection_name}.npz"
        self._load_cache()

    # ---- model ----------------------------------------------------------

    @property
    def model(self):  # noqa: ANN201 - SentenceTransformer, imported lazily
        """The underlying model, loaded on first use.

        Lazy because the CLI, the tests and the ingest script all import this
        module, and only some of them actually embed anything. On an 8 GB card
        the embedder and the reranker cannot both sit in VRAM, so whichever is
        not in use should not be resident.
        """
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            device = "cuda" if self.profile.name.startswith("cuda") else self.profile.name
            device = "cpu" if device == "cpu" else device
            log.info(
                "embedder.loading",
                model=self.profile.embedding_model,
                device=device,
                fp16=self.profile.use_fp16,
            )
            self._model = SentenceTransformer(self.profile.embedding_model, device=device)
            if self.profile.use_fp16:
                self._model = self._model.half()
        return self._model

    def unload(self) -> None:
        """Release the model and free VRAM.

        Needed on the 8 GB profile: BGE-M3 and bge-reranker-v2-m3 together do
        not fit, so the pipeline loads them in sequence rather than at once.
        """
        if self._model is not None:
            self._model = None
            try:
                import torch

                torch.cuda.empty_cache()
            except ImportError:
                pass
            log.info("embedder.unloaded")

    # ---- cache ----------------------------------------------------------

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            with np.load(self._cache_path) as data:
                self._cache = {key: data[key] for key in data.files}
            log.info("embedder.cache_loaded", entries=len(self._cache))
        except (OSError, ValueError, EOFError) as exc:
            # A truncated cache is not worth a crash; it is worth a warning.
            log.warning("embedder.cache_unreadable", error=str(exc))
            self._cache = {}

    def save_cache(self) -> None:
        if not self._cache:
            return
        np.savez_compressed(self._cache_path, **self._cache)
        log.info("embedder.cache_saved", entries=len(self._cache), path=str(self._cache_path))

    # ---- embedding ------------------------------------------------------

    def _embed(self, texts: list[str], prefix: str, use_cache: bool) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.profile.dense_dims), dtype=np.float32)

        keys = [_cache_key(self.profile.embedding_model, prefix, t) for t in texts]
        missing = [i for i, key in enumerate(keys) if not use_cache or key not in self._cache]

        if missing:
            batch = [prefix + texts[i] for i in missing]
            vectors = self.model.encode(
                batch,
                batch_size=self.profile.embed_batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,  # cosine distance requires this
                show_progress_bar=len(batch) > 64,
            )
            for position, index in enumerate(missing):
                self._cache[keys[index]] = vectors[position].astype(np.float32)

        return np.vstack([self._cache[key] for key in keys])

    def embed_passages(self, texts: list[str], use_cache: bool = True) -> np.ndarray:
        """Embed documents for indexing. Applies the profile's passage prefix."""
        return self._embed(texts, self.profile.passage_prefix, use_cache)

    def embed_queries(self, texts: list[str], use_cache: bool = True) -> np.ndarray:
        """Embed queries for search. Applies the profile's query prefix."""
        return self._embed(texts, self.profile.query_prefix, use_cache)

    def embed_query(self, text: str, use_cache: bool = True) -> np.ndarray:
        return self.embed_queries([text], use_cache)[0]

    # ---- introspection --------------------------------------------------

    def describe(self) -> dict[str, object]:
        """What was actually used, for the README and for `/healthz`."""
        return {
            "profile": self.profile.name,
            "model": self.profile.embedding_model,
            "dims": self.profile.dense_dims,
            "fp16": self.profile.use_fp16,
            "batch_size": self.profile.embed_batch_size,
            "query_prefix": self.profile.query_prefix or "(none)",
            "passage_prefix": self.profile.passage_prefix or "(none)",
            "cached_vectors": len(self._cache),
        }


def write_manifest(path: Path, embedder: Embedder, chunk_count: int) -> None:
    """Record what produced an index, next to the index.

    Asserted at startup so a profile switch fails loudly rather than at query
    time with a dimension mismatch.
    """
    path.write_text(
        json.dumps(
            {
                "collection": embedder.profile.collection_name,
                "model": embedder.profile.embedding_model,
                "dims": embedder.profile.dense_dims,
                "chunks": chunk_count,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
