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

---

## 2026-08-22 — [VOID, SUPERSEDED] Arabic normalization is worth far less than the spec predicts

> **This entry's conclusion is WRONG and is retained only as a record of the
> mistake.** It was measured against an eval set that could not discriminate:
> the dense-only baseline was 0.985, leaving no headroom for any component to
> show value. Re-measured on a corpus roughly twice the size and questions that
> do not reuse the article's vocabulary, normalization is worth **+0.028**,
> above the noise threshold and a demonstrated improvement. **The spec was
> right and this entry was wrong.** See "Re-measured on a discriminating eval
> set" below.

**The spec's claim.** BUILD.md, Part 3, on `arabic/normalize.py`: *"Without
normalization, dense retrieval is mediocre and BM25 is close to useless. This
is the step tutorials omit, and it is where most of your recall gain will come
from."*

**What was measured.** It is worth **+0.007 recall@10** on this corpus, which
is inside the noise threshold and not a demonstrated improvement.

| Configuration | recall@10 | MRR@10 | nDCG@10 | p95 |
|---|---|---|---|---|
| dense only, no normalization | 0.9851 | 0.8954 | 0.893 | 33 ms |
| + Arabic normalization | **0.9925** | 0.8854 | 0.889 | 33 ms |
| + BM25 hybrid with RRF | 0.9552 | 0.7071 | 0.760 | 30 ms |
| + cross-encoder reranking | **0.9925** | **0.9259** | 0.926 | 360 ms |

Reproduce: `python scripts/ingest.py --recreate --build-raw && python eval/ablate.py`.
Raw output in `eval/results/ablation.json`.

Note that normalization *lowers* MRR slightly (0.8954 → 0.8854) while raising
recall. It moves a relevant article into the top 10 that was outside it, and
demotes one that was ranked highly. Both movements are small.

**Why the spec is wrong here, specifically.** Two reasons, and neither means
the spec's reasoning was bad in general:

1. **BGE-M3 already absorbs orthographic variation.** The spec's claim dates
   from a world of lexical and weaker multilingual embedders. A model trained
   on large multilingual corpora has seen `الأجر` and `الاجر` and maps them
   close together without help.
2. **The corpus is unusually clean.** It is official HTML: typed, unvocalised,
   no diacritics, no presentation forms, no tatweel padding. The variation
   normalization exists to collapse is *largely absent from the input*. On a
   scanned or PDF-extracted corpus -- the two candidates rejected earlier in
   this file -- it would have far more to do.

**What changed, and what deliberately did not.**

**Changed:** the claim. No README or docstring in this repo repeats the spec's
"most of your recall gain" line. The ablation table is published with the
measured `+0.007` and the "within noise" verdict attached, rather than the
number being quietly omitted because it is unflattering.

**Not changed: normalization stays on by default.** Removing it would be
over-fitting to one clean corpus, and would be wrong for three concrete
reasons:

- **BM25 depends on it.** The lexical index matches strings. The dense
  encoder's tolerance for spelling variation does nothing for BM25, and the
  baseline row's BM25 is unstemmed and unnormalized precisely so the row is a
  true baseline.
- **Queries are dirtier than the corpus.** The corpus is typed by a ministry;
  queries are typed by users. The eval set's `colloquial` and `misspelled`
  questions exist for this reason, and user text carries the variation the
  official text does not.
- **The next corpus may not be clean.** Normalization is cheap (+0 ms at p95)
  and is the difference between working and not working on a scanned source.

Turning off a component that costs nothing, guards a real failure mode, and
merely fails to help *on this particular input* would be the wrong lesson to
draw from the measurement.

**Three caveats, stated because they bound how much this measurement is worth:**

1. **The baseline is already 0.985.** There is almost no headroom for any
   component to demonstrate value on recall@10. This measurement discriminates
   poorly by construction, and MRR is doing most of the real work.
2. **The eval set is model-drafted, not human-verified.** Per `eval/schema.py`,
   these figures measure the pipeline rather than its quality until
   `eval/review.py` has been run.
