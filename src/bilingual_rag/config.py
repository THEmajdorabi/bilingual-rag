"""Settings and hardware profiles.

Hardware is not known at spec time, so the profile is detected at startup and
logged. Nothing here may crash because a GPU is absent: the CPU profile is the
fallback that keeps the demo runnable anywhere, and slower is fine while broken
is not.

The one rule that survives every profile: **the reranker never changes.**
`BAAI/bge-reranker-base` is English and Chinese only, and on Arabic it reranks
close to randomly, which would silently poison the ablation table. Weaker
hardware shrinks the *candidate set*; it never swaps in a monolingual model.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import structlog
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()
log = structlog.get_logger(__name__)

ProfileName = Literal["cuda_large", "cuda_small", "mps", "cpu"]


class Profile(BaseModel):
    """Everything that varies with the hardware."""

    name: ProfileName
    embedding_model: str
    dense_dims: int
    reranker_model: str
    rerank_candidates: int
    embed_batch_size: int
    rerank_batch_size: int
    use_fp16: bool
    # E5 models require "query: " / "passage: " prefixes. Getting this wrong
    # quietly halves retrieval quality, so it travels with the model choice
    # rather than living in the embedder.
    query_prefix: str = ""
    passage_prefix: str = ""
    # The CPU profile runs the reranker as an int8 ONNX export.
    reranker_onnx: bool = False

    @property
    def collection_name(self) -> str:
        """Collection names encode the model and dimension.

        Two profiles produce vectors of different width (1024 vs 768). Sharing
        one collection name across them means a profile switch fails at query
        time with a dimension error, or worse, silently returns nonsense. The
        name makes them separate stores.
        """
        slug = self.embedding_model.split("/")[-1].replace("-", "_").replace(".", "_").lower()
        return f"chunks__{slug}_{self.dense_dims}"

    @property
    def raw_collection_name(self) -> str:
        """Collection built from un-normalized text.

        Exists only so the ablation's "no normalization" row measures what it
        claims. Without a separately built index that row silently reports the
        normalized pipeline twice, which is how an ablation table ends up
        showing that a component is worth nothing.
        """
        return self.collection_name + "__raw"


# The reranker is identical across every profile. See the module docstring.
RERANKER = "BAAI/bge-reranker-v2-m3"

PROFILES: dict[ProfileName, Profile] = {
    "cuda_large": Profile(
        name="cuda_large",
        embedding_model="BAAI/bge-m3",
        dense_dims=1024,
        reranker_model=RERANKER,
        rerank_candidates=50,
        embed_batch_size=16,
        rerank_batch_size=32,
        use_fp16=True,
    ),
    "cuda_small": Profile(
        name="cuda_small",
        embedding_model="BAAI/bge-m3",
        dense_dims=1024,
        reranker_model=RERANKER,
        rerank_candidates=30,
        embed_batch_size=8,
        rerank_batch_size=16,
        use_fp16=True,
    ),
    "mps": Profile(
        name="mps",
        embedding_model="BAAI/bge-m3",
        dense_dims=1024,
        reranker_model=RERANKER,
        rerank_candidates=20,
        embed_batch_size=8,
        rerank_batch_size=8,
        use_fp16=False,
    ),
    "cpu": Profile(
        name="cpu",
        embedding_model="intfloat/multilingual-e5-base",
        dense_dims=768,
        reranker_model=RERANKER,
        rerank_candidates=20,
        embed_batch_size=4,
        rerank_batch_size=4,
        use_fp16=False,
        # Mandatory for E5. Omitting them is a silent quality loss, not an error.
        query_prefix="query: ",
        passage_prefix="passage: ",
        reranker_onnx=True,
    ),
}

# multilingual-e5-base truncates at 512 tokens against BGE-M3's 8192, so on the
# CPU profile the heading path plus chunk body must stay under that.
MAX_TOKENS_BY_MODEL = {
    "BAAI/bge-m3": 8192,
    "intfloat/multilingual-e5-base": 512,
}


def detect_profile() -> ProfileName:
    """Pick a profile from the hardware actually present.

    Never raises. An absent or unimportable torch means CPU, which is the
    profile that must always work.
    """
    override = os.environ.get("BILINGUAL_RAG_PROFILE")
    if override in PROFILES:
        log.info("profile.override", profile=override)
        return override  # type: ignore[return-value]

    try:
        import torch
    except ImportError:
        log.warning("profile.no_torch", chosen="cpu")
        return "cpu"

    if torch.cuda.is_available():
        vram_gb = torch.cuda.get_device_properties(0).total_memory / 1e9
        chosen: ProfileName = "cuda_large" if vram_gb >= 12 else "cuda_small"
        log.info(
            "profile.cuda",
            device=torch.cuda.get_device_name(0),
            vram_gb=round(vram_gb, 1),
            chosen=chosen,
        )
        return chosen

    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        log.info("profile.mps")
        return "mps"

    log.info("profile.cpu")
    return "cpu"


@lru_cache(maxsize=1)
def active_profile() -> Profile:
    """The profile for this process. Detected once, logged once."""
    profile = PROFILES[detect_profile()]
    log.info(
        "profile.selected",
        name=profile.name,
        embedding_model=profile.embedding_model,
        dims=profile.dense_dims,
        reranker=profile.reranker_model,
        candidates=profile.rerank_candidates,
        collection=profile.collection_name,
    )
    return profile


class RetrievalSettings(BaseModel):
    top_k: int = 10  # fixed across every ablation row, so recall@10 is comparable
    dense_top_k: int = 50
    bm25_top_k: int = 50
    rrf_k: int = 60


class QdrantSettings(BaseModel):
    url: str = "http://localhost:6333"
    # Embedded mode writes to a local directory and needs no server. Used by
    # tests and by anyone who cannot run Docker.
    local_path: str | None = None
    timeout_s: float = 30.0


class AnswerSettings(BaseModel):
    max_steps: int = 6
    max_cost_usd: float = 0.10
    refusal_threshold: float = 0.35


class ModelSpec(BaseModel):
    provider: str
    id: str
    max_tokens: int = 2048


class Source(BaseModel):
    """One document in the corpus."""

    path: str
    doc_id: str
    title: str = ""


class Settings(BaseModel):
    models: dict[str, ModelSpec] = Field(default_factory=dict)
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    answer: AnswerSettings = Field(default_factory=AnswerSettings)
    sources: list[Source] = Field(default_factory=list)
    chunks_path: str = "data/chunks.jsonl"


def config_path() -> Path:
    override = os.environ.get("BILINGUAL_RAG_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "config.yaml"


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    path = config_path()
    if not path.exists():
        return Settings()
    with path.open("r", encoding="utf-8") as handle:
        return Settings.model_validate(yaml.safe_load(handle) or {})


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in. "
            f"Keys are never read from config.yaml."
        )
    return value
