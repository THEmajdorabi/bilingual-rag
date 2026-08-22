# Decisions

Every entry is a real choice between alternatives, the option rejected, and
why. A failed approach that was removed still gets an entry.

---

## 2026-08-22 — Corpus: rejected the WIPO PDF as silently corrupt

**Rejected:** `sa055ar.pdf` from WIPO Lex, the first and most official-looking
candidate.

**Chose, at the time:** the l-sat.com PDF — which was itself rejected a few
hours later for a different defect. See the next entry; this one is kept
because the failure mode it documents is distinct and the checker still tests
for it.

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

## 2026-08-22 — Corpus: the official HTML, after both PDF candidates failed

**Chose:** the Board of Experts HTML (`laws.boe.gov.sa`) as the indexed corpus.

**Rejected:** the l-sat.com PDF, which had already been chosen and written into
`data/README.md` before it was found to be corrupt.

**Why the reversal:** the l-sat PDF looked like the better engineering choice.
308 pages, inside the spec's 300-800 range, the Labor Law *with* its
Implementing Regulations, a clean text layer, zero presentation forms, and it
passed every kaf probe that had rejected the WIPO file. It also exercised the
PyMuPDF extraction path, which the HTML does not.

Reading the extracted body text showed adjacent letters transposed:

| Correct Arabic | Count | Extracted non-word | Count |
|---|---|---|---|
| `المادة` | 248 | `املادة` | **814** |
| `اللائحة` | 0 | `الالئحة` | **208** |
| `الأول` | 0 | `األول` | **114** |

`المادة` is `ا ل م ا د ة`. The extracted `املادة` is `ا م ل ا د ة` -- lam and
meem swapped -- and every `لا` pair comes out as `ال`. None of the right-hand
forms is a word in Arabic.

**The important part: the existing check passed this file.** The
logical-vs-visual test looks for a whole-line reversal (`ةداملا`) and found
none, because this is a *ligature-level* defect, not a line-level one. One
check catching one defect gave false confidence about a different defect.
`TRANSPOSITION_PROBES` was added for exactly this, and the checker now names
five distinct failure modes.

**Why HTML is the right answer, not a compromise:** both PDF defects have the
same root cause. A PDF stores glyph indices into an embedded font; extraction
must invert that mapping, and when the encoding table is wrong or the glyphs
were laid out visually, the text comes back wrong with no error. HTML has no
indirection -- the bytes are the characters.

**What it costs:** the PDF extraction path is no longer exercised by the
indexed corpus. That is a real loss and is mitigated, not hidden: the official
English translation is a PDF, it extracts cleanly because English needs no
shaping, and `ingest/extract.py` handles both formats behind one interface.

**What it gains, beyond correctness:** the BOE text is the *official*
publication and is *current*, carrying amendments from Royal Decree M/44 of
1446H that every PDF edition predates. The provenance caveat written into the
previous version of `data/README.md` is simply gone.

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

**Second complication:** extraction inserts spurious spaces inside words —
`الخامس ة عشرة` for `الخامسة عشرة`, `الأربع ون` for `الأربعون`. Rather than
enumerate every place a break might land, the parser matches against the
*space-stripped* form, which repairs all of them at once.

**Third complication:** amendment text quotes cross-references inside headings,
producing `المادة المادة الثالثة`. Leading copies of the noun are stripped.

**Measured on the official corpus:** 684 heading candidates, **649 parsed
(94.9%)**, yielding **all 245 distinct article numbers, 1–245, none missing**.
The ~35 unparsed candidates are false positives from amendment prose
(`وتكون بالنص الآتي:`), not articles that were lost — which is why
`find_article_headings` surfaces `None` instead of dropping them silently.

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
