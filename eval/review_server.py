"""Browser-based review of eval question labels.

Replaces the terminal CLI, which is unusable on Windows: `cmd` renders Arabic
as question marks and does no bidi reordering or letter joining, so an Arabic
legal passage is literally unreadable there. A browser does all three
correctly, which is the whole reason this exists.

Same job, same actions, same file. One question at a time, the full text of
every cited article beside it, and nothing marked verified without a real
click.

    python eval/review_server.py            # the --sample set of 23
    python eval/review_server.py --all      # every drafted question
    python eval/review_server.py --port 8777

**Safety properties, carried over from the CLI:**

  * **Binds to 127.0.0.1 only.** This writes to the eval set; it is not
    something to expose on a network, and `--host` is deliberately absent.
  * **Every mutation is a POST carrying a per-run token.** A GET can never
    change a label, so a crawler, a prefetch, or a stray `curl` cannot
    manufacture verification. This is the browser equivalent of the CLI's
    "refuse to run when stdin is not a terminal" guard, and it exists for the
    same reason: three labels were once marked human-verified by piped input
    that no human ever saw.
  * **Writes on every action**, so closing the tab never loses work.
"""

from __future__ import annotations

import argparse
import html
import secrets
import sys
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval"))

from schema import EvalSet, Question  # noqa: E402

from bilingual_rag.ingest.pipeline import load_chunks  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.yaml"
CHUNKS = ROOT / "data" / "chunks.jsonl"

DOC_TITLES = {
    "saudi_labor_law": "نظام العمل — Labor Law",
    "labor_regulations": "اللائحة التنفيذية — Implementing Regulations",
}

# Per-run token. Regenerated on every start, so a bookmarked POST from a
# previous session cannot fire against this one.
TOKEN = secrets.token_urlsafe(16)

# Fonts that exist on Windows and shape Arabic correctly. No web fonts: this
# runs offline and a missing CDN font would silently fall back to something
# that cannot join letters.
FONT_STACK = (
    '"Segoe UI", "Tahoma", "Arial", "Traditional Arabic", '
    '"Simplified Arabic", "Noto Naskh Arabic", sans-serif'
)

STYLE = f"""
:root {{
  --bg: #12141a; --panel: #1b1f27; --edge: #2c323d;
  --ink: #e8eaed; --dim: #9aa4b2; --accent: #7aa2f7;
  --ok: #4ec9a0; --bad: #f7768e; --warn: #e0af68;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 24px; background: var(--bg); color: var(--ink);
  font-family: {FONT_STACK}; font-size: 17px; line-height: 1.9;
}}
.wrap {{ max-width: 880px; margin: 0 auto; }}
.bar {{
  display: flex; justify-content: space-between; align-items: center;
  gap: 12px; margin-bottom: 18px; color: var(--dim); font-size: 14px;
}}
.progress {{ height: 4px; background: var(--panel); border-radius: 2px; overflow: hidden; }}
.progress > i {{ display: block; height: 100%; background: var(--accent); }}
.card {{
  background: var(--panel); border: 1px solid var(--edge);
  border-radius: 10px; padding: 20px; margin: 16px 0;
}}
.badge {{
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 13px; font-weight: 600; letter-spacing: .02em;
}}
.badge.low {{ background: #3a1f27; color: var(--bad); }}
.badge.medium {{ background: #3a3327; color: var(--warn); }}
.badge.high {{ background: #1f3a33; color: var(--ok); }}
.question {{ font-size: 25px; line-height: 2.1; margin: 10px 0; }}
.meta {{ color: var(--dim); font-size: 14px; }}
.article {{
  background: #151920; border-inline-start: 3px solid var(--accent);
  border-radius: 6px; padding: 14px 18px; margin: 12px 0;
  white-space: pre-wrap; font-size: 18px; line-height: 2.1;
}}
.article h4 {{ margin: 0 0 8px; color: var(--accent); font-size: 14px; }}
.missing {{ border-inline-start-color: var(--bad); color: var(--bad); }}
.actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }}
button, .btn {{
  font-family: inherit; font-size: 15px; padding: 10px 18px;
  border-radius: 8px; border: 1px solid var(--edge); cursor: pointer;
  background: #232833; color: var(--ink); text-decoration: none;
}}
button:hover, .btn:hover {{ border-color: var(--accent); }}
button.accept {{ background: #1f3a33; border-color: var(--ok); color: var(--ok); }}
button.reject {{ background: #3a1f27; border-color: var(--bad); color: var(--bad); }}
input[type=text] {{
  font-family: inherit; font-size: 15px; padding: 9px 12px; border-radius: 8px;
  border: 1px solid var(--edge); background: #12151b; color: var(--ink);
  min-width: 220px;
}}
kbd {{
  background: #2b3240; border: 1px solid var(--edge); border-radius: 4px;
  padding: 1px 6px; font-size: 12px; font-family: monospace;
}}
.reason {{ color: var(--warn); font-size: 15px; margin-top: 6px; }}
.note {{ color: var(--dim); font-size: 15px; }}
a {{ color: var(--accent); }}
.done {{ text-align: center; padding: 60px 0; }}
.done h1 {{ font-size: 28px; }}
hr {{ border: 0; border-top: 1px solid var(--edge); margin: 18px 0; }}
"""