3. **The three repeats show `sd = 0.0000` on every quality metric**, because
   retrieval here is deterministic: embeddings are cached, BM25 is
   deterministic, and reranker scores are cached per (query, chunk). The
   repeats vary latency only. "Mean of 3 runs" must not be read as a variance
   estimate on quality -- there is no run-to-run variance to estimate.

---

## 2026-08-22 — [VOID, SUPERSEDED] The BM25 hybrid hurts on this corpus, and is kept anyway (for now)

> **This entry's numbers are void**, measured against the same
> non-discriminating eval set. The direction was right but the magnitude was
> badly understated: BM25 alone costs **-0.170**, not -0.037. The conclusion
> that the hybrid is "a wash" is also wrong -- with reranking the full pipeline
> now beats dense+normalization by +0.038. See below.

**Measured:** adding BM25 with RRF fusion **lowers** recall@10 from 0.9925 to
0.9552 and MRR@10 from 0.8854 to 0.7071. Cross-encoder reranking then recovers
it exactly, back to 0.9925 recall and 0.9259 MRR, for **+330 ms at p95**.

So on this corpus `dense + normalization` and the full four-stage pipeline
reach **identical recall@10 (0.9925)**. The BM25-plus-reranker pair is a wash
on recall, buys +0.041 MRR, and costs an order of magnitude in latency
(33 ms → 360 ms).

**Why BM25 hurts here.** With 305 chunks and dense retrieval already at 0.99,
a second retriever has no signal left to add. What it does add is
lexically-similar-but-wrong articles -- legal text reuses the same vocabulary
across chapters -- and RRF promotes anything both lists rank at all, so those
near-misses displace correct articles from the top 10.

**Decision: no change to the default pipeline yet.** Dropping BM25 and the
reranker on this evidence would be premature, because the evidence has the
three weaknesses in the entry above: a near-ceiling baseline, an unverified
eval set, and no variance estimate. Making an architectural cut on a
measurement that cannot discriminate is how you end up with a fast pipeline
that is worse on the questions you did not test.

**Gate on the decision, so it does not drift into a default by neglect:**
re-run the ablation once the eval set is human-verified *and* extended with
harder questions -- ones where the answer is spread across chapters or phrased
without the article's own vocabulary. If the hybrid still fails to earn its
330 ms there, cut it and record the removal here.

**Confirmed rather than contradicted:** the spec predicts reranking is "usually
the biggest single latency cost". Measured: +330 ms of a 360 ms p95, against
~33 ms for dense and under 1 ms for BM25. That is exactly right.

---

## 2026-08-22 — Re-measured on a discriminating eval set; two earlier conclusions void

**What was wrong.** The first ablation reported a dense-only baseline of
**0.985 recall@10**. At that ceiling no component can demonstrate value, and
every delta in the table was noise dressed as a finding. The cause was
measured, not guessed:

| Diagnostic | Before | After |
|---|---|---|
| chunks indexed | 305 | **561** |
| corpus characters | 169,406 | **304,092** |
| mean question/passage lexical containment | 0.517 | **0.088** |
| median containment | 0.625 | **0.000** |
| questions with >= 75% containment | 37% | **0%** |
| labelled article at BM25 rank 1 | 52% | **2%** |
| labelled article in BM25 top 10 | 67% | **17%** |

The old questions restated their own answers. `ما هي مدة الإشعار المطلوبة قبل
إنهاء عقد العمل غير محدد المدة؟` shares every key term with article 75, so
**BM25 alone solved half the set by string matching**. That is a vocabulary
benchmark, not a retrieval benchmark.

**Two changes.**

1. **Corpus enlarged** with the Implementing Regulations
   (`hrsd_regulations_ar.pdf`, 109 pages, official HRSD publication, passes all
   five quality checks). 305 -> 561 chunks. Retrieval now has real competition,
   and the PDF path lost when the corpus moved to HTML is exercised again.
   The regulations number articles with **digits in brackets** -- `المادة )15(
   مكرر` -- where the Law spells them as ordinals, so the heading parser now
   handles both, and `مكرر` articles get a `15_bis` id.
2. **Every question rewritten to describe the situation, not the legal
   concept**, in Gulf colloquial with the misspellings users actually type:
   `مديري قال لي اطلع بكرة وراتبي شهري، لازم يعطيني وقت قبل؟` in place of the
   phrasing above.

