"""Is the eval set actually discriminating, or is it measuring vocabulary reuse?

A retrieval benchmark where the question restates the passage's own wording
measures string matching, not retrieval. It saturates near 1.0 for every
configuration, and an ablation table built on it cannot distinguish components
-- which is exactly what a 0.985 dense-only baseline looks like.

Three things are measured here:

  1. **Corpus size.** Retrieval over 300 chunks is an easier problem than over
     3,000. If there is nothing to be confused by, nothing gets confused.
  2. **Lexical overlap** between each question and its labelled article, as
     token containment: what fraction of the question's content words appear
     in the passage. High containment means the question was written by
     paraphrasing the answer.
  3. **Rank of the labelled article under BM25 alone.** Pure lexical retrieval
     is the null hypothesis. If BM25 alone puts the right article at rank 1,
     the question is answerable by string matching and tells you nothing about
     semantic retrieval.

Run: python scripts/diagnose_eval_difficulty.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from schema import EvalSet  # noqa: E402

from bilingual_rag.index.lexical import BM25Index, tokenize  # noqa: E402
from bilingual_rag.ingest.pipeline import load_chunks  # noqa: E402

# Very common Arabic function words. Overlap on these says nothing about
# whether a question reuses the article's vocabulary.
STOPWORDS = {
    tokenize(w, stem=True)[0]
    for w in [
        "من",
        "في",
        "على",
        "الى",
        "عن",
        "مع",
        "هذا",
        "هذه",
        "التي",
        "الذي",
        "ما",
        "ان",
        "او",
        "كل",
        "لا",
        "هل",
        "كم",
        "ماهي",
        "هو",
        "هي",
        "به",
        "له",
        "بها",
        "عند",
        "بعد",
        "قبل",
        "غير",
        "بين",
        "حتى",
        "اذا",
        "كان",
    ]
    if tokenize(w, stem=True)
}


def content_tokens(text: str) -> set[str]:
    """Stemmed content words, stopwords removed."""
    return {t for t in tokenize(text, stem=True) if t not in STOPWORDS and len(t) > 1}


def containment(question: str, passage: str) -> float:
    """Fraction of the question's content words that appear in the passage.

    Containment rather than Jaccard: the passage is far longer than the
    question, so Jaccard would be dominated by passage length and would
    understate reuse.
    """
    q_tokens = content_tokens(question)
    if not q_tokens:
        return 0.0
    return len(q_tokens & content_tokens(passage)) / len(q_tokens)


def main() -> int:
    chunks = load_chunks(ROOT / "data" / "chunks.jsonl")
    by_article: dict[str, str] = {}
    for chunk in chunks:
        if chunk.article_no:
            key = f"{chunk.doc_id}:{chunk.article_no}"
            by_article[key] = by_article.get(key, "") + " " + chunk.text

    print("CORPUS")
    print(f"  chunks indexed        : {len(chunks)}")
    print(f"  distinct articles     : {len(by_article)}")
    print(f"  mean chunk chars      : {statistics.fmean(len(c.text) for c in chunks):.0f}")
    print(f"  total corpus chars    : {sum(len(c.text) for c in chunks):,}")

    bm25 = BM25Index.load(ROOT / "data" / "bm25_index")

    evalset = EvalSet.load(ROOT / "eval" / "questions.yaml")
    answerable = [q for q in evalset.questions if q.answerable]

    rows = []
    for question in answerable:
        passage = " ".join(by_article.get(a, "") for a in question.qualified_articles())
        overlap = containment(question.query, passage)

        # Where does pure BM25 put the labelled article?
        ranked_ids = bm25.search_ids(question.query, top_k=len(chunks))
        id_to_article = {c.id: f"{c.doc_id}:{c.article_no}" for c in chunks}
        seen: list[str] = []
        for chunk_id in ranked_ids:
            article = id_to_article.get(chunk_id)
            if article and article not in seen:
                seen.append(article)
        rank = next(
            (i for i, a in enumerate(seen, start=1) if a in question.qualified_articles()), 999
        )
        rows.append((question, overlap, rank))

    overlaps = [o for _, o, _ in rows]
    bm25_top1 = sum(1 for _, _, r in rows if r == 1)
    bm25_top10 = sum(1 for _, _, r in rows if r <= 10)

    print("\nLEXICAL OVERLAP  (question content words found in the labelled passage)")
    print(f"  questions             : {len(rows)}")
    print(f"  mean containment      : {statistics.fmean(overlaps):.3f}")
    print(f"  median containment    : {statistics.median(overlaps):.3f}")
    print(
        f"  >= 0.50 containment   : {sum(1 for o in overlaps if o >= 0.5)} "
        f"({sum(1 for o in overlaps if o >= 0.5) / len(overlaps):.0%})"
    )
    print(
        f"  >= 0.75 containment   : {sum(1 for o in overlaps if o >= 0.75)} "
        f"({sum(1 for o in overlaps if o >= 0.75) / len(overlaps):.0%})"
    )

    print("\nBM25-ONLY BASELINE  (the null hypothesis: pure string matching)")
    print(f"  labelled article at rank 1  : {bm25_top1} ({bm25_top1 / len(rows):.0%})")
    print(f"  labelled article in top 10  : {bm25_top10} ({bm25_top10 / len(rows):.0%})")

    print("\nWORST OFFENDERS  (highest vocabulary reuse)")
    for question, overlap, rank in sorted(rows, key=lambda r: -r[1])[:10]:
        print(f"  {overlap:.2f}  bm25_rank={rank:<4} {question.id}  {question.query[:60]}")

    print("\nHARDEST  (lowest reuse -- these are the questions worth having)")
    for question, overlap, rank in sorted(rows, key=lambda r: r[1])[:8]:
        print(f"  {overlap:.2f}  bm25_rank={rank:<4} {question.id}  {question.query[:60]}")

    print("\nVERDICT")
    mean_overlap = statistics.fmean(overlaps)
    if mean_overlap >= 0.5 or bm25_top1 / len(rows) >= 0.5:
        print("  The eval set largely restates its own answers. Retrieval scores")
        print("  measure vocabulary overlap, and the ablation cannot discriminate.")
    else:
        print("  Vocabulary reuse is within a reasonable range.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