# Keyboard shortcuts, so the browser page keeps the CLI's speed.
SCRIPT = """
document.addEventListener('keydown', function (event) {
  if (event.target.tagName === 'INPUT') return;
  const map = {y: 'accept', n: 'reject', a: 'answerable', u: 'unanswerable', s: 'skip'};
  const action = map[event.key.toLowerCase()];
  if (action) {
    const form = document.getElementById(action);
    if (form) { event.preventDefault(); form.submit(); }
  }
  if (event.key === '/' ) {
    const box = document.getElementById('search-box');
    if (box) { event.preventDefault(); box.focus(); }
  }
});
"""


def esc(text: str) -> str:
    return html.escape(text or "")


def page(body: str, title: str = "Label review") -> str:
    """Wrap body in the RTL shell.

    `dir="rtl"` on the document, with `dir="auto"` on individual text nodes so
    an English question inside an RTL page still reads left-to-right.
    """
    return (
        "<!doctype html><html dir='rtl' lang='ar'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class='wrap'>{body}</div><script>{SCRIPT}</script></body></html>"
    )


class ReviewState:
    """The questions under review and the article text to judge them against."""

    def __init__(self, use_sample: bool, include_all: bool) -> None:
        self.use_sample = use_sample
        self.include_all = include_all
        self.evalset = EvalSet.load(QUESTIONS)
        self.articles = self._load_articles()
        self.order = [q.id for q in self._pool()]
        self.reviewed_this_run = 0

    def _pool(self) -> list[Question]:
        if self.use_sample:
            return self.evalset.review_sample()
        if self.include_all:
            return self.evalset.questions
        return self.evalset.review_order()

    @staticmethod
    def _load_articles() -> dict[str, str]:
        if not CHUNKS.exists():
            return {}
        grouped: dict[str, list[str]] = {}
        for chunk in load_chunks(CHUNKS):
            if chunk.article_no:
                grouped.setdefault(f"{chunk.doc_id}:{chunk.article_no}", []).append(chunk.text)
        return {key: "\n\n".join(parts) for key, parts in grouped.items()}

    def question(self, index: int) -> Question | None:
        if not (0 <= index < len(self.order)):
            return None
        wanted = self.order[index]
        return next((q for q in self.evalset.questions if q.id == wanted), None)

    def save(self) -> None:
        self.evalset.save(QUESTIONS)

    def remaining(self) -> int:
        return sum(
            1
            for qid in self.order
            if any(q.id == qid and q.verified_by == "drafted" for q in self.evalset.questions)
        )