Article labels are now `doc_id:article`, because both documents number an
"article 15" and a bare number stopped being unique.

**The new table** (53 answerable questions, mean of 3 runs, `cuda_small`):

| Configuration | recall@10 | MRR@10 | nDCG@10 | p95 |
|---|---|---|---|---|
| dense only, no normalization | 0.557 | 0.274 | 0.331 | 36 ms |
| + Arabic normalization | 0.585 | 0.293 | 0.352 | 36 ms |
| + BM25 hybrid with RRF | 0.415 | 0.185 | 0.236 | 31 ms |
| + cross-encoder reranking | **0.623** | **0.339** | **0.401** | 413 ms |

**What each conclusion becomes:**

- **Normalization: the spec was right, I was wrong.** +0.028 recall@10, above
  the 0.02 noise threshold, for 0 ms. The earlier "+0.007, within noise, the
  spec is wrong" was an artifact of a saturated benchmark. The lesson is not
  about Arabic -- it is that **a component measured at ceiling always looks
  worthless**, and the honest response to a suspiciously flat ablation is to
  attack the benchmark before drawing conclusions from it.
- **BM25 alone still hurts, and by far more than reported:** -0.170, not
  -0.037. Legal text reuses vocabulary across chapters, and RRF promotes
  anything both lists rank at all, so lexical near-misses displace correct
  articles.
- **Reranking is now clearly the largest single win:** +0.208 recall@10 and
  +0.154 MRR, for +382 ms. It more than recovers what BM25 costs.
- **The full pipeline is now justified, where before it was a wash.** Full
  (0.623) beats dense+normalization (0.585) by +0.038, above the noise
  threshold. Previously both sat at 0.993 and the extra 330 ms bought nothing.
  **The gate set in the superseded BM25 entry therefore resolves toward keeping
  the hybrid**, not cutting it.

**Still true, and still limiting:** these questions remain `verified_by:
drafted`. A model wrote them and a model labelled them; nobody has read the
article and confirmed. `eval/review.py` exists for that, and until it has been
run these numbers measure the pipeline rather than its quality. Retrieval is
also still deterministic, so `sd = 0` across repeats and there is no
variance estimate on quality.

**Composition drifted and is under target:** the rewrite cut multi-hop
questions from 15 to 9 against the spec's floor of 15, and answerable Arabic
from 47 to 33. Situation-framing makes multi-hop questions harder to write
without sliding back into legal phrasing. This is a stated gap, not a silent
one.

**Reproduce:**

```bash
python scripts/ingest.py --recreate --build-raw
python scripts/diagnose_eval_difficulty.py
python eval/ablate.py --repeats 3
```

---

## 2026-08-22 — The BM25 -0.170 investigated: mostly real, partly fusion config

**The challenge:** a -0.170 recall drop from *adding* a retriever is more
likely a bug than a finding. Three candidate bugs were tested before the
number was published. `scripts/investigate_bm25.py` reproduces all of it.

### Finding 1: BM25 alone has almost no signal on these queries

| Retriever | recall@10 | MRR@10 |
|---|---|---|
| dense only | 0.585 | 0.293 |
| **BM25 only** | **0.123** | 0.078 |

**73% of query tokens do not appear in the index vocabulary at all** -- 475 of
653. Examples: `يعطين`, `طلعون`, `عطان`, `مقنع`, `بكر`. These are colloquial
words that simply do not occur in formal legal text. BM25 matches strings, and
there are no strings to match.

This is a direct consequence of fixing the eval set. The *old* questions reused
the article's own vocabulary, which is exactly what BM25 needs; that is why
BM25 alone solved 52% of them at rank 1. Making the questions realistic removed
BM25's advantage, because **real users do not write like legislation.**

### Finding 2: equal weighting is the largest single contributor

| dense : BM25 | recall@10 | MRR@10 |
|---|---|---|
| 1:1 (current default) | 0.453 | 0.163 |
| 2:1 | 0.509 | 0.247 |
| 3:1 | 0.547 | 0.269 |
| 5:1 | 0.566 | 0.275 |
| 10:1 | 0.585 | 0.273 |
| 1:0 (BM25 off) | **0.585** | **0.293** |

