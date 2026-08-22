# Corpus

Raw documents are **gitignored**. `scripts/download_corpus.py` re-downloads
them, and `scripts/check_corpus_quality.py` verifies a source is fit to index
before anything is built on it.

## What is indexed

| | |
|---|---|
| **Document** | نظام العمل — Saudi Labor Law, with amendments in force |
| **Publisher** | هيئة الخبراء بمجلس الوزراء — Board of Experts at the Council of Ministers |
| **URL** | `https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1` |
| **File** | `boe_labor_law.html` |
| **Downloaded** | 2026-08-22 |
| **Size** | 567,479 bytes · 181,287 characters · 140,663 Arabic |
| **Instrument** | Royal Decree No. M/51 of 23/8/1426H, as amended |
| **Language** | Arabic |

This is the **official** text, and it is current: it carries amendments made by
Royal Decree M/44 of 1446H, which the older PDF editions predate.

It is HTML rather than PDF, and that is precisely why it is clean. HTML carries
real Unicode; a PDF carries glyph indices into an embedded font, and it is that
indirection that corrupted both PDF candidates below.

### Implementing Regulations (also indexed)

| | |
|---|---|
| **Document** | اللائحة التنفيذية لنظام العمل وملحقاتها |
| **Publisher** | Ministry of Human Resources and Social Development |
| **URL** | `https://www.hrsd.gov.sa/sites/default/files/2025-04/اللائحة التنفيذية لنظام العمل وملحقاتها.pdf` |
| **File** | `hrsd_regulations_ar.pdf` · 2,559,189 bytes · 109 pages · 170,231 Arabic characters |
| **Downloaded** | 2026-08-22 |

Added after the first eval showed retrieval saturating at 0.985: with only the
Law indexed there was too little in the corpus for retrieval to be confused by.
Passes all five quality checks, and being a clean PDF it restores the PyMuPDF
extraction path that the HTML corpus does not exercise.

It numbers articles with **digits in brackets** (`المادة )15( مكرر`) where the
Law spells them as ordinals, so the heading parser handles both styles.

### English translation

| | |
|---|---|
| **Publisher** | Ministry of Human Resources and Social Development |
| **URL** | `https://www.hrsd.gov.sa/sites/default/files/2017-05/LABOR%20LAW.pdf` |
| **File** | `hrsd_labor_law_en.pdf` · 865,587 bytes · 93 pages |
| **Downloaded** | 2026-08-22 |

Held for the cross-lingual evaluation questions and to exercise the PDF
extraction path. Being English, it has none of the Arabic shaping problems.
**Not indexed** — it is a translation of a document already in the index.

## Rejected sources, and why it matters

**Two of three candidate sources were silently corrupt, in different ways.**
Both are PDFs. Both extract without error. Both look like Arabic.

### 1. WIPO Lex `sa055ar.pdf` — kaf mis-encoded

Its font encoding maps **kaf (ك, U+0643) onto alef-madda (آ, U+0622)**
throughout:

| Correct | Count | Corrupted | Count |
|---|---|---|---|
| `كما` | 1 | `آما` | **17** |
| `كذلك` | 0 | `آذلك` | **8** |
| `كامل` | 1 | `آامل` | **13** |
| `كل` | 24 | `آل` | **107** |

Nothing raises. Had this been indexed, every word containing kaf would be wrong
in the index, normalization would then fold the bad alef-madda to a bare alef,
and retrieval quality would collapse for reasons no error message would ever
explain.

### 2. l-sat.com PDF — adjacent letters transposed

The second candidate looked ideal: 308 pages, the Labor Law *and* its
Implementing Regulations, a clean text layer, zero presentation forms, and it
passed every kaf probe. It was very nearly the chosen corpus.

It stores glyphs in **visual order**, so adjacent letters come out swapped:

| Correct Arabic | Count | Extracted non-word | Count |
|---|---|---|---|
| `المادة` (article) | 248 | `املادة` | **814** |
| `اللائحة` (the regulation) | 0 | `الالئحة` | **208** |
| `الأول` (the first) | 0 | `األول` | **114** |
| `لائحة` (regulation) | 0 | `الئحة` | **311** |

Every right-hand spelling is **not a word in Arabic**. `المادة` is
`ا ل م ا د ة`; the extracted `املادة` is `ا م ل ا د ة`, with lam and meem
transposed. Every `لا` pair is reversed to `ال`.

The document-level check that PyMuPDF returns logical order **passed** on this
file, because the word-level ligature order is a separate defect from
whole-line bidi reversal. Catching it needed probes on words that must exist.

### Why the official HTML wins

Both defects come from the same place: a PDF stores glyph indices into an
embedded font, not characters. Extraction has to invert that mapping, and when
the font's encoding table is wrong or the glyphs were laid out visually, the
text comes back wrong with no error.

HTML has no such indirection. The bytes are the characters. The clean source
also turned out to be the official and the most current one, which makes the
provenance argument and the correctness argument point the same way.

This is why `check_corpus_quality.py` exists and why it runs before ingestion.

## Reproducing

```bash
python scripts/download_corpus.py          # the two indexed sources + references
python scripts/check_corpus_quality.py data/*.pdf data/*.html
```

The quality checker exits non-zero if any source fails, so it can gate CI.
