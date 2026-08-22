"""Arabic ordinal words to article numbers.

A wrong article number is worse than a missing one: it produces a citation
pointing at the wrong law. So the parser returns None rather than guessing, and
these tests check the None cases as carefully as the successes.
"""

from __future__ import annotations

import pytest

from bilingual_rag.arabic.ordinals import find_article_headings, parse_ordinal


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        # Units.
        ("الأولى", 1),
        ("الثانية", 2),
        ("التاسعة", 9),
        ("العاشرة", 10),
        # Teens: unit + عشرة.
        ("الحادية عشرة", 11),
        ("الثانية عشرة", 12),
        ("التاسعة عشرة", 19),
        # Bare tens.
        ("العشرون", 20),
        ("الثلاثون", 30),
        ("التسعون", 90),
        # Compounds: unit + و + tens.
        ("الحادية والعشرون", 21),
        ("التاسعة والثلاثون", 39),
        ("الخامسة والأربعون", 45),
        # Hundreds.
        ("المائة", 100),
        ("المائتان", 200),
        ("الأولى بعد المائة", 101),
        ("التاسعة والثلاثون بعد المائة", 139),
        ("الخامسة والأربعون بعد المائتين", 245),
    ],
)
def test_parse_ordinal(phrase: str, expected: int) -> None:
    assert parse_ordinal(phrase) == expected


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        # Extraction breaks words at arbitrary points. Matching on the
        # space-stripped form repairs all of these at once.
        ("الخامس ة عشرة", 15),
        ("الثان ية والثلاثون", 32),
        ("الأربع ون", 40),
        ("العاشر ة", 10),
    ],
)
def test_intra_word_spaces_from_extraction_are_tolerated(phrase: str, expected: int) -> None:
    assert parse_ordinal(phrase) == expected


def test_the_doubled_noun_in_amendment_text_is_stripped() -> None:
    """Amendments quote a cross-reference inside a heading: `المادة المادة الثالثة`."""
    assert parse_ordinal("المادة الثالثة") == 3
    assert parse_ordinal("المادة المادة الثالثة") == 3


@pytest.mark.parametrize(
    "phrase",
    [
        "",
        "   ",
        "عشرة",  # a bare teen marker is not an article number
        "وتكون بالنص الآتي",  # amendment prose, not a heading
        "على النحو الآتي",
        "الآتي",
        "بعد الشيء",  # 'after' followed by a non-hundred
    ],
)
def test_unparseable_phrases_return_none_rather_than_guessing(phrase: str) -> None:
    assert parse_ordinal(phrase) is None


def test_the_tenth_is_not_mistaken_for_a_teen() -> None:
    """`العاشرة` (10) must not be read as `<unit> عشرة`.

    It does not end in the teen marker, which is what keeps the two apart.
    """
    assert parse_ordinal("العاشرة") == 10
    assert parse_ordinal("الحادية عشرة") == 11


def test_hamza_and_taa_marbuta_spellings_agree() -> None:
    """The parser folds through the project's own normalizer.

    So an ordinal spelled without the hamza, as users routinely type it,
    resolves to the same number.
    """
    assert parse_ordinal("الأولى") == parse_ordinal("الاولى") == 1
    assert parse_ordinal("الثانية") == parse_ordinal("الثانيه") == 2


def test_find_article_headings_reports_unparsed_rather_than_dropping() -> None:
    text = "المادة الأولى: نص\nالمادة كلام غير مفهوم: نص آخر"
    headings = find_article_headings(text)

    assert len(headings) == 2
    assert headings[0][2:] == ("الأولى", "1")
    # The second is surfaced with None, so a caller can count failures.
    assert headings[1][3] is None


def test_a_cross_reference_inside_prose_is_not_a_heading() -> None:
    """Headings begin a line; cross-references never do.

    Amendment text is full of phrases like "as set out in article thirty-eight
    of this law". Matching those would insert spurious article boundaries and
    split a real article in half, so the pattern is anchored to line starts.
    """
    text = "لا يجوز مخالفة ما تضمنته المادة الثامنة والثلاثون من هذا النظام."

    assert find_article_headings(text) == []


def test_a_heading_at_the_start_of_a_line_is_found() -> None:
    text = "شيء ما ينتهي هنا.\nالمادة الثالثة: النص."
    headings = find_article_headings(text)

    assert len(headings) == 1
    assert headings[0][3] == "3"


def test_a_heading_without_a_colon_is_found() -> None:
    """The official text writes some headings with no colon at all."""
    text = "المادة الثانية\n تعديلات المادة\nنص المادة هنا."
    headings = find_article_headings(text)

    assert headings[0][3] == "2"


# ---------------------------------------------------------------------------
# The Implementing Regulations use a different numbering style
# ---------------------------------------------------------------------------


def test_digit_headings_from_the_regulations_are_found() -> None:
    """The Law spells its article numbers; the Regulations use digits.

    Both documents share one index, so both styles must parse. The Regulations
    write `المادة (23):` -- and PDF extraction of right-to-left text mirrors
    the brackets, so `)23(` is what actually arrives.
    """
    upright = "\n".join(["المادة (23):", "نص"])
    mirrored = "\n".join(["المادة )23(", "نص"])

    assert find_article_headings(upright)[0][3] == "23"
    assert find_article_headings(mirrored)[0][3] == "23"


def test_bis_articles_get_their_own_number() -> None:
    """`مكرر` marks an article inserted between two existing ones.

    Article 15 مكرر sits between 15 and 16 and is a distinct article, so it
    cannot collapse onto 15 -- a citation to one is not a citation to the other.
    """
    bis = "\n".join(["المادة )15( مكرر", "نص"])
    plain = "\n".join(["المادة )15(", "نص"])

    assert find_article_headings(bis)[0][3] == "15_bis"
    assert find_article_headings(plain)[0][3] == "15"


def test_arabic_indic_digits_in_headings_are_read() -> None:
    text = "\n".join(["المادة )٢٣(", "نص"])
    assert find_article_headings(text)[0][3] == "23"


def test_both_numbering_styles_come_back_in_document_order() -> None:
    """The chunker ends each article at the next heading, so order matters."""
    text = "\n".join(
        [
            "المادة الأولى: نص",
            "المادة )2(",
            "نص آخر",
            "المادة الثالثة: ثالث",
        ]
    )
    numbers = [h[3] for h in find_article_headings(text)]

    assert numbers == ["1", "2", "3"]