Standard RRF weights every ranking equally, which is what makes it
parameter-free. When one retriever is 5x weaker, that equality is the problem:
a 0.132 swing between 1:1 and 10:1.

**But down-weighting only reaches parity, never past it.** At 10:1 recall
matches BM25-off exactly (0.585) and MRR is still worse (0.273 vs 0.293).
**There is no weighting at which BM25 helps.** So this is a partial explanation
of the magnitude, not a rescue of the component.

### Finding 3: candidate depth is not the lever

| BM25 depth | recall@10 |
|---|---|
| top-5 | 0.500 |
| top-10 | 0.491 |
| top-20 | 0.481 |
| top-30 | 0.481 |
| top-50 (default) | 0.453 |

Shallower is better, monotonically, which is consistent with "BM25 contributes
noise" -- but even top-5 is well below dense alone. Capping the depth cannot
fix a retriever that has no signal.

### Finding 4: two genuine stemmer bugs, found and fixed

Reading the stemmer's output on colloquial terms found it stripping **root
letters as though they were prefixes**:

| Word | Meaning | Was stemmed to | Problem |
|---|---|---|---|
| `فلوس` | money | `لوس` | leading `ف` is root, not the conjunction |
| `ورقه` | paper | `رقه` | leading `و` is root, not the conjunction |

Neither `لوس` nor `رقه` is a word, so those tokens matched nothing. Fixed by
requiring a longer remainder before stripping a **one-letter** prefix
(`MIN_STEM_AFTER_SHORT_PREFIX = 4`); multi-letter prefixes like `ال` are
unaffected. Regression tests in `test_retrieval_units.py`.

**Honest outcome: the fix did not help BM25.** Recall went 0.142 -> 0.123,
because stricter stemming means fewer matches, and on this query set the
matches it was making were mostly wrong anyway. The bug was real and worth
fixing on its own terms; it was not the cause of the regression.

### Verdict

The -0.170 decomposes as: **equal-weight fusion (~0.13) on top of a retriever
that genuinely has nothing to contribute (~0.04)**. It is not a fusion bug in
the sense of incorrect code -- RRF is behaving exactly as specified -- but the
headline number does overstate BM25's harm, because it was measured at the
worst available weighting.

**No default is being changed yet.** Re-weighting to 10:1 would be tuning
against 68 model-drafted, unverified labels, and recall is capped by label
quality. The eval set is being reviewed by hand first; these sweeps get re-run
against verified labels afterwards, and the decision to keep, re-weight or drop
BM25 is made then.

**What this changes in the published table:** the ablation row keeps its
measured value, with a footnote pointing here, because a reader who sees
"-0.170" deserves to know that most of it is a weighting artifact and that the
component is useless rather than actively catastrophic.

---

## 2026-08-22 — Label verification is partial, by risk-weighted sampling

**Chose:** verify **23 of 68** labels by hand, selected by risk, rather than
all 68.

**Rejected:** full manual review (accurate but expensive), and no review at all
(cheap but leaves every number uninterpretable).

**Why:** risk is not spread evenly across the labels, so neither extreme is the
right trade. Reviewing all 68 costs hours of a domain reader's attention, most
of it spent confirming labels that were never in doubt. Reviewing none leaves
recall capped by label quality in a way no retrieval change can lift.

**The sample, three disjoint groups** (`EvalSet.review_sample`, seeded at
20260822 so it reproduces exactly):

| Group | n | Why |
|---|---|---|
| every low-confidence label | **8** | the drafter's own flagged doubts: 3 cite articles never read in full, 5 are refusal labels judged before the Regulations were indexed |
| high-confidence **unanswerable**, sampled | **5** | refusal labels carry a specific risk that retrieval labels do not, so they get coverage beyond the ones already flagged |
| high-confidence **answerable**, sampled | **10** | the **control** |
| | **23 / 68** | |

The groups are disjoint on purpose. Five of the eight low-confidence labels are
themselves unanswerable, so counting them in both groups would have made the
review look larger than it is.

**The third group is the point.** If the high-confidence sample comes back
clean, the drafter's confidence self-assessment is calibrated and leaving the
other 45 unreviewed is defensible. **If any of those 10 are wrong, the
self-assessment is not trustworthy and all 68 need reviewing** -- the sample is
designed to detect that, not to assume it away.

