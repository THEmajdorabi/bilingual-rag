"""Measure retrieval quality against the eval set.

Reports recall@1/5/10, hit@10, MRR@10, nDCG@10 and latency percentiles, broken
down per tag, because an average across Arabic and English hides exactly the
thing you most want to see.

**Provenance is reported before the numbers.** A figure computed over
model-drafted questions measures the pipeline, not its quality, and this script
refuses to present one as a headline. Run `python eval/review.py` to verify
questions first.

Retrieval metrics need no API key. Faithfulness and refusal quality do, and
are measured by `eval/run_answer_eval.py` instead.

    python eval/run_eval.py                     # full pipeline
    python eval/run_eval.py --config "dense only, no normalization"
    python eval/run_eval.py --verified-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import RetrievalMetrics, aggregate  # noqa: E402
from schema import EvalSet, Question  # noqa: E402

from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.index.retriever import (  # noqa: E402
    ABLATION_CONFIGS,
    RetrievalConfig,
    Retriever,
)

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"


def evaluate(
    retriever: Retriever,
    questions: list[Question],
    config: RetrievalConfig,
) -> tuple[RetrievalMetrics, list[dict]]:
    """Run every answerable question and score the retrieved articles."""
    per_question: list[tuple[list[str], list[str], float]] = []
    details: list[dict] = []

    # Warm up before timing anything. The first query loads the embedder and
    # the cross-encoder, which takes tens of seconds; letting that land on
    # whichever question happens to be first would report a p95 that says more
    # about model loading than about the pipeline.
    if questions:
        retriever.retrieve(questions[0].query, config=config)

    for question in questions:
        started = time.perf_counter()
        result = retriever.retrieve(question.query, config=config)
        latency_ms = (time.perf_counter() - started) * 1000

        # Score on article numbers: an article can span several chunks, and
        # article numbers survive re-chunking.
        # Compare `doc_id:article`, not a bare number: both documents in the
        # index number an "article 15".
        retrieved_articles: list[str] = []
        for passage in result.passages:
            if not passage.article_no:
                continue
            key = f"{passage.doc_id}:{passage.article_no}"
            if key not in retrieved_articles:
                retrieved_articles.append(key)

        per_question.append((retrieved_articles, question.qualified_articles(), latency_ms))
        details.append(
            {
                "id": question.id,
                "query": question.query,
                "expected": question.qualified_articles(),
                "retrieved": retrieved_articles[:10],
                "tags": question.tags,
                "category": question.category,
                "difficulty": question.difficulty,
                "verified_by": question.verified_by,
                "latency_ms": round(latency_ms, 1),
            }
        )

    return aggregate(per_question), details


def by_group(details: list[dict], questions: list[Question]) -> dict[str, RetrievalMetrics]:
    """Per-tag breakdown. An average over Arabic and English hides the point."""
    lookup = {q.id: q for q in questions}
    groups: dict[str, list[tuple[list[str], list[str], float]]] = {}

    for detail in details:
        question = lookup[detail["id"]]
        labels = [f"difficulty:{question.difficulty}", f"category:{question.category}"]
        labels.append("lang:en" if question.is_cross_lingual else "lang:ar")
        if question.is_multi_hop:
            labels.append("multi_hop")
        for tag in question.tags:
            labels.append(f"tag:{tag}")

        triple = (detail["retrieved"], detail["expected"], detail["latency_ms"])
        for label in labels:
            groups.setdefault(label, []).append(triple)

    return {label: aggregate(triples) for label, triples in sorted(groups.items())}


def provenance_banner(evalset: EvalSet, evaluated: list[Question]) -> str:
    """State how far these numbers can be trusted, before stating the numbers.

    Three regimes, and the difference between them matters more than the
    figures underneath:

      * nothing verified   -- measures the pipeline, not its quality;
      * partly verified    -- verification was by *sampling*, so the verified
                              subset is evidence about the labels in general
                              only to the extent the sample was representative;
      * fully verified     -- the labels are checked.
    """
    verified = sum(1 for q in evaluated if q.verified_by == "human")
    total = len(evaluated)
    whole_set = len(evalset.questions)
    whole_verified = len(evalset.verified())

    lines = [
        "PROVENANCE",
        f"  questions evaluated : {total}",
        f"  human-verified      : {verified}",
        f"  model-drafted       : {total - verified}",
        f"  eval set overall    : {whole_verified}/{whole_set} verified",
    ]

    if whole_verified == 0:
        lines += [
            "",
            "  !! These figures measure the retrieval pipeline, not its quality.",
            "     The eval set has not been checked by a human, and an eval set",
            "     written by a model flatters the system it is scoring.",
            "     Run `python eval/review.py --sample` before quoting these numbers.",
        ]
    elif whole_verified < whole_set:
        lines += [
            "",
            f"  !! PARTIAL VERIFICATION: {whole_verified} of {whole_set} labels were checked,",
            "     selected by risk-weighted sampling rather than reviewed in full.",
            "     Unverified labels cap recall in a way no retrieval change can lift,",
            "     so quote these figures as measured against a partly-verified set.",
            "     See DECISIONS.md for the exact sampling design.",
        ]
    return "\n".join(lines)


def render(metrics: RetrievalMetrics, groups: dict[str, RetrievalMetrics], config_name: str) -> str:
    lines = [
        "",
        f"CONFIGURATION: {config_name}",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for key, value in metrics.as_dict().items():
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "PER-GROUP recall@10",
        "",
        "| Group | n | recall@10 | MRR@10 | p95 ms |",
        "|---|---|---|---|---|",
    ]
    for label, group in groups.items():
        data = group.as_dict()
        lines.append(
            f"| {label} | {data['n']} | {data['recall@10']} | {data['mrr@10']} | {data['p95_latency_ms']} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None, help="ablation config name; default is full")
    parser.add_argument(
        "--verified-only", action="store_true", help="only human-verified questions"
    )
    parser.add_argument("--out", default="eval/results/retrieval.json")
    args = parser.parse_args()

    evalset = EvalSet.load(QUESTIONS)
    questions = evalset.verified() if args.verified_only else evalset.questions
    answerable = [q for q in questions if q.answerable]

    if not answerable:
        print("no answerable questions to evaluate")
        if args.verified_only:
            print("(--verified-only was set and nothing has been verified yet)")
        return 1

    config = RetrievalConfig()
    if args.config:
        matches = [c for c in ABLATION_CONFIGS if c.name == args.config]
        if not matches:
            print(f"unknown config {args.config!r}. Known:")
            for candidate in ABLATION_CONFIGS:
                print(f"  - {candidate.name}")
            return 1
        config = matches[0]

    settings = load_settings()
    retriever = Retriever(qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()))
    retriever.load_bm25()
    if not config.use_normalization:
        retriever.load_raw_indexes()

    print(provenance_banner(evalset, answerable))

    metrics, details = evaluate(retriever, answerable, config)
    groups = by_group(details, answerable)
    print(render(metrics, groups, config.name))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "config": config.name,
                "provenance": {
                    "human_verified": sum(1 for q in answerable if q.verified_by == "human"),
                    "total": len(answerable),
                },
                "overall": metrics.as_dict(),
                "groups": {label: g.as_dict() for label, g in groups.items()},
                "per_question": details,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
