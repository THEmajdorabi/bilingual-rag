"""Structure-aware chunking.

The properties that matter are: an article is not split from its number, a
chunk is never cut mid-sentence, and the heading path reaches the embedding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bilingual_rag.ingest.chunk import (
    chunk_document,
    clean_article_text,
    detect_language,
    size_histogram,
    split_sentences,
)
from bilingual_rag.ingest.extract import Document, Page

CORPUS = Path(__file__).resolve().parents[1] / "data" / "boe_labor_law.html"


def make_document(text: str, doc_id: str = "test_doc") -> Document:
    return Document(doc_id=doc_id, source_path=Path("test"), pages=[Page(number=1, text=text)])


SAMPLE = """الباب الأول: التعريفات والأحكام العامة
الفصل الأول: التعريفات
المادة الأولى :
يسمى هذا النظام نظام العمل.
المادة الثانية :
يقصد بالألفاظ والعبارات الآتية المعاني المبينة أمامها ما لم يقتض السياق خلاف ذلك.
المادة الثالثة :
العمل حق للمواطن لا يجوز لغيره ممارسته إلا بعد توافر الشروط المنصوص عليها.
"""


def test_one_article_becomes_one_chunk() -> None:
    chunks = chunk_document(make_document(SAMPLE))

    assert len(chunks) == 3
    assert [c.article_no for c in chunks] == ["1", "2", "3"]


def test_heading_path_carries_chapter_and_section() -> None:
    chunks = chunk_document(make_document(SAMPLE))

    assert chunks[0].heading_path == [
        "الباب الأول",
        "الفصل الأول",
        "المادة 1",
    ]


def test_the_heading_path_reaches_the_embedded_text() -> None:
    """The prefix is the point: an isolated article lacks topical context."""
    chunks = chunk_document(make_document(SAMPLE))
    embedded = chunks[0].embedding_text()

    assert embedded.startswith("الباب الأول > الفصل الأول > المادة 1")
    assert chunks[0].text_norm in embedded


def test_display_text_and_index_text_are_kept_separately() -> None:
    """Citations quote the original; only `text_norm` is folded."""
    chunks = chunk_document(make_document(SAMPLE))
    chunk = chunks[0]

    assert "المادة الأولى" in chunk.text  # original spelling, hamza intact
    assert chunk.text_norm != chunk.text  # normalized differs
    assert "الماده" in chunk.text_norm  # taa marbuta folded in the index copy


def test_a_short_article_survives_the_size_floor() -> None:
    """Article 1 is one short sentence and is still a real article.

    The floor measures the body, not the heading, so a brief article is kept
    while a heading with no body at all is dropped.
    """
    chunks = chunk_document(make_document(SAMPLE))
    first = next(c for c in chunks if c.article_no == "1")

    assert "يسمى هذا النظام نظام العمل" in first.text


def test_headings_with_no_body_are_dropped() -> None:
    """The document has a table-of-contents region of consecutive headings."""
    toc = "المادة الأولى :\nالمادة الثانية :\nالمادة الثالثة :\n"
    assert chunk_document(make_document(toc)) == []


def test_a_long_article_splits_at_sentence_boundaries() -> None:
    sentence = "هذا نص طويل جدا يتكرر لاختبار التقسيم عند حدود الجمل. "
    body = sentence * 60
    document = make_document(f"المادة الأولى :\n{body}")

    chunks = chunk_document(document, max_chars=500)

    assert len(chunks) > 1
    # Every piece must end at a sentence terminator, never mid-clause.
    for chunk in chunks[:-1]:
        assert chunk.text.rstrip().endswith("."), f"cut mid-sentence: {chunk.text[-40:]!r}"


def test_every_piece_of_a_split_article_keeps_the_article_number() -> None:
    """Losing the number on continuation chunks would break citation."""
    body = "جملة اختبار للتقسيم الطويل جدا هنا. " * 80
    chunks = chunk_document(make_document(f"المادة الرابعة :\n{body}"), max_chars=400)

    assert len(chunks) > 1
    assert {c.article_no for c in chunks} == {"4"}


def test_split_pieces_overlap_by_one_sentence() -> None:
    """A fact spanning the split must be retrievable from either side."""
    sentences = [f"الجملة رقم {i} في هذه المادة الطويلة." for i in range(40)]
    chunks = chunk_document(make_document("المادة الأولى :\n" + " ".join(sentences)), max_chars=300)

    assert len(chunks) > 1
    first_tail = split_sentences(chunks[0].text)[-1]
    assert first_tail in chunks[1].text


def test_chunk_ids_are_stable_across_runs() -> None:
    """Content-hashed ids make re-ingestion of unchanged text a no-op."""
    first = chunk_document(make_document(SAMPLE))
    second = chunk_document(make_document(SAMPLE))

    assert [c.id for c in first] == [c.id for c in second]


def test_doubled_heading_from_extraction_is_removed() -> None:
    """The HTML emits the noun in one element and the ordinal in the next."""
    assert clean_article_text("المادة\nالمادة الثانية : نص").startswith("المادة الثانية")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("هذا نص عربي بالكامل", "ar"),
        ("this is entirely english", "en"),
        ("المادة 74 states the notice period rule clearly", "mixed"),
    ],
)
def test_detect_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_size_histogram_handles_an_empty_corpus() -> None:
    assert size_histogram([]) == "(no chunks)"


# ---------------------------------------------------------------------------
# Against the real corpus
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(not CORPUS.exists(), reason="corpus not downloaded")
def test_the_real_corpus_yields_every_article() -> None:
    """The headline ingestion number, asserted rather than eyeballed.

    Saudi Labor Law has 245 articles. Anything less means the chunker lost
    one, and a lost article is a question the system can never answer.
    """
    from bilingual_rag.ingest.extract import extract

    chunks = chunk_document(extract(CORPUS, doc_id="saudi_labor_law"))
    articles = {int(c.article_no) for c in chunks if c.article_no}

    assert articles == set(range(1, 246)), (
        f"missing articles: {sorted(set(range(1, 246)) - articles)}"
    )
    assert all(c.heading_path for c in chunks), "a chunk lost its heading path"