**What this means for the README, stated so the claim stays accurate:**

- No figure may be described as measured against a *verified* eval set.
  The accurate phrasing is **"measured against a partly-verified set: 23 of 68
  labels checked by hand, selected by risk-weighted sampling."**
- `run_eval.py` and `ablate.py` print a PARTIAL VERIFICATION banner naming the
  exact counts whenever some but not all labels are verified, so the caveat
  travels with the numbers rather than living only here.
- `--verified-only` computes over the verified subset alone. With n=23 that is
  a small sample, and it over-represents hard and risky questions by
  construction, so it is a **lower** bound rather than a like-for-like
  comparison with the full-set figure.

**Reproduce the sample:**

```bash
python eval/review.py --sample
```

**One safeguard added the hard way.** While testing the CLI, piped input
marked three labels `verified_by: human` that no human had read. They were
reset, and `review.py` now refuses to run when stdin is not a terminal.
Manufacturing provenance is worse than having none: an unverified label is
merely unknown, whereas a falsely verified one is a number nobody will think
to re-check. `--status` still works non-interactively, since inspecting
records no judgement.

---

## 2026-08-22 — Label review moved from the terminal to a local browser page

**Chose:** a local FastAPI page at `127.0.0.1`, one question per screen.

**Rejected:** the terminal CLI it replaces.

**Why:** the CLI was unusable for its only user. Windows `cmd` renders Arabic
as question marks, and even where a font is present it performs no bidi
reordering and no letter joining, so an Arabic legal passage arrives as
disconnected glyphs in the wrong order. A reviewer cannot judge whether an
article answers a question when they cannot read the article. A browser does
all three correctly given `dir="rtl"` and a font that ships with Windows.

No web fonts: this runs offline, and a CDN font that failed to load would fall
back to something that cannot join letters -- reintroducing the exact bug,
silently. The stack is Segoe UI / Tahoma, both present on Windows.

**The safety property is carried over, not dropped.** The CLI refused to run
when stdin was not a terminal, because piped input had once marked three labels
`verified_by: human` that no human read. The browser equivalent:

- every mutation is a **POST carrying a per-run token**, regenerated at
  startup, so a crawler, a link prefetcher, a stale bookmark or a stray `curl`
  cannot record a verification;
- **GET never writes** -- viewing and searching are side-effect free;
- the server binds **127.0.0.1 only** and has no `--host` flag, because it
  writes to the eval set and has no authentication.

`tests/test_review_server.py` asserts all of it: every mutating route is
enumerated and checked to reject a bad token, and viewing and searching are
checked not to change the verified count. Tests run against a temp copy of the
eval set, so the suite cannot pollute the real labels.

**A bug worth recording, because it is the second time this exact thing bit.**
Every GET returned 422. Under `from __future__ import annotations` an
annotation is a *string* that FastAPI resolves against **module** globals; the
`Request` import lived inside `build_app`, so FastAPI could not see it and
treated `request: Request` as an unknown query parameter. This is the same
failure mode as function-local types in `agent-core`'s tool registry, and the
same fix: the name has to be importable at module level.

**What did not change:** the file format, the sampling design, the actions, or
the meaning of `verified_by`. This is a different front end onto the same
23-question `--sample` review.

## 2026-08-22 — Label review was called off; the eval set ships UNVERIFIED

**Chosen:** stop the label review at 2 of 68 questions and publish every number
as provisional.

**Rejected:** finishing the 23-question `--sample` review before publishing;
quietly marking the remainder `verified_by: drafted` and reporting the numbers
without a caveat.

**Why:** the review is a human's time, and the owner of this repo chose to
spend it elsewhere and take the accuracy cost knowingly. That is a legitimate
trade. What is not legitimate is letting the resulting numbers read as though
the labels were checked.

**What this actually means for every number in this repo.** Recall@10 is
"the labelled article appeared in the top ten". If the label is wrong, a
correct retrieval scores as a miss and an incorrect one can score as a hit.
So:

- **recall, MRR and nDCG are bounded above by label quality**, and the bound is
  unknown because the labels are unchecked;
