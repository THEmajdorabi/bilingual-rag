"""Structure-aware chunking.

Fixed-size character windows destroy legal documents. A 500-character window
cuts an article in half, and the half that answers the question loses the
article number that would let you cite it.

Legal text is unusually regular, so chunking follows its structure instead:

    الباب  (chapter)  >  الفصل  (section)  >  المادة  (article)

One article is one chunk whenever it fits the budget. Articles longer than the
budget split at sentence boundaries, never mid-sentence, with one sentence of
overlap so a fact spanning the split is retrievable from either side.

Every chunk carries its heading path as a prefix in the embedded text:

    الباب الثالث > الفصل الأول > المادة 74

That prefix is the detail with the largest effect on retrieval per line of
code: it gives the embedding topical context an isolated article lacks, and it
makes the reader's job easier too.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from bilingual_rag.arabic.normalize import arabic_ratio, normalize_ar
from bilingual_rag.arabic.ordinals import find_article_headings
from bilingual_rag.ingest.extract import Document

log = structlog.get_logger(__name__)

# Target chunk size in characters. Arabic runs roughly 2 characters per token
# on current tokenizers, so 200-400 tokens is about 400-800 characters. The
# budget is the upper end; the measured distribution is reported by
# scripts/ingest.py.
MAX_CHUNK_CHARS = 1400
MIN_CHUNK_CHARS = 20

# Chapter and section headings. Both are followed by an ordinal and a colon.
CHAPTER = re.compile(r"الباب\s+([^:.\n]{2,60}?)\s*:")
SECTION = re.compile(r"الفصل\s+([^:.\n]{2,60}?)\s*:")

# Arabic sentences end at these. A colon also ends a clause in legal text and
# is a safe split point, but only when it does not immediately precede an
# enumerated list, which is why the split keeps the delimiter attached.
SENTENCE_END = re.compile(r"(?<=[.!?؟])\s+")


class Chunk(BaseModel):
    """One retrievable unit.

    Two text fields, deliberately. `text` is the original, shown to the user
    and quoted in citations with its diacritics intact. `text_norm` is folded
    for indexing and is never displayed.
    """

    id: str
    doc_id: str
    text: str
    text_norm: str
    heading_path: list[str] = Field(default_factory=list)
    article_no: str | None = None
    page: int = 1
    span: tuple[int, int] = (0, 0)
    lang: Literal["ar", "en", "mixed"] = "ar"
    ocr_confidence: float | None = None

    def embedding_text(self) -> str:
        """What actually gets embedded: heading path, then the body.

        The path is included because an article retrieved without its chapter
        context is much harder for both the embedder and the reader.
        """
        if not self.heading_path:
            return self.text_norm
        return " > ".join(self.heading_path) + "\n" + self.text_norm


def detect_language(text: str) -> Literal["ar", "en", "mixed"]:
    """Classify a chunk by script mix."""
    ratio = arabic_ratio(text)
    if ratio >= 0.85:
        return "ar"
    if ratio <= 0.15:
        return "en"
    return "mixed"


def split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping the terminator on the sentence."""
    parts = [p.strip() for p in SENTENCE_END.split(text)]
    return [p for p in parts if p]


# A bare `المادة` on its own line, immediately before the real heading. The
# HTML puts the noun in one element and the ordinal in the next, so extraction
# emits it twice. Cosmetic in the index, but citations quote `text` verbatim,
# so it would be visible to the user.
DOUBLED_HEADING = re.compile(r"^\s*المادة\s*\n\s*(?=المادة\b)")


def clean_article_text(text: str) -> str:
    """Remove extraction artifacts from the start of an article."""
    return DOUBLED_HEADING.sub("", text).strip()


def chunk_id(doc_id: str, start: int, text: str) -> str:
    """Stable id from content, so re-ingesting unchanged text is a no-op."""
    digest = hashlib.sha256(f"{doc_id}:{start}:{text}".encode()).hexdigest()
    return f"{doc_id}_{digest[:16]}"


def _headings_before(text: str, position: int, pattern: re.Pattern[str]) -> str | None:
    """The most recent heading of `pattern` at or before `position`."""
    latest = None
    for match in pattern.finditer(text, 0, position + 1):
        latest = match
    if latest is None:
        return None
    return f"{latest.group(0).rstrip(':').strip()}"


