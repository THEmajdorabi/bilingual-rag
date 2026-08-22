"""The ablation table: what each component is actually worth.

Runs the eval set through progressively complete retrieval pipelines and emits
the README table. This is the single most valuable artifact in the repository,
because it is the difference between "I assembled a RAG system from a blog
post" and "I measured each component and can tell you what it bought."

Each configuration is run `--repeats` times and the **mean** is reported. Where
two rows are within noise of each other, the table says so rather than claiming
an improvement -- an unqualified +0.004 is not a result.

Retrieval metrics need no API key, so the whole table below is measurable for
free. The answer-quality rows (faithfulness, refusal) live in
`run_answer_eval.py` because they do call a paid model.

    python eval/ablate.py                 # 3 repeats, all configs
    python eval/ablate.py --repeats 1     # quick
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from metrics import aggregate, percentile  # noqa: E402
from run_eval import provenance_banner  # noqa: E402
from schema import EvalSet  # noqa: E402

from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.index.retriever import ABLATION_CONFIGS, RetrievalConfig, Retriever  # noqa: E402

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"

# Two means closer than this are reported as indistinguishable rather than as
# an improvement. With ~67 questions, a difference below this is noise.
NOISE_THRESHOLD = 0.02


def run_config(retriever: Retriever, questions: list, config: RetrievalConfig) -> dict:
    """One pass of the eval set through one configuration."""
    triples = []
    for question in questions:
        started = time.perf_counter()
        result = retriever.retrieve(question.query, config=config)
        latency_ms = (time.perf_counter() - started) * 1000

        articles: list[str] = []
        for passage in result.passages:
            if not passage.article_no:
                continue
            key = f"{passage.doc_id}:{passage.article_no}"
            if key not in articles:
                articles.append(key)

        triples.append((articles, question.qualified_articles(), latency_ms))

    metrics = aggregate(triples)
    return {
        "recall@10": metrics.recall_at_10,
        "mrr@10": metrics.mrr_at_10,
        "ndcg@10": metrics.ndcg_at_10,
        "p95_latency_ms": percentile([t[2] for t in triples], 0.95),
    }


def summarise(runs: list[dict]) -> dict:
    """Mean and standard deviation across repeats."""
    keys = runs[0].keys()
    summary = {}
    for key in keys:
        values = [run[key] for run in runs]
        summary[key] = statistics.fmean(values)
        summary[f"{key}_sd"] = statistics.pstdev(values) if len(values) > 1 else 0.0
    return summary


def render_table(rows: list[tuple[str, dict]], repeats: int) -> str:
    # Retrieval here is deterministic -- embeddings are cached, BM25 is
    # deterministic, and reranker scores are cached per (query, chunk) -- so
    # repeats re-measure the same quality figure and only latency varies.
    # Saying so matters: "mean of 3 runs" otherwise implies a variance estimate
    # on quality that this pipeline cannot produce.
    deterministic = all(data.get("recall@10_sd", 0.0) == 0.0 for _, data in rows)

    lines = [
        "",
        f"ABLATION  (mean of {repeats} run{'s' if repeats != 1 else ''})",
    ]
    if deterministic and repeats > 1:
        lines.append("  quality metrics are identical across repeats (sd = 0): retrieval is")
        lines.append("  deterministic, so the repeats measure latency variance only.")
    lines += [
        "",
        "| Configuration | recall@10 | MRR@10 | nDCG@10 | p95 latency |",
        "|---|---|---|---|---|",
    ]
    for name, data in rows:
        lines.append(
            f"| {name} | {data['recall@10']:.3f} | {data['mrr@10']:.3f} | "
            f"{data['ndcg@10']:.3f} | {data['p95_latency_ms']:.0f} ms |"
        )

    # Deltas, with noise called out rather than dressed up.
    lines += ["", "WHAT EACH COMPONENT BOUGHT", ""]
    for index in range(1, len(rows)):
        previous_name, previous = rows[index - 1]
        name, current = rows[index]
        delta = current["recall@10"] - previous["recall@10"]
        latency_delta = current["p95_latency_ms"] - previous["p95_latency_ms"]

        if abs(delta) < NOISE_THRESHOLD:
            verdict = (
                f"recall@10 {delta:+.3f} -- within noise (< {NOISE_THRESHOLD}); "
                f"not a demonstrated improvement"
            )
        else:
            verdict = f"recall@10 {delta:+.3f}"
        lines.append(f"- **{name}**: {verdict}, p95 {latency_delta:+.0f} ms")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--verified-only", action="store_true")
    parser.add_argument("--out", default="eval/results/ablation.json")
    args = parser.parse_args()

    evalset = EvalSet.load(QUESTIONS)
    pool = evalset.verified() if args.verified_only else evalset.questions
    questions = [q for q in pool if q.answerable]

    if not questions:
        print("no answerable questions to evaluate")
        return 1

    settings = load_settings()
    retriever = Retriever(qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()))
    retriever.load_bm25()
    retriever.load_raw_indexes()

    print(provenance_banner(evalset, questions))

    # Warm up outside the timed runs: the first query loads two models.
    retriever.retrieve(questions[0].query, config=ABLATION_CONFIGS[-1])

    rows: list[tuple[str, dict]] = []
    for config in ABLATION_CONFIGS:
        runs = [run_config(retriever, questions, config) for _ in range(args.repeats)]
        summary = summarise(runs)
        rows.append((config.name, summary))
        print(
            f"  {config.name:34} recall@10={summary['recall@10']:.3f} "
            f"p95={summary['p95_latency_ms']:.0f}ms"
        )

    print(render_table(rows, args.repeats))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "repeats": args.repeats,
                "questions": len(questions),
                "provenance": {
                    "human_verified": sum(1 for q in questions if q.verified_by == "human"),
                    "total": len(questions),
                },
                "noise_threshold": NOISE_THRESHOLD,
                "rows": [{"config": name, **data} for name, data in rows],
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