- **the ablation table's *differences* are more trustworthy than its levels.**
  Every configuration is scored against the same labels, so a shared label
  error mostly cancels in the comparison and mostly does not cancel in the
  absolute number;
- **faithfulness does not depend on the labels at all.** It asks whether a
  claim is supported by the passage the answer cited, which is checked against
  the retrieved text, not against `relevant_articles`. It is the one answer
  metric here that is independent of the eval set's provenance;
- **refusal accuracy depends entirely on the labels**, since `answerable` is a
  label like any other, and there are only five unanswerable questions.

**How it is enforced rather than merely intended.** A caveat in a README is a
promise; these are checks:

- `eval/run_eval.py`, `eval/ablate.py` and `eval/run_answer_eval.py` each print
  the verified count before any metric, and print a block warning when it is
  zero;
- every results JSON carries `provenance.human_verified`;
- `GET /healthz` returns `eval_set_verified: false` with a note, so a service
  consumer sees it without reading the repo;
- the README states it above the tables, not in a footnote.

**The two verified labels are q024 and q025**, both reviewed in the browser
tool before review was called off. Two is not a sample; it is not enough to
estimate a label error rate, and this document does not pretend otherwise. The
honest summary is: *66 of 68 labels have been read by no one*.

## 2026-08-22 — Refusal is a sentinel from the model, not a phrase match

**Chosen:** the answer prompt instructs the model to reply with the exact token
`NO_ANSWER` and nothing else when the passages do not contain the answer;
`parse_refusal()` tests for that token.

