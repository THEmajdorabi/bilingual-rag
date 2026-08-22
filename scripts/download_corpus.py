"""Fetch the corpus, then refuse to declare success until it passes quality checks.

Downloading is the easy half. The half that matters is not accepting a file
that extracts cleanly but is silently wrong -- see `check_corpus_quality.py`
and the "Rejected source" section of `data/README.md`.

Run: python scripts/download_corpus.py [--force]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Direct-download URL for the indexed corpus. Percent-encoded because the
# path is Arabic; httpx would otherwise re-encode it inconsistently.
LSAT_URL = (
    "https://l-sat.com/wp-content/uploads/2022/02/"
    "%D9%86%D8%B8%D8%A7%D9%85-%D8%A7%D9%84%D8%B9%D9%85%D9%84-"
    "%D9%88%D9%84%D8%A7%D9%8A%D9%94%D8%AD%D8%AA%D9%87-"
    "%D8%A7%D9%84%D8%AA%D9%86%D9%81%D9%8A%D8%B0%D9%8A%D8%A9-"
    "%D8%A7%D9%84%D8%A7%D8%B5%D8%AF%D8%A7%D8%B1-%D8%A7%D9%84%D8%AB%D8%A7%D9%86%D9%8A-"
    "1443-%D8%A7%D9%84%D8%AE%D9%86%D9%81%D8%B1%D9%8A-.pdf"
)

BOE_URL = (
    "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/"
    "08381293-6388-48e2-8ad2-a9a700f2aa94/1"
)

HRSD_EN_URL = "https://www.hrsd.gov.sa/sites/default/files/2017-05/LABOR%20LAW.pdf"


@dataclass
class Source:
    filename: str
    url: str
    role: str
    indexed: bool
    min_bytes: int


SOURCES = [
    Source(
        filename="lsat_labor_law_ar.pdf",
        url=LSAT_URL,
        role="Arabic corpus: Labor Law + Implementing Regulations, 1443H",
        indexed=True,
        min_bytes=3_000_000,
    ),
    Source(
        filename="boe_labor_law.html",
        url=BOE_URL,
        role="Official Board of Experts text, for provenance cross-check",
        indexed=False,
        min_bytes=100_000,
    ),
    Source(
        filename="hrsd_labor_law_en.pdf",
        url=HRSD_EN_URL,
        role="Official English translation, for cross-lingual eval questions",
        indexed=False,
        min_bytes=500_000,
    ),
]


def download(source: Source, force: bool) -> tuple[bool, str]:
    """Fetch one source. Returns (changed, message)."""
    target = DATA_DIR / source.filename

    if target.exists() and not force:
        return False, f"already present ({target.stat().st_size:,} bytes)"

    try:
        with httpx.Client(follow_redirects=True, timeout=180.0) as client:
            response = client.get(source.url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return False, f"FAILED: {exc}"

    body = response.content
    if len(body) < source.min_bytes:
        # A short body is almost always an error page served with HTTP 200.
        return False, (
            f"FAILED: got {len(body):,} bytes, expected at least "
            f"{source.min_bytes:,}. The URL probably now serves an error page."
        )

    target.write_bytes(body)
    return True, f"downloaded {len(body):,} bytes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    failed = False

    for source in SOURCES:
        marker = "INDEXED" if source.indexed else "reference"
        changed, message = download(source, args.force)
        print(f"[{marker:9}] {source.filename}: {message}")
        print(f"            {source.role}")
        if message.startswith("FAILED"):
            failed = True

    if failed:
        print("\nAt least one source failed. See data/README.md for the URLs.")
        return 1

    print("\nNow verify before indexing:")
    print("    python scripts/check_corpus_quality.py data/*.pdf data/*.html")
    print("\nA source that extracts cleanly can still be silently corrupt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
