"""Arabic normalization: the module that decides whether this project works.

Arabic writes the same word many ways. Without normalization, dense retrieval
is mediocre and BM25 is close to useless, because `الأجر` and `الاجر` and
`الأَجْر` are three different strings for one word.

Two rules govern everything here:

  1. **The same function normalizes both the indexed text and the query.** A
     mismatch is a silent killer: retrieval quietly degrades and nothing
     errors. `tests/test_normalize.py` enforces it.
  2. **The original text is always kept alongside the normalized text.**
     Citations quote the original, with diacritics intact. The user never sees
     normalized text.

Measured on the real corpus (see DECISIONS.md): NFKC alone converts 2,107
Arabic Presentation Form characters per page into standard Arabic. That step is
not optional for this PDF, without it, `ﺍﻟﻤﺎﺩﺓ` and `المادة` never match.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------

# Diacritics (tashkeel). Deliberately wider than pyarabic's TASHKEEL constant,
# which covers only U+064B-U+0652:
#   U+064B-U+0652  fathatan..sukun          (the common marks)
#   U+0653-U+0655  maddah, hamza above/below (occur in vocalised legal text)
#   U+0656-U+065F  extended Quranic marks
#   U+0670         superscript alef          (outside the contiguous block)
# The class below is a literal character range, and these characters are
# invisible in an editor, so the codepoints are spelled out above, and
# `test_diacritic_range_covers_every_codepoint` asserts the range matches all
# of U+064B-U+065F plus U+0670. Review the test, not the glyphs.
TASHKEEL = re.compile("[ً-ٰٟ]")

TATWEEL = "ـ"  # kashida: pure justification padding, never semantic

# Alef forms: hamza-above, hamza-below, madda, wasla -> bare alef.
ALEF_FORMS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا"})

# Arabic-Indic (U+0660-0669) and Eastern Arabic-Indic (U+06F0-06F9) -> ASCII.
DIGITS = str.maketrans(
    {chr(0x0660 + i): str(i) for i in range(10)} | {chr(0x06F0 + i): str(i) for i in range(10)}
)

# Arabic punctuation -> ASCII equivalents, so a query typed with an ASCII
# comma matches a document using U+060C.
PUNCTUATION = str.maketrans({"،": ",", "؛": ";", "؟": "?", "٫": ".", "٬": ","})

# Lossy folds, applied only when the caller asks for them.
ALEF_MAKSURA = str.maketrans({"ى": "ي"})  # ى -> ي
TAA_MARBUTA = str.maketrans({"ة": "ه"})  # ة -> ه
HAMZA_CARRIERS = str.maketrans({"ؤ": "و", "ئ": "ي"})  # ؤ -> و, ئ -> ي

# Zero-width and directional marks. PDF extraction sprinkles these through
# bidirectional text; they are invisible and break exact matching.
INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")

WHITESPACE = re.compile(r"\s+")


def normalize_ar(
    text: str,
    *,
    aggressive: bool = False,
    fold_alef_maksura: bool = True,
    fold_taa_marbuta: bool = True,
) -> str:
    """Normalize Arabic text for indexing and querying.

    Steps run in a fixed order, because several depend on earlier ones, NFKC
    must come first so that presentation forms become ordinary letters before
    anything tries to match them.

    Args:
        text: raw text, typically straight from PDF extraction.
        aggressive: also fold hamza carriers (ؤ -> و, ئ -> ي). Off by default:
            it raises recall and costs precision, so it is measured, not assumed.
        fold_alef_maksura: fold ى -> ي. Lossy but almost always helps, because
            the two are routinely confused in typed Arabic.
        fold_taa_marbuta: fold ة -> ه. Same reasoning.

    Returns:
        Normalized text. Never shown to a user: see the module docstring.
    """
    if not text:
        return ""

    # 1. NFKC. Converts Arabic Presentation Forms (U+FB50-U+FEFF) to standard
    #    letters, including the ligature ﻻ -> لا. On this corpus that is the
    #    single highest-impact step.
    out = unicodedata.normalize("NFKC", text)

    # 2. Remove invisible directional and zero-width marks.
    out = INVISIBLE.sub("", out)

    # 3. Strip diacritics.
    out = TASHKEEL.sub("", out)

    # 4. Strip tatweel. Legal PDFs use it to justify lines, so it appears
    #    mid-word and would otherwise split tokens.
    out = out.replace(TATWEEL, "")

    # 5. Unify alef forms.
    out = out.translate(ALEF_FORMS)

    # 6-7. Optional lossy folds.
    if fold_alef_maksura:
        out = out.translate(ALEF_MAKSURA)
    if fold_taa_marbuta:
        out = out.translate(TAA_MARBUTA)
    if aggressive:
        out = out.translate(HAMZA_CARRIERS)

    # 8. Digits to ASCII. The corpus writes article numbers in Arabic-Indic
    #    digits while users type ASCII ones.
    out = out.translate(DIGITS)

    # 9. Punctuation to ASCII equivalents.
    out = out.translate(PUNCTUATION)

    # 10. Collapse whitespace. PDF extraction leaves ragged runs of newlines
    #     and spaces from column layout.
    out = WHITESPACE.sub(" ", out).strip()

    return out


def strip_diacritics(text: str) -> str:
    """Remove diacritics only, preserving everything else.

    Used for display when a passage should read cleanly without being folded
    into index form.
    """
    return TASHKEEL.sub("", unicodedata.normalize("NFKC", text))


def has_arabic(text: str) -> bool:
    """True when the text contains at least one Arabic-script character."""
    return any("؀" <= character <= "ۿ" for character in text)


def arabic_ratio(text: str) -> float:
    """Fraction of letter characters that are Arabic script.

    Used by language detection and by the ingest pipeline to tag a chunk as
    Arabic, English or mixed.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    arabic = sum(1 for c in letters if "؀" <= c <= "ۿ")
    return arabic / len(letters)
