"""Decide whether an Arabic source is fit to index, before building on it.

Written on day one, after the first candidate PDF turned out to be silently
corrupt: it rendered every kaf as an alef-madda, so `كما` came out as `آما`.
Nothing raises on that. Extraction succeeds, the text looks like Arabic, and
retrieval quality is quietly destroyed -- normalization then folds the bad
alef-madda to a bare alef, so the word is wrong in the index *and* in the
query, in different ways.

Four checks, each catching a different silent failure:

  1. **Extractability** -- is there a text layer, or is it a scan needing OCR?
  2. **Script order** -- logical or visual? PyMuPDF does no bidi reordering,
     and many Arabic PDFs store glyphs in visual order.
  3. **Letter-frequency sanity** -- a letter appearing far outside its natural
     rate is the signature of a broken font encoding.
  4. **Known-word probes** -- common Arabic words that must appear in any legal
     text. If `كذلك` appears zero times and `آذلك` appears eight, the encoding
     is broken.

Run: python scripts/check_corpus_quality.py data/some.pdf
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Words that must appear in any Saudi legal text of reasonable length, paired
# with the corrupted spelling produced when kaf is mis-encoded as alef-madda.
KAF_PROBES = [
    ("كما", "آما"),
    ("كذلك", "آذلك"),
    ("كل", "آل"),
    ("الكتابة", "الآتابة"),
    ("كامل", "آامل"),
]

# Structural markers of Saudi legislation.
STRUCTURE = {"chapter": "الباب", "section": "الفصل", "article": "المادة"}

# Rough expected share of Arabic letters, from ordinary Modern Standard Arabic
# prose. Used only to flag gross anomalies, not to grade style.
EXPECTED_MAX_SHARE = {
    "آ": 0.010,  # alef madda: genuinely rare
    "ك": 0.060,  # kaf: common
}


@dataclass
class Report:
    path: Path
    pages: int
    chars: int
    arabic_chars: int
    presentation_forms: int
    logical_order: bool
    structure_counts: dict[str, int]
    kaf_findings: list[tuple[str, str, int, int]]
    letter_shares: dict[str, float]
    problems: list[str]

    @property
    def usable(self) -> bool:
        return not self.problems


def extract_text(path: Path) -> tuple[str, int]:
    """Return the document's text and its page count."""
    if path.suffix.lower() == ".pdf":
        import pymupdf

        doc = pymupdf.open(path)
        return "\n".join(doc[i].get_text() for i in range(doc.page_count)), doc.page_count

    if path.suffix.lower() in (".html", ".htm"):
        # Deliberately dependency-free: strip tags rather than add a parser for
        # a one-off quality check.
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.I)
        return re.sub(r"<[^>]+>", " ", raw), 1

    return path.read_text(encoding="utf-8", errors="replace"), 1


def analyse(path: Path) -> Report:
    raw, pages = extract_text(path)
    text = unicodedata.normalize("NFKC", raw)
    flat = re.sub(r"\s+", " ", text)

    arabic_chars = sum(1 for c in flat if "؀" <= c <= "ۿ")
    presentation = sum(1 for c in raw if "ﭐ" <= c <= "﻿")
    problems: list[str] = []

    # 1. Extractability.
    if arabic_chars < 500:
        problems.append(
            f"only {arabic_chars} Arabic characters extracted: likely a scan needing OCR"
        )

    # 2. Script order. 'المادة' reversed is 'ةداملا'; if the reversed form
    #    dominates, the text is in visual order and needs repair.
    forward = flat.count("المادة")
    reversed_ = flat.count("ةداملا")
    logical_order = forward >= reversed_
    if not logical_order:
        problems.append(
            f"text appears to be in visual order (reversed 'المادة' x{reversed_} "
            f"vs forward x{forward}); a bidi reversal step is required"
        )

    # 3. Letter-frequency sanity.
    letters = [c for c in flat if "؀" <= c <= "ۿ" and c.isalpha()]
    total_letters = len(letters) or 1
    shares = {ch: letters.count(ch) / total_letters for ch in EXPECTED_MAX_SHARE}
    for ch, limit in EXPECTED_MAX_SHARE.items():
        if shares[ch] > limit:
            problems.append(
                f"U+{ord(ch):04X} accounts for {shares[ch]:.1%} of Arabic letters "
                f"(expected under {limit:.1%}): probable font-encoding corruption"
            )

    # 4. Known-word probes.
    findings = []
    for good, bad in KAF_PROBES:
        good_n, bad_n = flat.count(good), flat.count(bad)
        findings.append((good, bad, good_n, bad_n))
    corrupted = [f for f in findings if f[3] > f[2]]
    if corrupted:
        detail = ", ".join(f"{bad!r} x{bn} > {good!r} x{gn}" for good, bad, gn, bn in corrupted)
        problems.append(f"kaf appears mis-encoded as alef-madda: {detail}")

    structure = {name: len(re.findall(marker, flat)) for name, marker in STRUCTURE.items()}
    if structure["article"] < 20:
        problems.append(
            f"only {structure['article']} occurrences of 'المادة': "
            f"this may not be the full legal text"
        )

    return Report(
        path=path,
        pages=pages,
        chars=len(flat),
        arabic_chars=arabic_chars,
        presentation_forms=presentation,
        logical_order=logical_order,
        structure_counts=structure,
        kaf_findings=findings,
        letter_shares=shares,
        problems=problems,
    )


def render(report: Report) -> str:
    lines = [
        f"source           : {report.path.name}",
        f"pages            : {report.pages}",
        f"characters       : {report.chars:,}  ({report.arabic_chars:,} Arabic)",
        f"presentation forms: {report.presentation_forms:,}  "
        f"({'NFKC will fix these' if report.presentation_forms else 'none'})",
        f"script order     : {'logical (good)' if report.logical_order else 'VISUAL (needs repair)'}",
        "structure        : "
        + ", ".join(f"{k}={v}" for k, v in report.structure_counts.items()),
        "",
        "kaf probes (correct vs corrupted spelling):",
    ]
    for good, bad, good_n, bad_n in report.kaf_findings:
        verdict = "OK" if good_n >= bad_n else "CORRUPT"
        lines.append(f"   {good:<10} x{good_n:<5}  vs  {bad:<10} x{bad_n:<5}  {verdict}")

    lines.append("")
    lines.append("letter shares:")
    for ch, share in report.letter_shares.items():
        lines.append(
            f"   U+{ord(ch):04X} {ch}  {share:.2%}  (limit {EXPECTED_MAX_SHARE[ch]:.1%})"
        )

    lines.append("")
    if report.usable:
        lines.append("VERDICT: usable")
    else:
        lines.append("VERDICT: NOT usable")
        for problem in report.problems:
            lines.append(f"  - {problem}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    worst = 0
    for path in args.paths:
        if not path.exists():
            print(f"{path}: not found\n")
            worst = 1
            continue
        report = analyse(path)
        print(render(report))
        print("-" * 70)
        worst = worst or (0 if report.usable else 1)
    return worst


if __name__ == "__main__":
    sys.exit(main())