def render_question(state: ReviewState, index: int) -> str:
    question = state.question(index)
    if question is None:
        return render_done(state)

    total = len(state.order)
    done = sum(
        1
        for qid in state.order
        if not any(q.id == qid and q.verified_by == "drafted" for q in state.evalset.questions)
    )
    percent = int(100 * done / max(1, total))

    parts: list[str] = []
    parts.append(
        f"<div class='bar'><span>Question {index + 1} of {total}"
        f" &nbsp;·&nbsp; {done} decided &nbsp;·&nbsp; {esc(question.id)}</span>"
        f"<span><a class='btn' href='/done'>finish</a></span></div>"
        f"<div class='progress'><i style='width:{percent}%'></i></div>"
    )

    parts.append("<div class='card'>")
    parts.append(
        f"<span class='badge {question.confidence}'>{question.confidence.upper()} CONFIDENCE</span>"
        f" <span class='meta' dir='auto'>{esc(question.category)} · {esc(question.difficulty)}"
        f"{' · ' + esc(', '.join(question.tags)) if question.tags else ''}</span>"
    )
    if question.confidence_reason:
        parts.append(f"<div class='reason' dir='auto'>{esc(question.confidence_reason)}</div>")

    if question.question_ar:
        parts.append(f"<div class='question' dir='rtl'>{esc(question.question_ar)}</div>")
    if question.question_en:
        parts.append(f"<div class='question' dir='ltr'>{esc(question.question_en)}</div>")
    parts.append("</div>")

    if question.answerable:
        parts.append(
            f"<div class='meta' dir='auto'>Labelled: "
            f"<b>{esc(', '.join(question.qualified_articles())) or '(none)'}</b> — "
            f"{esc(DOC_TITLES.get(question.doc_id, question.doc_id))}</div>"
        )
        for key in question.qualified_articles():
            text = state.articles.get(key)
            if text is None:
                parts.append(
                    f"<div class='article missing'><h4>{esc(key)}</h4>"
                    "NOT FOUND in the index — this label is almost certainly wrong. "
                    "Use “fix articles” below, or search.</div>"
                )
            else:
                parts.append(
                    f"<div class='article' dir='rtl'><h4 dir='ltr'>{esc(key)}</h4>{esc(text)}</div>"
                )
        parts.append(
            "<div class='note' dir='auto'>Does the text above actually answer the "
            "question as asked?</div>"
        )
    else:
        parts.append(
            "<div class='card'><b dir='auto'>Labelled UNANSWERABLE — the system must refuse.</b>"
            + (
                f"<div class='note' dir='auto'>{esc(question.notes)}</div>"
                if question.notes
                else ""
            )
            + "<div class='note' dir='auto'>Is this genuinely absent from <b>both</b> indexed "
            "documents? The Implementing Regulations were added to the index after some of "
            "these were judged. Search below before trusting the label.</div></div>"
        )

    def form(action: str, label: str, css: str = "", extra: str = "") -> str:
        return (
            f"<form id='{action}' method='post' action='/q/{index}/{action}' "
            f"style='display:inline'>"
            f"<input type='hidden' name='token' value='{TOKEN}'>{extra}"
            f"<button class='{css}' type='submit'>{label}</button></form>"
        )

    article_input = (
        "<input type='text' name='articles' dir='ltr' "
        f"value='{esc(','.join(question.relevant_articles))}' "
        "placeholder='e.g. 75,76'>"
    )

    parts.append("<div class='actions'>")
    parts.append(form("accept", "Accept  <kbd>y</kbd>", "accept"))
    parts.append(form("reject", "Reject  <kbd>n</kbd>", "reject"))
    parts.append(form("skip", "Skip  <kbd>s</kbd>"))
    parts.append("</div>")

    parts.append("<div class='actions'>")
    parts.append(form("articles", "Fix articles", "", article_input))
    if question.answerable:
        parts.append(form("unanswerable", "It is NOT answerable  <kbd>u</kbd>"))
    else:
        parts.append(form("answerable", "It IS answerable  <kbd>a</kbd>", "", article_input))
    parts.append("</div>")

    parts.append("<hr>")
    parts.append(
        f"<form method='get' action='/q/{index}' class='actions'>"
        "<input id='search-box' type='text' name='search' dir='auto' "
        "placeholder='search the corpus…  /'>"
        "<button type='submit'>Search</button></form>"
    )
    return page("".join(parts), f"{question.id} — label review")


def render_search(state: ReviewState, index: int, needle: str) -> str:
    hits = [(key, text) for key, text in state.articles.items() if needle in text]
    parts = [
        f"<div class='bar'><span>Search results for “{esc(needle)}” — {len(hits)} article(s)"
        f"</span><span><a class='btn' href='/q/{index}'>back to question</a></span></div>"
    ]
    if not hits:
        parts.append("<div class='card' dir='auto'>No article contains that text.</div>")
    for key, text in hits[:20]:
        position = text.find(needle)
        snippet = text[max(0, position - 200) : position + 400]
        parts.append(
            f"<div class='article' dir='rtl'><h4 dir='ltr'>{esc(key)}</h4>…{esc(snippet)}…</div>"
        )
    if len(hits) > 20:
        parts.append(f"<div class='note'>… and {len(hits) - 20} more</div>")
    return page("".join(parts), "search")


def render_done(state: ReviewState) -> str:
    verified = len(state.evalset.verified())
    total = len(state.evalset.questions)
    return page(
        "<div class='done'>"
        "<h1 dir='auto'>Review session finished</h1>"
        f"<p dir='auto'>Decided this run: <b>{state.reviewed_this_run}</b></p>"
        f"<p dir='auto'>Human-verified overall: <b>{verified} / {total}</b></p>"
        "<p class='note' dir='auto'>Saved to eval/questions.yaml. Re-measure with:<br>"
        "<code>python eval/ablate.py --repeats 3 --verified-only</code></p>"
        "<p><a class='btn' href='/q/0'>back to the start</a></p>"
        "</div>",
        "done",
    )


