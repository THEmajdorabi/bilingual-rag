"""Review drafted eval questions against the article text they cite.

The eval set is what every reported number rests on. A question drafted by a
model and never checked measures the pipeline, not its quality -- and if the
model that drafted it is related to the model being evaluated, it flatters
itself.

This CLI shows one question at a time next to the **actual text** of the
article it cites, and records `verified_by: human` only on a yes. It also lets
you fix a wrong article label, or reject a question outright.

    python eval/review.py                  # review every drafted question
    python eval/review.py --id q001        # review one
    python eval/review.py --category notice_period
    python eval/review.py --status         # composition, review nothing

Keys: [y] correct  [n] reject  [e] edit articles  [s] skip  [q] save and quit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from schema import EvalSet, Question  # noqa: E402

from bilingual_rag.ingest.pipeline import load_chunks  # noqa: E402

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"
CHUNKS = Path(__file__).resolve().parents[1] / "data" / "chunks.jsonl"

RULE = "=" * 78


def article_texts() -> dict[str, str]:
    """Map article number -> its full text, for display during review."""
    if not CHUNKS.exists():
        print(f"warning: {CHUNKS} not found. Run scripts/ingest.py first.\n")
        return {}
    grouped: dict[str, list[str]] = {}
    for chunk in load_chunks(CHUNKS):
        if chunk.article_no:
            grouped.setdefault(chunk.article_no, []).append(chunk.text)
    return {number: "\n".join(parts) for number, parts in grouped.items()}


def show(question: Question, articles: dict[str, str], position: int, total: int) -> None:
    print(f"\n{RULE}")
    print(
        f"[{position}/{total}]  {question.id}   category={question.category}   "
        f"difficulty={question.difficulty}"
    )
    print(RULE)

    if question.question_ar:
        print(f"\n  AR: {question.question_ar}")
    if question.question_en:
        print(f"\n  EN: {question.question_en}")

    if not question.answerable:
        print("\n  LABELLED UNANSWERABLE -- the system must refuse.")
        if question.notes:
            print(f"  note: {question.notes}")
        print("\n  Check: is this genuinely absent from the Labor Law?")
        return

    print(f"\n  labelled articles: {', '.join(question.relevant_articles) or '(none)'}")
    for number in question.relevant_articles:
        text = articles.get(number)
        print(f"\n  --- article {number} " + "-" * 50)
        if text is None:
            print("  !! NOT FOUND in the index. The label is probably wrong.")
        else:
            body = text[:900]
            for line in body.splitlines():
                print(f"  {line}")
            if len(text) > 900:
                print(f"  ... [{len(text) - 900} more characters]")

    print("\n  Check: does the text above actually answer the question?")


def prompt() -> str:
    while True:
        choice = input("\n  [y]es / [n]o / [e]dit / [s]kip / [q]uit > ").strip().lower()
        if choice in {"y", "n", "e", "s", "q"}:
            return choice
        print("  expected one of y, n, e, s, q")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="review only this question id")
    parser.add_argument("--category", help="review only this category")
    parser.add_argument("--status", action="store_true", help="print composition and exit")
    parser.add_argument("--all", action="store_true", help="include already-verified questions")
    args = parser.parse_args()

    evalset = EvalSet.load(QUESTIONS)

    if args.status:
        print("\nEval set composition:\n")
        for key, value in evalset.composition().items():
            print(f"  {key:26} {value}")
        verified = len(evalset.verified())
        total = len(evalset.questions)
        print(f"\n  {verified}/{total} human-verified ({verified / total:.0%})")
        if verified < total:
            print("\n  Numbers computed over drafted questions measure the pipeline,")
            print("  not its quality. Run `python eval/review.py` to verify them.")
        return 0

    pool = evalset.questions if args.all else evalset.drafted()
    if args.id:
        pool = [q for q in pool if q.id == args.id]
    if args.category:
        pool = [q for q in pool if q.category == args.category]

    if not pool:
        print("nothing to review")
        return 0

    articles = article_texts()
    reviewed = 0

    print(f"\nReviewing {len(pool)} question(s). Answers are saved on quit.")

    for position, question in enumerate(pool, start=1):
        show(question, articles, position, len(pool))
        choice = prompt()

        if choice == "q":
            break
        if choice == "s":
            continue
        if choice == "y":
            question.verified_by = "human"
            reviewed += 1
        elif choice == "n":
            # Rejected questions are removed rather than left to rot as
            # drafted: a question nobody believes should not be in the file.
            evalset.questions = [q for q in evalset.questions if q.id != question.id]
            print(f"  removed {question.id}")
        elif choice == "e":
            raw = input("  correct article numbers, comma separated > ").strip()
            question.relevant_articles = [a.strip() for a in raw.split(",") if a.strip()]
            question.verified_by = "human"
            reviewed += 1
            print(f"  {question.id} -> articles {question.relevant_articles}")

    evalset.save(QUESTIONS)
    print(f"\nsaved {QUESTIONS}")
    print(f"verified this session: {reviewed}")
    print(f"human-verified total : {len(evalset.verified())}/{len(evalset.questions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
