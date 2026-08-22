"""Light Arabic stemming, for BM25 only. Never for display.

Arabic is heavily affixed: `العمل`, `بالعمل`, `وللعمل`, `أعمال` are all forms
around one root. BM25 matches strings, so without stemming a query using one
form misses documents using another.

Two options were on the table, and the spec is explicit that the winner should
be measured rather than assumed:

  * **Light stemming** (this module): strip a small set of known prefixes and
    suffixes, and only when the remainder is still a plausible word.
  * **ISRI root stemmer** (`nltk.stem.isri`): aggressive root extraction.

`scripts/ablate_retrieval.py` measures both on the eval set. The expectation is
that ISRI over-stems legal terminology -- it reduces distinct legal terms to a
shared three-letter root, which destroys the precision that makes a citation
correct rather than merely topical -- but the number decides, not the
expectation.
"""

from __future__ import annotations

# Prefixes, longest first so `وال` is tried before `ال` and `و`.
# `ال` is the definite article; the single letters are conjunctions and
# prepositions that attach to the following word.
PREFIXES = (
    "وال",
    "بال",
    "كال",
    "فال",
    "لل",
    "ال",
    "و",
    "ب",
    "ك",
    "ف",
    "ل",
)

# Suffixes, longest first. Plurals, duals, and attached pronouns.
SUFFIXES = (
    "اتها",
    "اتهم",
    "ونها",
    "ينها",
    "هما",
    "كما",
    "هن",
    "ها",
    "هم",
    "كم",
    "نا",
    "ات",
    "ون",
    "ين",
    "ان",
    "ه",
    "ة",
    "ي",
)

# A stem shorter than this is almost certainly over-stemmed. Arabic roots are
# three letters, so anything below three has lost the word.
MIN_STEM_LENGTH = 3


def light_stem(word: str) -> str:
    """Strip at most one prefix and one suffix, if the remainder survives.

    Conservative on purpose. Stripping repeatedly, or stripping down to a root,
    collapses distinct legal terms onto each other -- and in a citation system
    a false match is worse than a miss, because it produces an answer attached
    to the wrong article.
    """
    if len(word) <= MIN_STEM_LENGTH:
        return word

    stem = word
    for prefix in PREFIXES:
        if stem.startswith(prefix) and len(stem) - len(prefix) >= MIN_STEM_LENGTH:
            stem = stem[len(prefix) :]
            break

    for suffix in SUFFIXES:
        if stem.endswith(suffix) and len(stem) - len(suffix) >= MIN_STEM_LENGTH:
            stem = stem[: -len(suffix)]
            break

    return stem


def stem_tokens(tokens: list[str]) -> list[str]:
    return [light_stem(token) for token in tokens]


def isri_stem(word: str) -> str:
    """ISRI root stemmer, for the comparison in the ablation.

    Imported lazily because nltk is not a runtime dependency -- it is only
    needed to produce the measurement that justifies not using it.
    """
    from nltk.stem.isri import ISRIStemmer

    return ISRIStemmer().stem(word)