def build_app(state: ReviewState):  # noqa: ANN201 - FastAPI app
    """Assemble the routes.

    FastAPI's imports are at module level, not in here. Under
    `from __future__ import annotations` every annotation is a *string* that
    FastAPI resolves against the module globals at request time; a `Request`
    imported into this function's local scope is invisible there, so FastAPI
    treats `request: Request` as an unknown query parameter and every GET
    returns 422. Same failure mode as function-local types in agent-core's
    tool registry, for the same reason.
    """
    app = FastAPI(title="eval label review", docs_url=None, redoc_url=None)

    def check(token: str) -> None:
        """Every mutation carries the per-run token.

        Without this a GET-like request could flip a label, which is exactly
        the failure the CLI's terminal guard existed to prevent: verification
        recorded when no human decided anything.
        """
        if not secrets.compare_digest(token, TOKEN):
            raise HTTPException(status_code=403, detail="stale or missing token; reload the page")

    def advance(index: int) -> RedirectResponse:
        return RedirectResponse(url=f"/q/{index + 1}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/q/0", status_code=303)

    @app.get("/q/{index}", response_class=HTMLResponse)
    def show(index: int, request: Request) -> HTMLResponse:
        needle = request.query_params.get("search", "").strip()
        if needle:
            return HTMLResponse(render_search(state, index, needle))
        return HTMLResponse(render_question(state, index))

    @app.get("/done", response_class=HTMLResponse)
    def done() -> HTMLResponse:
        return HTMLResponse(render_done(state))

    @app.post("/q/{index}/accept")
    def accept(index: int, token: str = Form(...)) -> RedirectResponse:
        check(token)
        question = state.question(index)
        if question:
            question.verified_by = "human"
            state.reviewed_this_run += 1
            state.save()
        return advance(index)

    @app.post("/q/{index}/reject")
    def reject(index: int, token: str = Form(...)) -> RedirectResponse:
        check(token)
        question = state.question(index)
        if question:
            # Removed rather than left as drafted: a question nobody believes
            # should not sit in the file pretending to be data.
            state.evalset.questions = [q for q in state.evalset.questions if q.id != question.id]
            state.reviewed_this_run += 1
            state.save()
        return advance(index)

    @app.post("/q/{index}/skip")
    def skip(index: int, token: str = Form(...)) -> RedirectResponse:
        check(token)
        return advance(index)

    @app.post("/q/{index}/articles")
    def set_articles(
        index: int, token: str = Form(...), articles: str = Form("")
    ) -> RedirectResponse:
        check(token)
        question = state.question(index)
        wanted = [a.strip() for a in articles.split(",") if a.strip()]
        if question and wanted:
            question.relevant_articles = wanted
            question.answerable = True
            question.verified_by = "human"
            state.reviewed_this_run += 1
            state.save()
        return advance(index)

    @app.post("/q/{index}/answerable")
    def mark_answerable(
        index: int, token: str = Form(...), articles: str = Form("")
    ) -> RedirectResponse:
        check(token)
        question = state.question(index)
        wanted = [a.strip() for a in articles.split(",") if a.strip()]
        if question and wanted:
            question.answerable = True
            question.relevant_articles = wanted
            question.verified_by = "human"
            state.reviewed_this_run += 1
            state.save()
        return advance(index)

    @app.post("/q/{index}/unanswerable")
    def mark_unanswerable(index: int, token: str = Form(...)) -> RedirectResponse:
        check(token)
        question = state.question(index)
        if question:
            question.answerable = False
            question.relevant_articles = []
            question.relevant_chunks = []
            question.verified_by = "human"
            state.reviewed_this_run += 1
            state.save()
        return advance(index)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--all", action="store_true", help="every drafted question, not the sample")
    parser.add_argument(
        "--full-set",
        action="store_true",
        help="include already-verified questions too",
    )
    args = parser.parse_args()

    state = ReviewState(use_sample=not (args.all or args.full_set), include_all=args.full_set)

    if not state.order:
        print("nothing to review")
        return 0
    if not state.articles:
        print(f"warning: {CHUNKS} not found — article text will be missing.")
        print("Run `python scripts/ingest.py` first.\n")

    import uvicorn

    low = sum(1 for q in state._pool() if q.confidence == "low")
    print()
    print(f"  Reviewing {len(state.order)} question(s); {low} flagged LOW CONFIDENCE.")
    print(f"  Open:  http://127.0.0.1:{args.port}/")
    print()
    print("  Keys: y accept · n reject · s skip · a answerable · u unanswerable · / search")
    print("  Nothing is marked verified without a real button click.")
    print()

    # 127.0.0.1 only, and no --host flag: this writes to the eval set and has
    # no authentication, so it must not be reachable from the network.
    uvicorn.run(build_app(state), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