def _split_long_article(
    body: str, budget: int, overlap_sentences: int = 1
) -> list[tuple[int, str]]:
    """Split an over-long article at sentence boundaries.

    Returns (offset_within_body, text) so the caller can keep real spans.
    Never splits mid-sentence: a legal clause cut in half is worse than a
    slightly oversized chunk.
    """
    sentences = split_sentences(body)
    if not sentences:
        return [(0, body)]

    pieces: list[tuple[int, str]] = []
    current: list[str] = []
    current_len = 0
    search_from = 0
    start_offset = 0

    for sentence in sentences:
        # Track the true offset of each sentence inside the body.
        found = body.find(sentence, search_from)
        if found == -1:
            found = search_from
        search_from = found + len(sentence)

        if current and current_len + len(sentence) > budget:
            pieces.append((start_offset, " ".join(current)))
            # Carry the tail sentences forward as overlap.
            carried = current[-overlap_sentences:] if overlap_sentences else []
            current = [*carried, sentence]
            current_len = sum(len(c) for c in current)
            start_offset = found - sum(len(c) + 1 for c in carried)
        else:
            if not current:
                start_offset = found
            current.append(sentence)
            current_len += len(sentence) + 1

    if current:
        pieces.append((max(0, start_offset), " ".join(current)))

    return pieces


def chunk_document(
    document: Document,
    max_chars: int = MAX_CHUNK_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[Chunk]:
    """Split a document into article-aligned chunks with heading paths."""
    chunks: list[Chunk] = []

    for page in document.pages:
        text = page.text
        headings = find_article_headings(text)
        # Keep only headings that resolved to a number: the rest are
        # cross-references inside amendment prose, not article starts.
        starts = [(h[0], h[1], h[3]) for h in headings if h[3] is not None]

        if not starts:
            log.warning("chunk.no_articles_found", doc_id=document.doc_id, page=page.number)
            continue

        for index, (start, heading_end, article_no) in enumerate(starts):
            end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
            article_text = clean_article_text(text[start:end])

            # The floor applies to the *body*, not the whole chunk. The
            # document has a table-of-contents region where headings appear
            # consecutively with nothing after them; those must be dropped.
            # But article 1 is a single short sentence ("this law is called the
            # Labor Law"), and measuring the heading plus body would discard a
            # real article for being brief.
            body = article_text[heading_end - start :].strip()
            if len(body) < min_chars:
                continue

            chapter = _headings_before(text, start, CHAPTER)
            section = _headings_before(text, start, SECTION)
            path = [p for p in (chapter, section, f"المادة {article_no}") if p]

            body_offset = 0
            pieces = (
                [(0, article_text)]
                if len(article_text) <= max_chars
                else _split_long_article(article_text, max_chars)
            )

            for body_offset, piece in pieces:
                absolute_start = start + body_offset
                chunks.append(
                    Chunk(
                        id=chunk_id(document.doc_id, absolute_start, piece),
                        doc_id=document.doc_id,
                        text=piece,
                        text_norm=normalize_ar(piece),
                        heading_path=path,
                        article_no=str(article_no),
                        page=page.number,
                        span=(absolute_start, absolute_start + len(piece)),
                        lang=detect_language(piece),
                        ocr_confidence=page.ocr_confidence,
                    )
                )

    log.info(
        "chunk.done",
        doc_id=document.doc_id,
        chunks=len(chunks),
        articles=len({c.article_no for c in chunks}),
    )
    return chunks


def size_histogram(chunks: list[Chunk], buckets: int = 8) -> str:
    """Render the chunk-size distribution, for the README.

    A distribution is the honest way to describe chunk size: a single mean
    hides the long articles that got split and the short ones that did not.
    """
    if not chunks:
        return "(no chunks)"

    sizes = sorted(len(c.text) for c in chunks)
    low, high = sizes[0], sizes[-1]
    width = max(1, (high - low) // buckets + 1)

    lines = [f"n={len(sizes)}  min={low}  max={high}  median={sizes[len(sizes) // 2]}"]
    for bucket in range(buckets):
        lo = low + bucket * width
        hi = lo + width
        count = sum(1 for s in sizes if lo <= s < hi)
        bar = "#" * min(50, count * 50 // max(1, len(sizes)) + (1 if count else 0))
        lines.append(f"  {lo:5}-{hi:<5} {count:4}  {bar}")
    return "\n".join(lines)
