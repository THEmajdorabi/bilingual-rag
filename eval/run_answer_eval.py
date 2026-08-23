"""Measure answer quality: faithfulness, refusal accuracy, citation correctness.

Separate from `run_eval.py` because this one costs money, every question is a
model call, plus one cheap call per claim for the faithfulness check.

Four metrics, and the two that matter most are about *not* answering:

  * **faithfulness**: of the claims that cite a passage, how many does that
    passage actually support;
  * **refusal accuracy**: on questions labelled unanswerable, how often does
    the system correctly decline;
  * **false refusal rate**: on answerable questions, how often does it
    decline anyway. A system that refuses everything scores 100% on refusal
    accuracy, so the two must be read together;
  * **citation validity**: how often it cites an article it was never given.
    Any non-zero value here is a serious defect.

PROVENANCE: the eval set is model-drafted and UNVERIFIED. Every number below
is provisional.

    python eval/run_answer_eval.py                 # all questions
    python eval/run_answer_eval.py --limit 10      # a cheap subset
    python eval/run_answer_eval.py --use-agent     # route through agent-core
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from schema import EvalSet  # noqa: E402

from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.index.retriever import Retriever  # noqa: E402
from bilingual_rag.qa.answer import Answerer  # noqa: E402


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    import math

    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--use-agent", action="store_true")
    parser.add_argument(
        "--model-set",
        default="",
        help="a suffix under `models:` in config.yaml. '' = the Anthropic default, "
        "'openai' = answer_openai + faithfulness_openai.",
    )
    parser.add_argument("--out", default="eval/results/answers.json")
    args = parser.parse_args()

    evalset = EvalSet.load(ROOT / "eval" / "questions.yaml")
    questions = evalset.questions[: args.limit] if args.limit else evalset.questions

    verified = len(evalset.verified())
    print("PROVENANCE")
    print(f"  questions      : {len(questions)}")
    print(f"  human-verified : {verified} of {len(evalset.questions)}")
    if verified == 0:
        print()
        print("  !! The eval set is model-drafted and UNVERIFIED. Every number")
        print("     below measures the pipeline, not its quality, and must be")
        print("     reported as provisional.")
    print()

    settings = load_settings()
    retriever = Retriever(qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()))
    retriever.load_bm25()
    answerer = Answerer(settings=settings, model_set=args.model_set)

    agent = None
    if args.use_agent:
        from bilingual_rag.qa.agent import ResearchAgent

        agent = ResearchAgent(retriever, settings)

    rows: list[dict] = []
    started_all = time.perf_counter()

    for index, question in enumerate(questions, start=1):
        started = time.perf_counter()
        if agent is not None:
            passages, _ = agent.gather(question.query)
        else:
            passages = retriever.retrieve(question.query).passages
        retrieval_ms = (time.perf_counter() - started) * 1000

        answer = answerer.answer(question.query, passages)
        elapsed = (time.perf_counter() - started) * 1000

        cited = {c.article_no for c in answer.citations if c.article_no}
        expected = set(question.relevant_articles)

        rows.append(
            {
                "id": question.id,
                "answerable": question.answerable,
                "refused": answer.refused,
                "expected_articles": sorted(expected),
                "cited_articles": sorted(cited),
                "cited_a_correct_article": bool(cited & expected),
                "claims_checked": answer.claims_checked,
                "claims_supported": answer.claims_supported,
                "faithfulness": answer.faithfulness,
                "hallucinated_citations": answer.hallucinated_citations,
                "latency_ms": round(elapsed, 1),
                "retrieval_ms": round(retrieval_ms, 1),
                "cost_usd": round(answer.cost_usd, 6),
                "answer_cost_usd": round(answer.answer_cost_usd, 6),
                "faithfulness_cost_usd": round(answer.faithfulness_cost_usd, 6),
                "answer_input_tokens": answer.answer_input_tokens,
                "answer_output_tokens": answer.answer_output_tokens,
                "faithfulness_input_tokens": answer.faithfulness_input_tokens,
                "faithfulness_output_tokens": answer.faithfulness_output_tokens,
                "answer_text": answer.text,
                "lang": answer.lang,
                "verified_by": question.verified_by,
            }
        )
        print(
            f"  [{index:>2}/{len(questions)}] {question.id} "
            f"{'REFUSED' if answer.refused else 'answered'} "
            f"cited={sorted(cited) or '-'} "
            f"faith={answer.faithfulness if answer.faithfulness is not None else '-'} "
            f"{elapsed:.0f}ms"
        )

    answerable = [r for r in rows if r["answerable"]]
    unanswerable = [r for r in rows if not r["answerable"]]
    checked = [r for r in rows if r["claims_checked"]]

    total_claims = sum(r["claims_checked"] for r in rows)
    supported_claims = sum(r["claims_supported"] for r in rows)

    summary = {
        "questions": len(rows),
        "human_verified": verified,
        "eval_set_verified": verified == len(evalset.questions) and verified > 0,
        # Faithfulness over every checked claim, not the mean of per-question
        # rates: a question with one claim should not weigh the same as one
        # with six.
        "faithfulness": round(supported_claims / total_claims, 4) if total_claims else None,
        "claims_checked": total_claims,
        "questions_with_claims": len(checked),
        "refusal_accuracy": (
            round(sum(1 for r in unanswerable if r["refused"]) / len(unanswerable), 4)
            if unanswerable
            else None
        ),
        "false_refusal_rate": (
            round(sum(1 for r in answerable if r["refused"]) / len(answerable), 4)
            if answerable
            else None
        ),
        "cited_a_correct_article": (
            round(sum(1 for r in answerable if r["cited_a_correct_article"]) / len(answerable), 4)
            if answerable
            else None
        ),
        "hallucinated_citation_questions": sum(1 for r in rows if r["hallucinated_citations"]),
        "p50_latency_ms": round(percentile([r["latency_ms"] for r in rows], 0.50), 1),
        "p95_latency_ms": round(percentile([r["latency_ms"] for r in rows], 0.95), 1),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        # The cost breakdown. Retrieval makes no API call, so its share of the
        # bill is machine time, reported as latency rather than as dollars this
        # repo cannot honestly attribute.
        "cost_per_question_usd": round(sum(r["cost_usd"] for r in rows) / len(rows), 6),
        "answer_cost_usd": round(sum(r["answer_cost_usd"] for r in rows), 4),
        "faithfulness_cost_usd": round(sum(r["faithfulness_cost_usd"] for r in rows), 4),
        "answer_cost_per_question_usd": round(
            sum(r["answer_cost_usd"] for r in rows) / len(rows), 6
        ),
        "faithfulness_cost_per_question_usd": round(
            sum(r["faithfulness_cost_usd"] for r in rows) / len(rows), 6
        ),
        "faithfulness_cost_per_claim_usd": (
            round(sum(r["faithfulness_cost_usd"] for r in rows) / total_claims, 6)
            if total_claims
            else None
        ),
        "answer_input_tokens": sum(r["answer_input_tokens"] for r in rows),
        "answer_output_tokens": sum(r["answer_output_tokens"] for r in rows),
        "faithfulness_input_tokens": sum(r["faithfulness_input_tokens"] for r in rows),
        "faithfulness_output_tokens": sum(r["faithfulness_output_tokens"] for r in rows),
        "p50_retrieval_ms": round(percentile([r["retrieval_ms"] for r in rows], 0.50), 1),
        "p95_retrieval_ms": round(percentile([r["retrieval_ms"] for r in rows], 0.95), 1),
        "wall_s": round(time.perf_counter() - started_all, 1),
        "used_agent": args.use_agent,
        "model_set": args.model_set or "default",
        "answer_model": settings.models[answerer._key("answer")].id,
        "faithfulness_model": settings.models[answerer._key("faithfulness")].id,
    }

    print()
    print("ANSWER QUALITY" + ("  (agent routing)" if args.use_agent else ""))
    print("| Metric | Value |")
    print("|---|---|")
    for key, value in summary.items():
        print(f"| {key} | {value} |")

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"summary": summary, "per_question": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
