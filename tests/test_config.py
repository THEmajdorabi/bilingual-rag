"""Hardware profiles.

The rules under test are the ones that fail silently when broken: a reranker
swapped for a monolingual model, a collection name shared between two vector
widths, and E5's mandatory prefixes going missing.
"""

from __future__ import annotations

import pytest

from bilingual_rag.config import (
    MAX_TOKENS_BY_MODEL,
    PROFILES,
    RERANKER,
    Profile,
    detect_profile,
)


def test_every_profile_uses_the_same_multilingual_reranker() -> None:
    """`bge-reranker-base` is English and Chinese only.

    On Arabic it reranks close to randomly, which would not error -- it would
    quietly poison the ablation table. Weaker hardware shrinks the candidate
    set instead, which is a latency choice, not a quality one.
    """
    for name, profile in PROFILES.items():
        assert profile.reranker_model == RERANKER, f"{name} swapped the reranker"
        assert "base" not in profile.reranker_model.rsplit("/", 1)[-1].replace("v2-m3", "")


def test_weaker_hardware_shrinks_the_candidate_set_not_the_model() -> None:
    order = ["cuda_large", "cuda_small", "mps", "cpu"]
    candidates = [PROFILES[name].rerank_candidates for name in order]  # type: ignore[index]

    assert candidates == sorted(candidates, reverse=True)
    assert all(c > 0 for c in candidates)


def test_collection_names_encode_model_and_dimension() -> None:
    """Two profiles produce different vector widths.

    Sharing a collection name across them fails at query time, or silently
    returns nonsense. Encoding the model and dimension makes them separate
    stores, so a profile switch fails loudly at startup instead.
    """
    names = {name: profile.collection_name for name, profile in PROFILES.items()}

    assert names["cuda_large"] == names["cuda_small"]  # same model, same width
    assert names["cpu"] != names["cuda_small"]
    assert "1024" in names["cuda_small"]
    assert "768" in names["cpu"]


def test_the_cpu_profile_carries_the_e5_prefixes() -> None:
    """E5 requires `query: ` and `passage: `.

    Getting this wrong is a silent halving of retrieval quality, not an error,
    so the prefixes travel with the model choice rather than living in the
    embedder where they could be forgotten.
    """
    cpu = PROFILES["cpu"]

    assert cpu.query_prefix == "query: "
    assert cpu.passage_prefix == "passage: "


def test_bge_profiles_use_no_prefixes() -> None:
    """BGE-M3 needs none, and adding them would degrade it."""
    for name in ("cuda_large", "cuda_small", "mps"):
        assert PROFILES[name].query_prefix == ""  # type: ignore[index]
        assert PROFILES[name].passage_prefix == ""  # type: ignore[index]


def test_context_limits_are_recorded_for_both_embedders() -> None:
    """E5 truncates at 512 against BGE-M3's 8192.

    On the CPU profile the heading path plus body must stay under that, so the
    limit has to be knowable from config rather than discovered by truncation.
    """
    assert MAX_TOKENS_BY_MODEL["intfloat/multilingual-e5-base"] == 512
    assert MAX_TOKENS_BY_MODEL["BAAI/bge-m3"] == 8192

    for profile in PROFILES.values():
        assert profile.embedding_model in MAX_TOKENS_BY_MODEL


def test_detect_profile_falls_back_to_cpu_without_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    """An absent GPU must never crash: the CPU profile is the one that always works."""
    import builtins

    real_import = builtins.__import__

    def no_torch(name: str, *args: object, **kwargs: object) -> object:
        if name == "torch":
            raise ImportError("torch is not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.delenv("BILINGUAL_RAG_PROFILE", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_torch)

    assert detect_profile() == "cpu"


def test_the_profile_can_be_forced_by_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Needed to measure every profile's throughput on one machine."""
    monkeypatch.setenv("BILINGUAL_RAG_PROFILE", "cpu")
    assert detect_profile() == "cpu"

    monkeypatch.setenv("BILINGUAL_RAG_PROFILE", "cuda_large")
    assert detect_profile() == "cuda_large"


def test_an_unknown_profile_override_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not select a profile that does not exist."""
    monkeypatch.setenv("BILINGUAL_RAG_PROFILE", "cuda_enormous")
    assert detect_profile() in PROFILES


def test_profile_is_a_frozen_shape() -> None:
    """Every field the pipeline reads must be present on every profile."""
    required = set(Profile.model_fields)
    for profile in PROFILES.values():
        assert set(profile.model_dump()) == required
