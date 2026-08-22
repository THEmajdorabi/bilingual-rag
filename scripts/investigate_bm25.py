"""Why does adding BM25 cost -0.170 recall@10?

A drop that large from adding a retriever is more likely a bug than a finding,
so this tests the three plausible bugs before the number gets published:

  1. **Equal weighting.** Standard RRF weights every ranking the same. If BM25
     is much weaker than dense, equal weight lets it drag good results down.
     Swept over dense:BM25 weights.
  2. **Candidate depth.** BM25 contributes its top 50. Each one it ranks at all
     gets RRF credit, so a deep list of near-misses can outvote a short list of
     hits. Swept over BM25 depth.
  3. **The stemmer.** `light_stem` was written for formal legal Arabic. The
     rewritten eval set is Gulf colloquial. If the stemmer mangles colloquial
     forms, BM25 is querying for tokens that are not in the index.

Reports what each intervention is worth, and prints the stemmer's actual
output on colloquial query terms so the third can be judged by eye.

Run: python scripts/investigate_bm25.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from metrics import aggregate  # noqa: E402
from schema import EvalSet  # noqa: E402

from bilingual_rag.arabic.normalize import normalize_ar  # noqa: E402
from bilingual_rag.arabic.stem import light_stem  # noqa: E402
from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.index.fusion import rrf  # noqa: E402
from bilingual_rag.index.lexical import BM25Index, tokenize  # noqa: E402
from bilingual_rag.index.retriever import Retriever  # noqa: E402
from bilingual_rag.ingest.pipeline import load_chunks  # noqa: E402

TOP_K = 10


def articles_of(ids: list[str], lookup: dict[str, str], limit: int) -> list[str]:
    """Map chunk ids to distinct `doc_id:article` keys, in rank order."""
    out: list[str] = []
    for chunk_id in ids:
        key = lookup.get(chunk_id)
        if key and key not in out:
            out.append(key)
        if len(out) >= limit:
            break
    return out


def score(rows: list[tuple[list[str], list[str]]]) -> tuple[float, float]:
    metrics = aggregate([(r, g, 0.0) for r, g in rows])
    return metrics.recall_at_10, metrics.mrr_at_10


def main() -> int:
    settings = load_settings()
    chunks = load_chunks(ROOT / "data" / "chunks.jsonl")
    lookup = {c.id: f"{c.doc_id}:{c.article_no}" for c in chunks if c.article_no}

    evalset = EvalSet.load(ROOT / "eval" / "questions.yaml")
    questions = [q for q in evalset.questions if q.answerable]

    retriever = Retriever(qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()))
    bm25 = BM25Index.load(ROOT / "data" / "bm25_index")

    # Collect each retriever's ranking once; the sweeps only re-fuse.
    dense_rankings: list[list[str]] = []
    bm25_rankings: list[list[str]] = []
    for question in questions:
        vector = retriever.embedder.embed_query(question.query)
        dense_rankings.append(retriever.store.search_ids(vector, top_k=50))
        bm25_rankings.append(bm25.search_ids(question.query, top_k=50))

    gold = [q.qualified_articles() for q in questions]

    print("=" * 72)
    print("1. EACH RETRIEVER ALONE")
    print("=" * 72)
    dense_only = [
        (articles_of(d, lookup, TOP_K), g) for d, g in zip(dense_rankings, gold, strict=True)
    ]
    bm25_only = [
        (articles_of(b, lookup, TOP_K), g) for b, g in zip(bm25_rankings, gold, strict=True)
    ]
    d_recall, d_mrr = score(dense_only)
    b_recall, b_mrr = score(bm25_only)
    print(f"  dense only   recall@10={d_recall:.3f}  mrr@10={d_mrr:.3f}")
    print(f"  BM25 only    recall@10={b_recall:.3f}  mrr@10={b_mrr:.3f}")
    print(f"\n  BM25 alone finds the answer for {b_recall:.0%} of questions.")
    print("  If that is very low, fusing it in can only add noise.")

    print("\n" + "=" * 72)
    print("2. WEIGHTED RRF  (dense weight : BM25 weight)")
    print("=" * 72)
    for dense_w, bm25_w in [(1, 1), (2, 1), (3, 1), (5, 1), (10, 1), (1, 0)]:
        rows = []
        for dense, lexical, g in zip(dense_rankings, bm25_rankings, gold, strict=True):
            fused = [cid for cid, _ in rrf([dense, lexical], weights=[dense_w, bm25_w])]
            rows.append((articles_of(fused, lookup, TOP_K), g))
        recall, mrr = score(rows)
        label = f"{dense_w}:{bm25_w}"
        note = "  <- equal weighting, the current default" if (dense_w, bm25_w) == (1, 1) else ""
        note = "  <- BM25 disabled entirely" if bm25_w == 0 else note
        print(f"  dense:bm25 = {label:<6} recall@10={recall:.3f}  mrr@10={mrr:.3f}{note}")

    print("\n" + "=" * 72)
    print("3. BM25 CANDIDATE DEPTH  (equal weights)")
    print("=" * 72)
    for depth in (5, 10, 20, 30, 50):
        rows = []
        for dense, lexical, g in zip(dense_rankings, bm25_rankings, gold, strict=True):
            fused = [cid for cid, _ in rrf([dense, lexical[:depth]])]
            rows.append((articles_of(fused, lookup, TOP_K), g))
        recall, mrr = score(rows)
        note = "  <- current default" if depth == 50 else ""
        print(f"  bm25 top-{depth:<3} recall@10={recall:.3f}  mrr@10={mrr:.3f}{note}")

    print("\n" + "=" * 72)
    print("4. STEMMER ON COLLOQUIAL QUERY TERMS")
    print("=" * 72)
    print("  The stemmer was written for formal legal Arabic. These are words")
    print("  from the rewritten eval set. A stem that is not a real root, or")
    print("  that collides with an unrelated word, is a bug.\n")
    colloquial = [
        "يبوني",
        "طلعوني",
        "ابغى",
        "اشتغلت",
        "مديري",
        "راتبي",
        "الشغل",
        "بالاسبوع",
        "اجازتي",
        "يخليني",
        "بيخصمون",
        "تروح",
        "ينفع",
        "وش",
        "كذا",
        "هالسنه",
        "فلوس",
        "ورقه",
        "حقوقي",
        "مستحقات",
    ]
    for word in colloquial:
        normalized = normalize_ar(word)
        stem = light_stem(normalized)
        flag = "  <-- stem is 3 chars or fewer" if len(stem) <= 3 else ""
        print(f"  {word:<12} -> normalize {normalized:<12} -> stem {stem:<12}{flag}")

    print("\n  How many query tokens survive into the index vocabulary?")
    vocabulary = set()
    for chunk in chunks:
        vocabulary.update(tokenize(chunk.text_norm, stem=True))

    total = 0
    missing = 0
    examples: list[str] = []
    for question in questions:
        for token in tokenize(question.query, stem=True):
            total += 1
            if token not in vocabulary:
                missing += 1
                if len(examples) < 15 and token not in examples:
                    examples.append(token)
    print(
        f"  query tokens: {total}, absent from the index vocabulary: "
        f"{missing} ({missing / max(1, total):.0%})"
    )
    print(f"  examples: {', '.join(examples)}")

    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    if b_recall < 0.35:
        print(f"  BM25 alone reaches only {b_recall:.3f} recall@10. On situation-framed")
        print("  colloquial queries it has little lexical signal to work with, so")
        print("  equal-weight fusion mostly injects noise. This is a property of the")
        print("  query style, not obviously a bug in the fusion code.")
    else:
        print(f"  BM25 alone reaches {b_recall:.3f}, which is respectable. If fusion still")
        print("  hurts, suspect the fusion path rather than the retriever.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
