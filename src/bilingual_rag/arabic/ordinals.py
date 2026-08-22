"""Arabic ordinal words to integers.

The spec assumed article headings look like `المادة 74`. The real corpus
contains no digit-numbered articles at all -- all 239 are spelled out, and the
larger ones are compounds:

    المادة التاسعة والثلاثون              -> 39   (ninth and-thirty)
    المادة التاسعة والثلاثون بعد المائة   -> 139  (... after the hundred)

Without this parser every chunk would carry `article_no = None`, and citations
-- the feature the project exists to demonstrate -- would have nothing to cite.

Arabic ordinals compose in a fixed pattern:

    [unit] [و + tens] [بعد المائة | بعد المائتين | بعد الثلاثمائة ...]
      9         30              +100                                  = 139

Two extraction artifacts have to be tolerated. PDF text layers insert spurious
spaces inside words (`الخامس ة عشرة` for `الخامسة عشرة`), and the same ordinal
appears with and without the definite article. Both are handled by normalizing
the phrase before matching rather than by multiplying the pattern list.
"""

from __future__ import annotations

import re

from bilingual_rag.arabic.normalize import normalize_ar

# Feminine ordinals, which is the form used with المادة (a feminine noun).
# Keys are stored already normalized -- see `_key` -- so lookups are robust to
# hamza spelling, taa marbuta and alef maksura.
UNITS: dict[str, int] = {
    "الأولى": 1,
    "الحادية": 1,  # only appears in compounds: الحادية والعشرون = 21
    "الثانية": 2,
    "الثالثة": 3,
    "الرابعة": 4,
    "الخامسة": 5,
    "السادسة": 6,
    "السابعة": 7,
    "الثامنة": 8,
    "التاسعة": 9,
    "العاشرة": 10,
}

TENS: dict[str, int] = {
    "العشرون": 20,
    "العشرين": 20,
    "الثلاثون": 30,
    "الثلاثين": 30,
    "الأربعون": 40,
    "الأربعين": 40,
    "الخمسون": 50,
    "الخمسين": 50,
    "الستون": 60,
    "الستين": 60,
    "السبعون": 70,
    "السبعين": 70,
    "الثمانون": 80,
    "الثمانين": 80,
    "التسعون": 90,
    "التسعين": 90,
}

# `عشرة` after a unit makes a teen: الحادية عشرة = 11 ... التاسعة عشرة = 19.
TEEN_MARKER = "عشرة"

# Hundreds, expressed as "after the N hundred".
HUNDREDS: dict[str, int] = {
    "المائة": 100,
    "المئة": 100,
    "المائتين": 200,
    "المئتين": 200,
    "الثلاثمائة": 300,
    "الثلاثمئة": 300,
    "الأربعمائة": 400,
    "الأربعمئة": 400,
    "الخمسمائة": 500,
    "الخمسمئة": 500,
}

AFTER = "بعد"  # "after", the connector before a hundreds term
AND = "و"  # conjunction joining unit and tens


def _key(word: str) -> str:
    """Normalize one ordinal word for dictionary lookup.

    Uses the project's single `normalize_ar`, so an ordinal is folded exactly
    the way indexed text is. Spaces are then removed entirely, which repairs
    the intra-word breaks the PDF text layer introduces.
    """
    return normalize_ar(word).replace(" ", "")


# Lookup tables keyed on normalized forms, built once at import.
_UNITS = {_key(k): v for k, v in UNITS.items()}
_TENS = {_key(k): v for k, v in TENS.items()}
_HUNDREDS = {_key(k): v for k, v in HUNDREDS.items()}
_TEEN = _key(TEEN_MARKER)
_AFTER = _key(AFTER)


def parse_ordinal(phrase: str) -> int | None:
    """Convert an Arabic ordinal phrase to an integer, or None if unparseable.

    Examples:
        الأولى                          -> 1
        الحادية عشرة                    -> 11
        التاسعة والثلاثون               -> 39
        التاسعة والثلاثون بعد المائة    -> 139
        الثلاثون                        -> 30

    Returns None rather than guessing. A wrong article number is worse than a
    missing one: it would produce a citation pointing at the wrong law.
    """
    if not phrase:
        return None

    normalized = normalize_ar(phrase)
    if not normalized:
        return None

    # Split the hundreds suffix off first, so the remainder is a plain 1-99.
    hundreds = 0
    if _AFTER in normalized.replace(" ", ""):
        parts = re.split(r"\s*بعد\s*", normalized, maxsplit=1)
        if len(parts) == 2:
            base_text, hundreds_text = parts
            hundreds_key = _key(hundreds_text)
            if hundreds_key not in _HUNDREDS:
                return None
            hundreds = _HUNDREDS[hundreds_key]
            normalized = base_text

    tokens = [t for t in normalized.split() if t]
    if not tokens:
        # "بعد المائة" alone means article 100 exactly.
        return hundreds or None

    value = _parse_below_hundred(tokens)
    if value is None:
        return None
    return value + hundreds


def _parse_below_hundred(tokens: list[str]) -> int | None:
    """Parse the 1-99 part: a unit, a tens, a teen, or unit + و + tens."""
    # Rejoin then re-split on the conjunction, because the PDF sometimes
    # separates `و` from the word it attaches to.
    joined = " ".join(tokens)

    # Teen: <unit> عشرة
    keys = [_key(t) for t in tokens]
    if _TEEN in keys:
        index = keys.index(_TEEN)
        if index == 0:
            return None  # "عشرة" alone is not an ordinal article number
        unit = _UNITS.get(keys[index - 1])
        if unit is None:
            return None
        # العاشرة is 10 on its own; a teen is 10 + unit.
        return 10 + unit

    # Compound: <unit> و<tens>
    match = re.match(r"^(.*?)\s*و\s*(\S+)$", joined)
    if match:
        unit_key, tens_key = _key(match.group(1)), _key(match.group(2))
        unit, tens = _UNITS.get(unit_key), _TENS.get(tens_key)
        if unit is not None and tens is not None:
            return unit + tens

    # Bare tens, or a bare unit.
    single = _key(joined)
    if single in _TENS:
        return _TENS[single]
    if single in _UNITS:
        return _UNITS[single]

    return None


# `المادة` followed by an ordinal phrase, ending at a colon.
# The phrase is length-bounded and forbids a full stop, so a heading cannot
# swallow the sentence that precedes it.
ARTICLE_HEADING = re.compile(r"المادة\s+([^:.\n]{2,60}?)\s*:")


def find_article_headings(text: str) -> list[tuple[int, int, str, int | None]]:
    """Locate every article heading in `text`.

    Returns (start, end, phrase, article_number) per match, in document order.
    `article_number` is None when the phrase could not be parsed, so the caller
    can count and report failures rather than silently dropping articles.
    """
    results = []
    for match in ARTICLE_HEADING.finditer(text):
        phrase = match.group(1).strip()
        results.append((match.start(), match.end(), phrase, parse_ordinal(phrase)))
    return results
