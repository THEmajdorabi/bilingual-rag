"""Split the false-refusal rate into retrieval misses and over-cautious answering.

A false refusal — declining a question the corpus does answer — has two very
different causes, and the headline rate does not distinguish them:

  * **retrieval miss.** The governing article was not in the passages the model
    was shown. Refusing is then the CORRECT behaviour, and the defect is in
    retrieval. Counting it as an answer-layer failure blames the wrong half.
  * **over-cautious answering.** The governing article WAS in front of the
    model and it declined anyway. That is the real defect in the answer layer.

The split is measurable because retrieval is deterministic: the same query
against the same index returns the same passages, so the passages each refused
question was shown can be recovered exactly, without re-running the model or
spending anything.

    python eval/diagnose_refusals.py
    python eval/diagnose_refusals.py --answers eval/results/answers_openai.json

Needs Qdrant and the ingested corpus. Makes no API call and costs nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from schema import EvalSet  # noqa: E402

from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.index.retriever import Retriever  # noqa: E402


def classify(rows: list[dict], evalset: EvalSet, retriever: Retriever, top_k: int) -> list[dict]:
    """For every answerable question, record whether retrieval found the article."""
    by_id = {q.id: q for q in evalset.questions}
    out: list[dict] = []

    for row in rows:
        if not row["answerable"]:
            continue
        question = by_id.get(row["id"])
        if question is None:
            # The eval set changed after the answer run. Skipped rather than
            # guessed at, and counted in the report.
            out.append({"id": row["id"], "status": "dropped_from_eval_set"})
            continue

        passages = retriever.retrieve(question.query, top_k=top_k).passages
        retrieved = {p.article_no for p in passages if p.article_no}
        expected = set(question.relevant_articles)
        found = bool(retrieved & expected)

        out.append(
            {
                "id": row["id"],
                "refused": bool(row["refused"]),
                "expected_articles": sorted(expected),
                "retrieved_articles": sorted(retrieved),
                "article_retrieved": found,
                "cited_a_correct_article": bool(row.get("cited_a_correct_article")),
                # The four-way split. `correct_refusal` is not a defect in the
                # answer layer; `over_cautious` is.
                "status": (
                    ("correct_refusal" if not found else "over_cautious")
                    if row["refused"]
                    else ("answered_with_article" if found else "answered_without_article")
                ),
            }
        )
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", default="eval/results/answers.json")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    payload = json.loads((ROOT / args.answers).read_text(encoding="utf-8"))
    rows = payload["per_question"]
    summary = payload["summary"]

    evalset = EvalSet.load(ROOT / "eval" / "questions.yaml")
    settings = load_settings()
    retriever = Retriever(qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()))
    retriever.load_bm25()

    detail = classify(rows, evalset, retriever, args.top_k)
    counts: dict[str, int] = {}
    for item in detail:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

    answerable = [d for d in detail if d["status"] != "dropped_from_eval_set"]
    refused = [d for d in answerable if d.get("refused")]
    correct = counts.get("correct_refusal", 0)
    over = counts.get("over_cautious", 0)

    model = summary.get("answer_model", "claude-sonnet-5 (pre-instrumentation)")
    print(f"FALSE REFUSALS, SPLIT BY CAUSE   ({args.answers}, {model})")
    print()
    print(f"  answerable questions scored     : {len(answerable)}")
    print(f"  refused                         : {len(refused)}")
    if summary.get("false_refusal_rate") is not None:
        print(f"  reported false refusal rate     : {summary['false_refusal_rate']:.4f}")
    print()
    print("| Outcome | n | share of refusals | What it means |")
    print("|---|---|---|---|")
    total_refusals = len(refused) or 1
    print(
        f"| **retrieval miss** (article absent) | {correct} | "
        f"{correct / total_refusals:.1%} | Refusing was CORRECT. The defect is retrieval. |"
    )
    print(
        f"| **over-cautious** (article present) | {over} | "
        f"{over / total_refusals:.1%} | The model had the article and declined. Answer-layer defect. |"
    )
    misses = correct + counts.get("answered_without_article", 0)
    hits = len(answerable) - misses
    print()
    print("The denominators that matter, and are not in the headline rate:")
    print(f"  questions where retrieval FOUND a labelled article : {hits} of {len(answerable)}")
    print(f"  questions where it did NOT                         : {misses} of {len(answerable)}")
    print()
    print(
        f"  reported false refusal rate                        : "
        f"{len(refused) / len(answerable):.4f}   ({len(refused)}/{len(answerable)})"
    )
    print(
        f"  CORRECTED: over-cautious / retrieval succeeded      : "
        f"{over / hits:.4f}   ({over}/{hits})"
    )
    print(
        f"  answered WITHOUT the labelled article              : "
        f"{counts.get('answered_without_article', 0) / len(answerable):.4f}"
        f"   ({counts.get('answered_without_article', 0)}/{len(answerable)})"
    )
    print()
    print("Control — the questions it DID answer:")
    print(f"  answered with the article retrieved   : {counts.get('answered_with_article', 0)}")
    print(
        f"  answered WITHOUT the article retrieved: {counts.get('answered_without_article', 0)}"
        "   <- answered from passages that do not contain the labelled article"
    )
    if counts.get("dropped_from_eval_set"):
        print(f"  dropped (no longer in the eval set)    : {counts['dropped_from_eval_set']}")

    print()
    if over > correct:
        print("VERDICT: over-cautious answering dominates. The answer prompt is the thing to fix.")
    elif correct > over:
        print("VERDICT: retrieval misses dominate. The refusals are mostly the system being right.")
    else:
        print("VERDICT: evenly split.")

    out_path = ROOT / (args.out or args.answers.replace(".json", "_refusal_split.json"))
    out_path.write_text(
        json.dumps(
            {
                "source": args.answers,
                "answer_model": model,
                "top_k": args.top_k,
                "answerable_scored": len(answerable),
                "refusals": len(refused),
                "counts": counts,
                "retrieval_miss_refused": correct,
                "over_cautious": over,
                "retrieval_missed_total": correct + counts.get("answered_without_article", 0),
                "retrieval_found_total": len(answerable)
                - (correct + counts.get("answered_without_article", 0)),
                "false_refusal_rate_reported": round(len(refused) / len(answerable), 4)
                if answerable
                else None,
                "false_refusal_rate_corrected": round(
                    over
                    / (len(answerable) - (correct + counts.get("answered_without_article", 0))),
                    4,
                )
                if answerable
                else None,
                "answered_without_article_rate": round(
                    counts.get("answered_without_article", 0) / len(answerable), 4
                )
                if answerable
                else None,
                "per_question": detail,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
