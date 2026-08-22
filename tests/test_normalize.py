"""Arabic normalization, tested with real Arabic strings.

The invisible-character classes in `normalize.py` cannot be reviewed by
reading them, so they are reviewed here instead: every codepoint the module
claims to cover is asserted explicitly.
"""

from __future__ import annotations

import pytest

from bilingual_rag.arabic.normalize import (
    TASHKEEL,
    arabic_ratio,
    has_arabic,
    normalize_ar,
    strip_diacritics,
)

# ---------------------------------------------------------------------------
# Character-class coverage -- the review that reading the source cannot give
# ---------------------------------------------------------------------------


def test_diacritic_range_covers_every_codepoint() -> None:
    """U+064B-U+065F plus U+0670, wider than pyarabic's U+064B-U+0652."""
    for codepoint in range(0x064B, 0x0660):
        assert TASHKEEL.match(chr(codepoint)), f"U+{codepoint:04X} not matched"
    assert TASHKEEL.match("ٰ"), "superscript alef not matched"

    # Must not eat ordinary letters. U+0671 is alef wasla, a real letter.
    assert not TASHKEEL.match("ٱ")
    assert not TASHKEEL.match("ا")  # bare alef


def test_maddah_and_hamza_marks_are_stripped() -> None:
    """U+0653-U+0655 are outside pyarabic's TASHKEEL and do occur in legal text."""
    for mark in ("ٓ", "ٔ", "ٕ"):
        assert normalize_ar("م" + mark + "ا") == "ما"


# ---------------------------------------------------------------------------
# The properties the spec names
# ---------------------------------------------------------------------------


def test_the_same_word_with_and_without_diacritics_normalizes_identically() -> None:
    vocalised = "الأَجْرُ الأَسَاسِيُّ"
    plain = "الأجر الأساسي"
    assert normalize_ar(vocalised) == normalize_ar(plain)


def test_all_four_alef_forms_collapse() -> None:
    forms = ["أحمد", "إحمد", "آحمد", "ٱحمد"]
    normalized = {normalize_ar(f) for f in forms}
    assert len(normalized) == 1, f"alef forms did not collapse: {normalized}"
    assert normalized.pop().startswith("ا")


def test_arabic_indic_and_ascii_digits_match() -> None:
    assert normalize_ar("المادة ٧٤") == normalize_ar("المادة 74")
    # Eastern Arabic-Indic (Persian/Urdu) digits too.
    assert normalize_ar("۷۴") == "74"


def test_tatweel_is_stripped() -> None:
    """Kashida is justification padding and appears mid-word in legal PDFs."""
    assert normalize_ar("الــــعمل") == normalize_ar("العمل")


def test_english_survives_unchanged_in_mixed_text() -> None:
    mixed = "المادة 74 states the notice period"
    result = normalize_ar(mixed)
    assert "states the notice period" in result


def test_normalization_is_idempotent() -> None:
    samples = [
        "الأَجْرُ الأَسَاسِيُّ",
        "المادة ٧٤: مدة الإشعار",
        "mixed العربية and English 123",
        "الــــعمل",
    ]
    for sample in samples:
        once = normalize_ar(sample)
        assert normalize_ar(once) == once, f"not idempotent: {sample!r}"


def test_empty_and_whitespace() -> None:
    assert normalize_ar("") == ""
    assert normalize_ar("   \n\t  ") == ""


# ---------------------------------------------------------------------------
# Configurable, lossy folds
# ---------------------------------------------------------------------------


def test_alef_maksura_fold_is_configurable() -> None:
    assert normalize_ar("على", fold_alef_maksura=True) == "علي"
    assert normalize_ar("على", fold_alef_maksura=False) == "على"


def test_taa_marbuta_fold_is_configurable() -> None:
    assert normalize_ar("مدة", fold_taa_marbuta=True) == "مده"
    assert normalize_ar("مدة", fold_taa_marbuta=False) == "مدة"


def test_aggressive_folds_hamza_carriers_and_is_off_by_default() -> None:
    assert normalize_ar("مسؤول", aggressive=True) == "مسوول"
    assert normalize_ar("مسؤول") == "مسؤول"


# ---------------------------------------------------------------------------
# Presentation forms -- the corpus-specific trap
# ---------------------------------------------------------------------------


def test_presentation_forms_become_standard_letters() -> None:
    """The corpus PDF stores text in U+FB50-U+FEFF, not ordinary Arabic.

    Without NFKC the presentation-form spelling and the ordinary spelling are
    different strings and never match. This is measured at 2,107 such
    characters per page on the real document.
    """
    presentation = "ﻫﻟﻣﺎﺪﺓ"  # article, in presentation forms
    assert not any("ﭐ" <= c <= "﻿" for c in normalize_ar(presentation))


def test_lam_alef_ligature_expands() -> None:
    """U+FEFB is a single codepoint that must become two letters."""
    assert normalize_ar("ﻻ") == normalize_ar("لا")


def test_invisible_direction_marks_are_removed() -> None:
    """PDF extraction of bidirectional text sprinkles these through the string."""
    dirty = "المادة‏ 74‫ test⁦"
    assert "‏" not in normalize_ar(dirty)
    assert "‫" not in normalize_ar(dirty)
    assert "⁦" not in normalize_ar(dirty)


def test_arabic_punctuation_maps_to_ascii() -> None:
    assert normalize_ar("نعم، لا؛ ماذا؟") == "نعم, لا; ماذا?"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_strip_diacritics_keeps_everything_else() -> None:
    """Display path: remove marks without folding letters."""
    assert strip_diacritics("الأَجْرُ") == "الأجر"
    # Unlike normalize_ar, the hamza is preserved.
    assert "أ" in strip_diacritics("الأَجْرُ")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("العمل", True),
        ("hello", False),
        ("hello العمل", True),
        ("123", False),
        ("", False),
    ],
)
def test_has_arabic(text: str, expected: bool) -> None:
    assert has_arabic(text) is expected


def test_arabic_ratio() -> None:
    assert arabic_ratio("العمل") == 1.0
    assert arabic_ratio("hello") == 0.0
    assert arabic_ratio("") == 0.0
    # Digits and punctuation are not letters and must not skew the ratio.
    assert arabic_ratio("المادة 74!") == 1.0
    assert 0.4 < arabic_ratio("عمل work") < 0.6
