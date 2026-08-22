"""Fusion, stemming and lexical tokenization.

All offline: no model, no Qdrant. The end-to-end retrieval quality numbers
live in the eval set; these are the properties that must hold regardless.
"""

from __future__ import annotations

import pytest

from bilingual_rag.arabic.normalize import normalize_ar
from bilingual_rag.arabic.stem import light_stem
from bilingual_rag.index.fusion import DEFAULT_K, rrf, rrf_ids
from bilingual_rag.index.lexical import tokenize

# ---------------------------------------------------------------------------
# Reciprocal rank fusion
# ---------------------------------------------------------------------------


def test_rrf_uses_rank_not_score() -> None:
    """RRF's whole point: it needs no comparable scale between retrievers.

    A cosine similarity of 0.82 and a BM25 score of 14.3 cannot be added. Only
    the positions are used.
    """
    fused = rrf([["a", "b"], ["a", "c"]])
    scores = dict(fused)

    # 'a' is rank 1 in both lists.
    assert scores["a"] == pytest.approx(2 / (DEFAULT_K + 1))
    # 'b' and 'c' are each rank 2 in one list.
    assert scores["b"] == pytest.approx(1 / (DEFAULT_K + 2))
    assert scores["c"] == pytest.approx(1 / (DEFAULT_K + 2))


def test_a_document_ranked_well_by_both_retrievers_wins() -> None:
    dense = ["x", "a", "b"]
    lexical = ["y", "a", "c"]

    assert rrf_ids([dense, lexical])[0] == "a"


def test_rrf_is_deterministic_on_ties() -> None:
    """An ablation table that shuffles between runs measures nothing."""
    rankings = [["b", "a"], ["a", "b"]]

    first = rrf_ids(rankings)
    second = rrf_ids(rankings)

    assert first == second
    # Equal scores break on the id, not on dict insertion order.
    assert first == ["a", "b"]


def test_rrf_handles_an_empty_ranking() -> None:
    assert rrf_ids([[], ["a"]]) == ["a"]
    assert rrf_ids([[], []]) == []


def test_rrf_limit() -> None:
    assert len(rrf_ids([["a", "b", "c", "d"]], limit=2)) == 2


def test_a_larger_k_flattens_the_advantage_of_rank_one() -> None:
    """k is the knob the ablation tunes, so its direction must be understood."""
    rankings = [["a", "b"]]
    tight = dict(rrf(rankings, k=1))
    loose = dict(rrf(rankings, k=1000))

    assert tight["a"] / tight["b"] > loose["a"] / loose["b"]


# ---------------------------------------------------------------------------
# Light stemming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("العمل", "عمل"),  # definite article stripped
        ("بالعمل", "عمل"),  # preposition + article
        ("والعمل", "عمل"),  # conjunction + article
        ("عمال", "عمال"),  # already short; left alone
    ],
)
def test_light_stem_strips_common_affixes(word: str, expected: str) -> None:
    assert light_stem(normalize_ar(word)) == expected


def test_light_stem_refuses_to_over_stem() -> None:
    """A stem below three letters has lost the word.

    In a citation system a false match is worse than a miss, because it
    produces an answer attached to the wrong article.
    """
    for short in ("عمل", "من", "في", "ال"):
        assert len(light_stem(short)) >= min(len(short), 3)


def test_light_stem_strips_at_most_one_prefix_and_one_suffix() -> None:
    """Repeated stripping collapses distinct legal terms onto each other."""
    stemmed = light_stem(normalize_ar("والعمال"))
    assert len(stemmed) >= 3


def test_stemming_makes_inflected_forms_match() -> None:
    """The reason BM25 needs a stemmer at all."""
    a = light_stem(normalize_ar("العمل"))
    b = light_stem(normalize_ar("بالعمل"))
    assert a == b


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------


def test_tokenize_finds_arabic_words() -> None:
    """A naive [a-z]+ pattern finds no Arabic at all."""
    tokens = tokenize("المادة الرابعة والسبعون", stem=False)
    assert len(tokens) == 3
    assert all(tokens)


def test_tokenize_drops_digits_and_punctuation() -> None:
    tokens = tokenize("المادة 74: إنهاء العقد.", stem=False)
    assert "74" not in tokens
    assert ":" not in tokens


def test_tokenize_handles_mixed_script() -> None:
    tokens = tokenize("المادة 74 states the rule", stem=False)
    assert "states" in tokens
    assert any("؀" <= c <= "ۿ" for t in tokens for c in t)


def test_tokenize_normalizes_before_splitting() -> None:
    """Index and query must fold identically, or retrieval degrades silently."""
    assert tokenize("الأجر", stem=False) == tokenize("الاجر", stem=False)


def test_tokenize_of_empty_text() -> None:
    assert tokenize("") == []
    assert tokenize("!!! 123 ???") == []
