# Decisions

Every entry is a real choice between alternatives, the option rejected, and
why. A failed approach that was removed still gets an entry.

---

## 2026-08-22 — Corpus: rejected the WIPO PDF as silently corrupt

**Chose:** `نظام العمل ولائحته التنفيذية`, second edition 1443H, a 308-page PDF
carrying the Labor Law together with its Implementing Regulations.

**Rejected:** `sa055ar.pdf` from WIPO Lex, which was the first and most
official-looking candidate.

**Why:** the WIPO PDF is **silently corrupt**. Extraction succeeds, the output
looks like Arabic, and every check short of reading it passes. But its font
encoding maps **kaf (ك, U+0643) onto alef-madda (آ, U+0622)** throughout:

| Correct | Count | Corrupted | Count |
|---|---|---|---|
| `كما` | 1 | `آما` | **17** |
| `كذلك` | 0 | `آذلك` | **8** |
| `كامل` | 1 | `آامل` | **13** |
| `كل` | 24 | `آل` | **107** |

Two things make this the worst class of bug rather than merely a bad one.
Nothing raises — there is no error to notice. And normalization makes it
*worse*: `normalize_ar` folds alef-madda to bare alef, so `كذلك` would be
indexed as `اذلك` while a user typing `كذلك` searches for `كذلك`. The index and
the query would be wrong in *different* ways, and recall would collapse for a
reason no log line would ever explain.

Found on day one, by reading extracted text rather than trusting a page count.

**Consequence:** `scripts/check_corpus_quality.py` now exists and runs before
ingestion. It checks extractability, script order, letter-frequency sanity, and
known-word probes, and exits non-zero on failure so it can gate CI.

---

## 2026-08-22 — Corpus provenance: a third-party compilation over the official HTML

**Chose:** the l-sat.com PDF as the **indexed** corpus.

**Rejected:** the Board of Experts HTML (`laws.boe.gov.sa`) as the indexed
corpus — though it is downloaded and kept.

**Why:** this is a genuine trade-off, and the honest reading is that neither
option is strictly better.

The BOE page is the *authoritative* text, and for a legal demo provenance
matters. But it is HTML, it carries the statute without the implementing
regulations, and it would leave the PDF extraction half of this project — the
part with the real engineering in it — untested.

The l-sat PDF is 308 pages, inside the spec's 300–800 page target, includes the
regulations, has a clean text layer with **zero presentation forms**, is in
logical order, and passes every quality probe. It exercises the pipeline the
project is actually about.

**The cost, stated plainly:** it is a third-party republication, not a
government file. That is a real weakness for a legal application and is
recorded in `data/README.md` rather than glossed over.

**Mitigation:** the official BOE text is downloaded alongside it, and
`scripts/verify_against_official.py` cross-checks indexed article text against
it. The spec forbids mixing sources in one index, so BOE is a verification
input, never an indexed one.

---

## 2026-08-22 — Article numbers are Arabic ordinal *words*, not digits

**Found:** the spec's chunking design assumes headings like `المادة 74`. The
real corpus contains **zero** digit-numbered articles. All 239 are spelled out:
`المادة التاسعة والثلاثون` (article thirty-ninth), `المادة التاسعة والثلاثون
بعد المائة` (one hundred thirty-ninth).

**Consequence:** structure-aware chunking needs an Arabic ordinal-word parser
to recover an `article_no` at all. A regex over digits would produce a corpus
with no article numbers, which would break citations — the feature the whole
project is built to demonstrate.

This is a spec detail that was wrong once inside the problem, changed rather
than worked around, exactly as Part 0 §6 instructs.

**Second complication:** PDF extraction inserts spurious spaces inside words —
`الخامس ة عشرة` for `الخامسة عشرة`, `الثان ية` for `الثانية`, `الأربع ون` for
`الأربعون`. The parser must tolerate intra-word whitespace, so it normalizes
spaces out of the ordinal phrase before matching.

---

## 2026-08-22 — Invisible character classes are reviewed by test, not by reading

**Chose:** keep literal character ranges in `normalize.py`, and assert their
exact codepoint coverage in `tests/test_normalize.py`.

**Rejected:** writing them as `\uXXXX` escapes.

**Why:** escapes are the obvious answer, and the first attempt did exactly that.
But the class covers diacritics and bidirectional marks, which are *invisible*
either way — an escape sequence is no more reviewable than a glyph when you are
checking whether U+0653 is included. A test that walks every codepoint in the
claimed range and asserts it matches is the only form of review that actually
catches an off-by-one.

`test_diacritic_range_covers_every_codepoint` walks U+064B–U+065F plus U+0670,
and asserts the class does *not* swallow ordinary letters.

**Related:** the range is deliberately wider than pyarabic's `TASHKEEL`
constant, which stops at U+0652 and so misses maddah and the hamza-above/below
marks at U+0653–U+0655. Those do occur in vocalised legal text.
