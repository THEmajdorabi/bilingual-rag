"""Get text out of a source document, with page or section coordinates.

Two backends behind one interface:

  * **HTML**: the indexed Arabic corpus. Parsed with the standard library's
    `html.parser`, so there is no extra dependency for a job this small.
  * **PDF**: PyMuPDF, per page, used for the English translation and
    available for any future PDF corpus.

The PDF path also carries the scanned-page detection and the OCR hook, because
a PDF *can* be a scan. HTML never is.

Why HTML is the primary path here is not an accident of convenience: see
`DECISIONS.md`. Two candidate Arabic PDFs extracted without error and were
both silently corrupt, in different ways, because a PDF stores glyph indices
into an embedded font rather than characters.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# A page whose text is this short while the page carries a large image is a
# scan, not a text page.
SCANNED_TEXT_THRESHOLD = 50


@dataclass
class Page:
    """One unit of source text with a coordinate for citation.

    `number` is a 1-based page for PDFs. HTML has no pages, so the whole
    document is a single Page and the article heading supplies the coordinate
    instead.
    """

    number: int
    text: str
    is_scanned: bool = False
    ocr_confidence: float | None = None


@dataclass
class Document:
    doc_id: str
    source_path: Path
    pages: list[Page] = field(default_factory=list)

    def full_text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def scanned_pages(self) -> int:
        return sum(1 for page in self.pages if page.is_scanned)


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style and preserving block breaks.

    Written rather than pulled in from BeautifulSoup because the requirement is
    exactly this: text, with newlines where the markup implied them.
    """

    # Containers whose contents are not visible text. These must all be
    # elements that actually close: a void element here would raise the skip
    # depth and never lower it, silently discarding the whole document.
    SKIP = {"script", "style", "noscript", "head", "title"}

    # Void elements never emit an end tag, so they must never affect depth.
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "table",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            # Void elements have no end tag. `<br>` still marks a line break.
            if tag in self.BLOCK:
                self._parts.append("\n")
            return
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self.BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        # Collapse runs of blank lines, but keep single breaks: they are the
        # only structural signal HTML leaves once tags are gone.
        joined = re.sub(r"[ \t]+", " ", joined)
        joined = re.sub(r"\n\s*\n+", "\n", joined)
        return joined.strip()


def extract_html(path: Path, doc_id: str | None = None) -> Document:
    """Extract text from an HTML document as a single page."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    parser = _TextExtractor()
    parser.feed(raw)
    # NFKC here, not later: it is the step that makes presentation forms and
    # ligatures comparable, and everything downstream assumes it has run.
    text = unicodedata.normalize("NFKC", parser.text())

    log.info("extract.html", path=str(path), characters=len(text))
    return Document(
        doc_id=doc_id or path.stem,
        source_path=path,
        pages=[Page(number=1, text=text)],
    )


def extract_pdf(path: Path, doc_id: str | None = None) -> Document:
    """Extract text from a PDF, one Page per page, flagging likely scans."""
    import pymupdf

    document = pymupdf.open(path)
    pages: list[Page] = []

    for index in range(document.page_count):
        page = document[index]
        text = unicodedata.normalize("NFKC", page.get_text())
        stripped = text.strip()

        # A near-empty page carrying a large image is a scan needing OCR.
        has_images = bool(page.get_images(full=False))
        is_scanned = len(stripped) < SCANNED_TEXT_THRESHOLD and has_images

        pages.append(Page(number=index + 1, text=text, is_scanned=is_scanned))

    doc = Document(doc_id=doc_id or path.stem, source_path=path, pages=pages)
    log.info(
        "extract.pdf",
        path=str(path),
        pages=len(pages),
        scanned=doc.scanned_pages,
        characters=len(doc.full_text()),
    )
    if doc.scanned_pages:
        log.warning(
            "extract.scanned_pages_detected",
            count=doc.scanned_pages,
            hint="run OCR via ingest.ocr, or these pages contribute no text",
        )
    return doc


def extract(path: str | Path, doc_id: str | None = None) -> Document:
    """Extract a document by file type."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        return extract_html(path, doc_id)
    if suffix == ".pdf":
        return extract_pdf(path, doc_id)
    raise ValueError(f"unsupported source type {suffix!r}: expected .html or .pdf")