**Rejected:** matching refusal phrases ("لا تتوفر", "I don't have enough
information", "the documents do not state"), which is what this repo did first.

**Why:** phrase matching is wrong in both directions, and both directions were
observed. It missed refusals phrased in a way the list did not anticipate, and
worse, it counted *hedged answers* as refusals -- "I cannot be certain, but the
period is sixty days" contains the refusal phrase and is not a refusal. That
error inflates refusal accuracy and deflates the false refusal rate at the same
time, which makes a system look cautious precisely when it is being reckless.

A sentinel is a single string under the prompt's control, and the failure mode
is loud: if the model stops emitting it, refusal accuracy collapses to zero
rather than drifting quietly.

**The cost:** it depends on instruction-following. A weaker model that ignores
the instruction breaks this. That is a reason to check the sentinel rate when
changing models, not a reason to go back to guessing from prose.

## 2026-08-22 — Faithfulness is checked per claim, and the score is claim-weighted

**Chosen:** split the answer into claims, ask a cheap model whether the cited
passage supports each one, and report supported claims over total claims across
the whole run.

**Rejected:** a per-answer 0/1 "is this answer faithful"; the mean of
per-question faithfulness rates.

**Why per claim:** an answer that gets four things right and invents a fifth is
not 0% faithful and it is not 100% faithful. Per-claim scoring is the only
resolution at which "mostly grounded, one invented deadline" is expressible,
and the invented deadline is exactly the failure that matters in a legal
setting.

**Why claim-weighted and not the mean of rates:** a one-claim answer and a
six-claim answer would otherwise count equally, so a system could improve its
score by answering tersely. The run reports 116 claims across 43 answers, so
the weighting is not a rounding detail.

**What it does not measure:** whether the claim is true. It measures whether
the cited passage says it. An answer that faithfully restates an article that
does not govern the question scores 1.0 on faithfulness and 0.0 on citation
correctness -- which is why both are reported and neither is called "accuracy".

## 2026-08-22 — A reviewer rejected q054; the set is 67 questions, not 68

**What happened:** during review, q054 (*"كم الحد الادنى للراتب بالريال اللي
لازم ياخذه اي موظف؟"* — what is the minimum wage) was **rejected** and removed.
Its own `confidence_reason` had flagged the risk: the label was drafted as
`answerable: false` against the Labor Law alone, *before* the Implementing
Regulations were indexed, and the Regulations may well cover it. A question
whose correct label is unknown is worse than no question, because it scores
the retriever against a coin flip.

**Consequences, stated rather than smoothed over:**

- **Counts differ between runs, and both are reported as measured.** The
  answer-quality run covers **68** questions; it completed before the
  rejection. The retrieval and ablation runs cover the **53 answerable** of the
  current **67**. Nothing has been back-fitted to make the two agree.
- **Two labels are human-verified** (q024, q025) and **one was rejected**
  (q054). That is the entire extent of human review: 64 of the remaining 67
  labels have been read by no one.

**A test had to change, and the change is the interesting part.**
`test_the_sample_is_the_23_question_set` asserted `len(state.order) == 23` and
failed the moment the data legitimately changed. The sample size is *derived* —
every low-confidence label, plus 5 unanswerable and 10 answerable — so pinning
the literal total tested the data, not the recipe. It now asserts the
composition: the three groups are disjoint, every low-confidence label is
included, and the two samples are exactly 5 and 10. That test would have caught
a broken sampler; the old one caught a reviewer doing their job.

## 2026-08-22 — Answer cost is recorded split, not just totalled

**Chosen:** `Answer` carries `answer_cost_usd` and `faithfulness_cost_usd`
separately, with token counts for each, and the eval reports both plus a
per-claim faithfulness cost.

**Rejected:** a single `cost_usd`, which is what it had.

**Why:** the two calls scale differently. Answer generation is **one call per
question**; the faithfulness check is **one call per claim**, and a run
produced 116 claims across 43 answered questions — 2.7 claims each. A cost
projection built on the total is therefore insensitive to the one lever that
actually moves it at volume. Splitting them makes the arithmetic in the
README's cost section derivable from measurements rather than asserted.

**Retrieval is reported as latency, not dollars.** The embedder and the
cross-encoder run locally, so retrieval makes no API call and has no per-query
price this repo can honestly attribute. What it has is machine time, which is
measured. Any dollar figure for it depends on an instance rate that is an
assumption about deployment, so where the README projects one, the rate is
stated inline and the arithmetic is left visible.

## 2026-08-22 — Repo 04 is an optional dependency, over HTTP, that can be absent

**Chosen:** `qa/router.py` calls repo 04's `/route` when `DIALECT_SERVICE_URL`
is set, with a 1.5-second timeout, catching everything, and returning
"unknown" on any failure.

**Rejected:** importing repo 04; making it a hard dependency of `/ask`.

**Why HTTP and not an import:** repo 04 pulls in torch, transformers and a
model checkpoint. Repo 02 already loads two large models of its own onto one
8 GB card. Two processes, two lifecycles, one small JSON call between them is
the correct shape, and it is also the shape this would take in production.

**Why the outage path is the important one.** A classifier that is down, slow,
or returning nonsense must not change what `/ask` returns. `tests/test_router.py`
covers the connection refused, timeout, 500, and not-configured cases and
asserts the same degraded result in each: `route="default"`, `dialect=None`,
and an answer that is exactly what it would have been without routing.

**What is NOT claimed.** The dialect is reported in the response and nothing is
done with it. Whether routing on dialect improves retrieval has **not been
measured**, and asserting a benefit from an integration that has not been
A/B tested is the kind of claim this repo exists to avoid making. The hook is
real; the feature is not yet a feature.

## 2026-08-23 — The container build was broken, and the README claimed it worked

**What happened:** `docker build .` failed at
`ERROR: No matching distribution found for agent-core`. The README documented
`make deploy` as a working command. It was not.

**The cause is a real packaging seam, not a typo.** This repo depends on
`agent-core` as a **local path**, declared under `[tool.uv.sources]`. That is a
**uv** mechanism. `pip` inside the image does not read it, sees a bare
`agent-core` requirement in `dependencies`, and looks for it on PyPI, where it
does not exist. And even if pip did understand it, the sibling directory is
outside a build context rooted at this repo, so the files are not there to
install.

**The fix:** build from the **parent** directory, so both repos are in scope.

```
docker build -f bilingual-rag/Dockerfile ..
```

`docker compose` does this via `context: ..`, so `make deploy` is unchanged for
the user. `agent-core` is pip-installed into `/opt/agent-core` first, which
satisfies the requirement before this repo is installed.

**Rejected alternatives:**

- **Publishing `agent-core` to a package index.** Correct for a real
  deployment, unavailable here, and it would make the container depend on a
  release cadence rather than on the code in the tree.
- **Vendoring a copy of `agent-core` into this repo.** Duplicated source that
  nothing checks -- the exact failure mode repo 03 goes out of its way to guard
  against for one small function, let alone a whole package.
- **Making `agent-core` optional.** `qa/provider.py` imports it
  unconditionally, and the answer layer does not exist without it.

**The lesson worth keeping:** `make test` passing says nothing about whether
the container builds, and a build that works in a uv-managed venv says nothing
about whether it works under pip. The only thing that establishes it is running
the build, which is what found this.

## 2026-08-23 — `localhost` in config.yaml, and the second thing the container build found

**What happened:** with the image finally building, the API container started
and died with `qdrant_client.http.exceptions.ResponseHandlingException:
[Errno 111] Connection refused`.

**The cause:** `config.yaml` sets `qdrant.url: http://localhost:6333`, which is
correct on a laptop and wrong inside a container, where Qdrant is a *sibling
service* on the compose network and `localhost` is the API container itself.
`docker-compose.yml` had been setting `QDRANT_URL: http://qdrant:6333` since it
was written -- and **nothing read it**.

**The fix:** `load_settings()` applies the environment on top of the file, for
the values that are properties of *where* the service runs rather than of what
it does. Right now that is one value.

```python
qdrant_url = os.environ.get("QDRANT_URL")
if qdrant_url:
    settings.qdrant.url = qdrant_url
```

**Why the environment and not another config file:** a second config file
means two places to look and two things to keep in step. A URL that changes per
deployment is exactly what an environment variable is for, and the compose file
already documented the intent -- the code just never honoured it.

**The pattern across both container bugs.** Neither was findable by reading.
`make test` passed throughout, the compose file *said* the right thing, and the
Dockerfile *looked* right. What found them was running `docker compose up` and
watching it fail twice. A deployment path that has never been executed is not a
deployment path; it is a description of one.

## 2026-08-23 — The false-refusal rate was two failures added together

**What the headline said:** 18.9% of answerable questions were declined
(`claude-sonnet-5`), 34.0% (`gpt-5.6-terra`).

**What it hid:** a refusal has two causes with opposite fixes.

- **Retrieval miss** — the governing article was never in the passages the
  model saw. Refusing is then *correct*, and counting it as an answer-layer
  failure blames the wrong half of the system.
- **Over-cautious answering** — the article was in front of the model and it
  declined anyway. That is the answer-layer defect.

**The split is free to measure**, because retrieval is deterministic: the same
query against the same index returns the same passages, so the passages every
refused question was shown can be recovered exactly without re-running a model.
`eval/diagnose_refusals.py` does it, and it costs nothing.

| | claude-sonnet-5 | gpt-5.6-terra |
|---|---|---|
| Refusals | 10 of 53 | 18 of 53 |
| of which retrieval misses | 5 | 9 |
| of which over-cautious | 5 | 9 |
| **Corrected rate** (over-cautious / retrieval succeeded) | **0.139** (5/36) | **0.250** (9/36) |

Both split exactly evenly, on two different models, which is a coincidence of
small numbers and is reported as one.

**The finding that matters is the one nobody was looking for.** The same
classification shows that **17 of the 53 answerable questions — 32% — never had
the governing article retrieved**, and that Sonnet **answered 12 of those 17
anyway**: 22.6% of all answerable questions were answered from passages that do
not contain the article the question was about.

**And faithfulness did not catch a single one.** It stayed at 0.922, because
faithfulness asks whether each claim is supported by the passage the answer
*cited* — and those answers cited real passages, just not the right ones. A
system can be perfectly faithful to the wrong document.

This is the strongest argument in this repo for the design decision to report
**citation correctness (0.660) beside faithfulness (0.922)** rather than either
alone. Faithfulness measures internal consistency; citation correctness is the
only one of the two that is about being right.

**What it changes:** the answer prompt is not the first thing to fix. A third
of answerable questions are a retrieval problem, and the answer layer's own
defect is 13.9%, not 18.9%. Both numbers still rest on unverified labels — a
wrong label makes a correct retrieval look like a miss — so this is a sharper
hypothesis, not a settled conclusion.
