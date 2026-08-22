# Corpus

Raw documents are **gitignored**. `scripts/download_corpus.py` re-downloads
them, and `scripts/check_corpus_quality.py` verifies a source is fit to index
before anything is built on it.

## What is indexed

| | |
|---|---|
| **Document** | نظام العمل ولائحته التنفيذية — Saudi Labor Law and its Implementing Regulations |
| **Edition** | Second edition, 1443H (2022) |
| **File** | `lsat_labor_law_ar.pdf` |
| **Source URL** | `https://l-sat.com/wp-content/uploads/2022/02/نظام-العمل-ولائحته-التنفيذية-الاصدار-الثاني-1443-الخنفري-.pdf` |
| **Downloaded** | 2026-08-22 |
| **Size** | 3,615,329 bytes · 308 pages · 318,491 Arabic characters |
| **Language** | Arabic |

## Provenance and terms

The Saudi Labor Law is **public legal text**. The law itself carries no
copyright — legislation is not a copyrightable work — but this particular PDF
is a third-party republication that adds its own typesetting and the
implementing regulations alongside the statute.

**This is a compilation, not the government's own file.** It was chosen for
engineering reasons documented in `../DECISIONS.md`, and its article text is
cross-checked against the official source below.

### Official cross-check

| | |
|---|---|
| **Publisher** | هيئة الخبراء بمجلس الوزراء — Board of Experts at the Council of Ministers |
| **URL** | `https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1` |
| **File** | `boe_labor_law.html` |
| **Downloaded** | 2026-08-22 |
| **Instrument** | Royal Decree No. M/51 dated 23/8/1426H (27 September 2005) |

This is the authoritative text. It is downloaded but **not indexed** — the spec
forbids mixing sources in v1. It exists so that
`scripts/verify_against_official.py` can confirm the indexed articles match the
government's wording.

### English translation

| | |
|---|---|
| **Publisher** | Ministry of Human Resources and Social Development (hrsd.gov.sa) |
| **URL** | `https://www.hrsd.gov.sa/sites/default/files/2017-05/LABOR%20LAW.pdf` |
| **File** | `hrsd_labor_law_en.pdf` · 865,587 bytes · 93 pages |
| **Downloaded** | 2026-08-22 |

Held for the cross-lingual evaluation questions. **Not indexed** in v1.

## Rejected source, and why it matters

`sa055ar.pdf` from WIPO Lex was the first candidate and is **silently
corrupt**. Extraction succeeds and the output looks like Arabic, but the font
encoding maps **kaf (ك, U+0643) to alef-madda (آ, U+0622)** throughout:

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

This is why `check_corpus_quality.py` exists and why it runs before ingestion.

## Reproducing

```bash
python scripts/download_corpus.py          # fetches all three files
python scripts/check_corpus_quality.py data/*.pdf data/*.html
```

The quality checker exits non-zero if any source fails, so it can gate CI.
