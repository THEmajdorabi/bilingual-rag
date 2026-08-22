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
    "المائتان": 200,
    "المائتين": 200,
    "المئتان": 200,
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
_ARTICLE_WORD = normalize_ar("المادة")


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

    # Amendment text repeats the noun -- `المادة المادة الثالثة` -- because a
    # cross-reference is quoted inside a heading. Drop any leading copies.
    while normalized.startswith(_ARTICLE_WORD):
        normalized = normalized[len(_ARTICLE_WORD) :].strip()
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
        # "بعد المائة" with nothing before it means article 100 exactly.
        return hundreds or None

    # A bare hundred: `المادة المائة` is article 100, `المادة المائتان` is 200.
    # These carry no `بعد`, so they never reach the split above.
    bare_hundred = _HUNDREDS.get(_key(" ".join(tokens)))
    if bare_hundred is not None and not hundreds:
        return bare_hundred

    value = _parse_below_hundred(tokens)
    if value is None:
        return None
    return value + hundreds


def _parse_below_hundred(tokens: list[str]) -> int | None:
    """Parse the 1-99 part: a unit, a tens, a teen, or unit + و + tens.

    Everything is matched against the *space-stripped* form. Extraction breaks
    words at arbitrary points -- `الخامس ة عشرة` for `الخامسة عشرة`, `الأربع ون`
    for `الأربعون` -- and removing spaces before matching repairs all of those
    at once, instead of enumerating every place a break might land.
    """
    compact = _key(" ".join(tokens))
    if not compact:
        return None

    # Teen: <unit> + عشرة, e.g. الحادية عشرة = 11.
    # `العاشرة` (10) does not end in عشرة, so it cannot be misread as a teen.
    if compact.endswith(_TEEN):
        prefix = compact[: -len(_TEEN)]
        unit = _UNITS.get(prefix)
        if unit is not None:
            return 10 + unit
        return None

    # Bare tens or bare unit.
    if compact in _TENS:
        return _TENS[compact]
    if compact in _UNITS:
        return _UNITS[compact]

    # Compound: <unit> + و + <tens>, e.g. التاسعة والثلاثون = 39.
    # Tested suffix-first because `و` also occurs inside ordinary words, so
    # splitting on the letter itself would be wrong.
    for tens_key, tens_value in _TENS.items():
        suffix = "و" + tens_key
        if compact.endswith(suffix):
            unit = _UNITS.get(compact[: -len(suffix)])
            if unit is not None:
                return unit + tens_value

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
