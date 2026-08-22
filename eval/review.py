"""Review eval question labels against the article text they cite.

Every number this project reports is capped by the quality of these labels. A
question drafted by a model and never checked measures the pipeline, not its
quality -- and when the drafter is related to the system being scored, it
flatters itself.

This shows one question at a time beside the **full text of the article it
cites**, and records `verified_by: human` only on an explicit yes.

**Least-confident questions come first.** `confidence` is the drafter's own
self-assessment of each label, so the shakiest get your attention while it is
freshest. Its only job is to order the queue; it is not a measurement.

    python eval/review.py --sample           # the risk-weighted partial review
    python eval/review.py                    # all drafted, least confident first
    python eval/review.py --confidence low   # only the risky ones
    python eval/review.py --unanswerable     # only refusal labels
    python eval/review.py --id q054
    python eval/review.py --status           # counts, review nothing
    python eval/review.py --search "اجازة"   # search the corpus for an article

Keys per question:
    y  label is correct              -> verified_by: human
    n  question is bad               -> removed from the file
    e  wrong article(s)              -> type the correct ones, marks verified
    a  it IS answerable after all    -> flips an unanswerable label, asks for articles
    u  it is NOT answerable          -> flips an answerable label, clears articles
    s  skip, decide later
    ?  search the corpus for text
    q  save and quit

Progress is saved on quit, and on every accept, so a long session is never
lost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from schema import EvalSet, Question  # noqa: E402

from bilingual_rag.ingest.pipeline import load_chunks  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
CHUNKS = ROOT / "data" / "chunks.jsonl"

WIDE = "=" * 78
THIN = "-" * 78

DOC_TITLES = {
    "saudi_labor_law": "نظام العمل  (Labor Law)",
    "labor_regulations": "اللائحة التنفيذية  (Implementing Regulations)",
}


def load_articles() -> dict[str, str]:
    """Map `doc_id:article` -> full article text."""
    if not CHUNKS.exists():
        print(f"\n!! {CHUNKS} not found. Run `python scripts/ingest.py` first.")
        print("   Without it this tool cannot show you the article text.\n")
        return {}
    grouped: dict[str, list[str]] = {}
    for chunk in load_chunks(CHUNKS):
        if chunk.article_no:
            grouped.setdefault(f"{chunk.doc_id}:{chunk.article_no}", []).append(chunk.text)
    return {key: "\n".join(parts) for key, parts in grouped.items()}


def wrap(text: str, width: int = 74, indent: str = "  ") -> list[str]:
    """Wrap while keeping existing line breaks, which carry list structure."""
    out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            cut = cut if cut > width // 2 else width
            out.append(indent + line[:cut])
            line = line[cut:].lstrip()
        out.append(indent + line)
    return out


def show(question: Question, articles: dict[str, str], position: int, total: int) -> None:
    marker = {"low": "!! LOW CONFIDENCE", "medium": "~  medium", "high": "   high"}
    print(f"\n{WIDE}")
    print(
        f"[{position}/{total}]  {question.id}   {marker[question.confidence]}   "
        f"category={question.category}   difficulty={question.difficulty}"
    )
    print(WIDE)

    if question.confidence_reason:
        for line in wrap(f"why: {question.confidence_reason}", indent="  "):
            print(line)

    print()
    if question.question_ar:
        print(f"  AR: {question.question_ar}")
    if question.question_en:
        print(f"  EN: {question.question_en}")
    if question.tags:
        print(f"  tags: {', '.join(question.tags)}")

    if not question.answerable:
        print(f"\n{THIN}")
        print("  LABELLED UNANSWERABLE -- the system is expected to REFUSE.")
        if question.notes:
            for line in wrap(f"note: {question.notes}"):
                print(line)
        print()
        print("  Check: is this genuinely absent from BOTH indexed documents?")
        print("  The Implementing Regulations were added to the index after some")
        print("  of these were judged. Use `?` to search before trusting the label.")
        return

    print(f"\n  labelled: {', '.join(question.qualified_articles()) or '(none)'}")
    print(f"  document: {DOC_TITLES.get(question.doc_id, question.doc_id)}")

    for key in question.qualified_articles():
        text = articles.get(key)
        print(f"\n{THIN}")
        print(f"  ARTICLE {key}")
        print(THIN)
        if text is None:
            print("  !! NOT FOUND in the index. The label is almost certainly wrong.")
            print("     Use `e` to correct it, or `?` to search for the right article.")
        else:
            for line in wrap(text):
                print(line)

    print(f"\n{THIN}")
    print("  Check: does the text above actually answer the question as asked?")


def search_corpus(articles: dict[str, str]) -> None:
    """Find which article contains a phrase, so a wrong label can be corrected."""
    needle = input("  search the corpus for > ").strip()
    if not needle:
        return
    hits = [(key, text) for key, text in articles.items() if needle in text]
    if not hits:
        print(f"  no article contains {needle!r}")
        return
    print(f"  {len(hits)} article(s) contain that text:")
    for key, text in hits[:8]:
        index = text.find(needle)
        snippet = text[max(0, index - 60) : index + 90].replace("\n", " ")
        print(f"\n    {key}")
        print(f"      ...{snippet}...")
    if len(hits) > 8:
        print(f"\n    ... and {len(hits) - 8} more")


def ask_articles(current: list[str]) -> list[str] | None:
    raw = input(f"  article numbers, comma separated (now: {','.join(current) or 'none'}) > ")
    parts = [a.strip() for a in raw.split(",") if a.strip()]
    return parts or None


def require_interactive() -> None:
    """Refuse to run when stdin is not a terminal.

    This tool exists to record *human* judgement. Piping input into it -- a
    test harness, a shell heredoc, a stray `echo` -- would mark labels
    `verified_by: human` when no human saw them, which is worse than leaving
    them unverified: it manufactures provenance.

    This guard was added after exactly that happened while testing the CLI:
    three labels came back marked human-verified and had to be reset.
    """
    if not sys.stdin.isatty():
        print(
            "\nrefusing to run: stdin is not a terminal.\n\n"
            "This CLI records human judgement, so it must be driven by a human.\n"
            "Piped input would mark labels as human-verified when nobody read them.\n\n"
            "Run it directly in a terminal, or use --status to inspect without reviewing.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def prompt() -> str:
    valid = {"y", "n", "e", "a", "u", "s", "?", "q"}
    while True:
        choice = input(
            "\n  [y]es [n]o [e]dit [a]nswerable [u]nanswerable [s]kip [?]search [q]uit > "
        )
        choice = choice.strip().lower()
        if choice in valid:
            return choice
        print(f"  expected one of: {' '.join(sorted(valid))}")


def print_status(evalset: EvalSet) -> None:
    print("\nEval set composition:\n")
    for key, value in evalset.composition().items():
        print(f"  {key:26} {value}")

    print("\nDrafter confidence (drafted questions only):\n")
    for level in ("low", "medium", "high"):
        items = [q for q in evalset.by_confidence(level) if q.verified_by == "drafted"]
        print(f"  {level:8} {len(items)}")

    verified = len(evalset.verified())
    total = len(evalset.questions)
    print(f"\n  {verified}/{total} human-verified ({verified / total:.0%})")
    if verified < total:
        print("\n  Until these are verified, every reported number measures the")
        print("  pipeline rather than its quality. Start with:")
        print("      python eval/review.py --confidence low")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="review only this question id")
    parser.add_argument("--category", help="review only this category")
    parser.add_argument("--confidence", choices=["low", "medium", "high"])
    parser.add_argument(
        "--sample",
        action="store_true",
        help="risk-weighted partial review: all low-confidence, plus a sample of "
        "high-confidence unanswerable and answerable labels",
    )
    parser.add_argument("--unanswerable", action="store_true", help="only refusal labels")
    parser.add_argument("--status", action="store_true", help="print counts and exit")
    parser.add_argument("--all", action="store_true", help="include already-verified")
    args = parser.parse_args()

    evalset = EvalSet.load(QUESTIONS)

    if args.status:
        print_status(evalset)
        return 0

    if args.sample:
        pool = evalset.review_sample()
        low = sum(1 for q in pool if q.confidence == "low")
        unanswerable = sum(1 for q in pool if not q.answerable and q.confidence != "low")
        answerable = sum(1 for q in pool if q.answerable and q.confidence != "low")
        low_unanswerable = sum(1 for q in pool if q.confidence == "low" and not q.answerable)
        print()
        print("RISK-WEIGHTED SAMPLE")
        print(
            f"  {low:2} low-confidence      (every flagged doubt, "
            f"incl. {low_unanswerable} unanswerable)"
        )
        print(
            f"  {unanswerable:2} unanswerable        (high-confidence, sampled: refusal coverage)"
        )
        print(f"  {answerable:2} answerable          (high-confidence, sampled: the control)")
        print(f"  {len(pool):2} total, disjoint groups, out of {len(evalset.questions)} questions")
        print()
        print("  The third group is the control: if high-confidence labels turn out")
        print("  wrong, the self-assessment is untrustworthy and all 68 need review.")
    else:
        pool = evalset.questions if args.all else evalset.review_order()

    if args.id:
        pool = [q for q in pool if q.id == args.id]
    if args.category:
        pool = [q for q in pool if q.category == args.category]
    if args.confidence:
        pool = [q for q in pool if q.confidence == args.confidence]
    if args.unanswerable:
        pool = [q for q in pool if not q.answerable]

    if not pool:
        print("nothing to review")
        return 0

    # Checked here, after --status and the filters, so inspection still works
    # in a pipeline and only the judgement-recording path is gated.
    require_interactive()

    articles = load_articles()
    reviewed = 0
    low_count = sum(1 for q in pool if q.confidence == "low")

    print(f"\nReviewing {len(pool)} question(s), least confident first.")
    if low_count:
        print(f"The first {low_count} are flagged LOW CONFIDENCE -- check those hardest.")
    print("Progress saves on every accept and on quit.\n")

    for position, question in enumerate(pool, start=1):
        while True:
            show(question, articles, position, len(pool))
            choice = prompt()

            if choice == "?":
                search_corpus(articles)
                continue

            if choice == "q":
                evalset.save(QUESTIONS)
                print(f"\nsaved {QUESTIONS}")
                print(f"verified this session: {reviewed}")
                print(f"human-verified total : {len(evalset.verified())}/{len(evalset.questions)}")
                return 0

            if choice == "s":
                break

            if choice == "y":
                question.verified_by = "human"
                reviewed += 1
                print(f"  {question.id} accepted")

            elif choice == "n":
                # Removed rather than left as drafted: a question nobody
                # believes should not sit in the file pretending to be data.
                evalset.questions = [q for q in evalset.questions if q.id != question.id]
                print(f"  {question.id} removed")

            elif choice == "e":
                corrected = ask_articles(question.relevant_articles)
                if corrected is None:
                    print("  no change")
                    continue
                question.relevant_articles = corrected
                question.verified_by = "human"
                reviewed += 1
                print(f"  {question.id} -> {question.qualified_articles()}")

            elif choice == "a":
                # An "unanswerable" that turns out to be covered -- the exact
                # risk created by indexing the Regulations after these were
                # judged against the Law alone.
                corrected = ask_articles(question.relevant_articles)
                if corrected is None:
                    print("  need at least one article to mark it answerable")
                    continue
                question.answerable = True
                question.relevant_articles = corrected
                question.verified_by = "human"
                reviewed += 1
                print(f"  {question.id} is answerable -> {question.qualified_articles()}")

            elif choice == "u":
                question.answerable = False
                question.relevant_articles = []
                question.relevant_chunks = []
                question.verified_by = "human"
                reviewed += 1
                print(f"  {question.id} marked unanswerable")

            evalset.save(QUESTIONS)
            break

    evalset.save(QUESTIONS)
    print(f"\nsaved {QUESTIONS}")
    print(f"verified this session: {reviewed}")
    print(f"human-verified total : {len(evalset.verified())}/{len(evalset.questions)}")
    print("\nWhen done, re-measure with:")
    print("    python eval/ablate.py --repeats 3 --verified-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
