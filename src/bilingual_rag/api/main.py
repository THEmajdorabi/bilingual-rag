"""The FastAPI service.

    POST /ask       question -> grounded answer with citations, or a refusal
    POST /ask/stream  the same, streamed as server-sent events
    POST /search    retrieval only: no model call, no cost
    GET  /articles/{article_no}
    GET  /healthz
    GET  /metrics   Prometheus
    GET  /          the RTL UI

Models load once at startup rather than per request. On the 8 GB profile the
embedder and the cross-encoder do not both fit in VRAM, so the retriever loads
them in sequence; a per-request load would make every query pay tens of
seconds.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from bilingual_rag.api.schemas import (
    AskRequest,
    AskResponse,
    HealthResponse,
    PassageOut,
    SearchRequest,
    SearchResponse,
)
from bilingual_rag.config import QdrantSettings, load_settings
from bilingual_rag.index.retriever import Retriever
from bilingual_rag.qa.answer import Answerer

log = structlog.get_logger(__name__)

UI_DIR = Path(__file__).resolve().parents[3] / "src" / "bilingual_rag" / "ui"

# The disclaimer the UI must always show, kept here so the API and the page
# cannot drift apart.
DISCLAIMER_AR = "عرض توضيحي للبحث في وثائق عامة. ليس استشارة قانونية."
DISCLAIMER_EN = "Retrieval demo over public documents. Not legal advice."

EVAL_NOTE = (
    "Retrieval quality figures for this service are measured against a "
    "model-drafted, UNVERIFIED evaluation set. Treat every reported number as "
    "provisional."
)


class Service:
    """Holds the loaded models and indexes for the process lifetime."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.retriever: Retriever | None = None
        self.answerer: Answerer | None = None
        self.started = time.time()
        self.requests = 0
        self.refusals = 0

    def load(self) -> None:
        log.info("service.loading")
        self.retriever = Retriever(
            qdrant_settings=QdrantSettings(**self.settings.qdrant.model_dump())
        )
        self.retriever.load_bm25()
        # The model set comes from BILINGUAL_RAG_MODEL_SET; see Answerer._key.
        self.answerer = Answerer(settings=self.settings)
        # Warm the models so the first real request is not the one that pays
        # for loading them.
        self.retriever.retrieve("مدة الإشعار")
        log.info("service.ready", chunks=self.retriever.store.count())


service = Service()


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    service.load()
    yield


app = FastAPI(
    title="bilingual-rag",
    description="Ask in Arabic or English. Answers grounded in Saudi labour law, with citations.",
    version="0.1.0",
    lifespan=lifespan,
)


def get_retriever() -> Retriever:
    if service.retriever is None:
        raise HTTPException(status_code=503, detail="service still loading")
    return service.retriever


def get_answerer() -> Answerer:
    if service.answerer is None:
        raise HTTPException(status_code=503, detail="service still loading")
    return service.answerer


# ---- rate limiting -------------------------------------------------------

_HITS: dict[str, list[float]] = {}
RATE_LIMIT = 30  # requests
RATE_WINDOW = 60.0  # seconds


def rate_limit(request: Request) -> None:
    """A small fixed-window limiter, per client IP.

    Enough to stop one tab hammering a GPU-backed endpoint. Not a substitute
    for a real gateway, and it is per-process, so it resets on restart.
    """
    client = request.client.host if request.client else "unknown"
    now = time.time()
    hits = [t for t in _HITS.get(client, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"rate limit: {RATE_LIMIT} requests per {RATE_WINDOW:.0f}s",
        )
    hits.append(now)
    _HITS[client] = hits


# ---- endpoints -----------------------------------------------------------


@app.post("/ask", response_model=AskResponse)
def ask(
    body: AskRequest,
    _: None = Depends(rate_limit),
    retriever: Retriever = Depends(get_retriever),
    answerer: Answerer = Depends(get_answerer),
) -> AskResponse:
    """Answer a question from the corpus, or refuse."""
    service.requests += 1
    agent_steps = None
    searches: list[str] = []

    if body.use_agent:
        from bilingual_rag.qa.agent import ResearchAgent

        passages, info = ResearchAgent(retriever, service.settings).gather(body.question)
        agent_steps = info["steps"]
        searches = info["searches"]
    else:
        passages = retriever.retrieve(body.question, top_k=body.top_k).passages

    answer = answerer.answer(body.question, passages, check_faithfulness=body.check_faithfulness)
    service.refusals += int(answer.refused)

    return AskResponse(
        answer=answer.text,
        citations=answer.citations,
        refused=answer.refused,
        refusal_reason=answer.refusal_reason,
        retrieved_count=answer.retrieved_count,
        latency_ms=answer.latency_ms,
        cost_usd=round(answer.cost_usd, 6),
        faithfulness=answer.faithfulness,
        claims_checked=answer.claims_checked,
        unsupported_claims=answer.unsupported_claims,
        hallucinated_citations=answer.hallucinated_citations,
        lang=answer.lang,
        agent_steps=agent_steps,
        agent_searches=searches,
    )


@app.post("/ask/stream")
def ask_stream(
    body: AskRequest,
    _: None = Depends(rate_limit),
    retriever: Retriever = Depends(get_retriever),
    answerer: Answerer = Depends(get_answerer),
) -> StreamingResponse:
    """Server-sent events, so the UI can show progress rather than a spinner.

    Retrieval and answering are not themselves streamed token-by-token; what
    streams is the *stage* an eight-second request is currently in, which is
    the part a waiting user actually wants.
    """

    def events():  # noqa: ANN202
        def send(event: str, payload: dict[str, Any]) -> str:
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        yield send("stage", {"stage": "retrieving"})
        passages = retriever.retrieve(body.question, top_k=body.top_k).passages

        yield send(
            "passages",
            {
                "count": len(passages),
                "articles": [p.article_no for p in passages if p.article_no],
            },
        )
        yield send("stage", {"stage": "answering"})

        answer = answerer.answer(
            body.question, passages, check_faithfulness=body.check_faithfulness
        )
        service.requests += 1
        service.refusals += int(answer.refused)

        yield send(
            "answer",
            {
                "answer": answer.text,
                "refused": answer.refused,
                "citations": [c.model_dump() for c in answer.citations],
                "faithfulness": answer.faithfulness,
                "claims_checked": answer.claims_checked,
                "unsupported_claims": answer.unsupported_claims,
                "hallucinated_citations": answer.hallucinated_citations,
                "latency_ms": answer.latency_ms,
                "cost_usd": round(answer.cost_usd, 6),
                "lang": answer.lang,
            },
        )
        yield send("done", {})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/search", response_model=SearchResponse)
def search(
    body: SearchRequest,
    _: None = Depends(rate_limit),
    retriever: Retriever = Depends(get_retriever),
) -> SearchResponse:
    """Retrieval only. No model call, so no cost and no possibility of a
    hallucinated answer -- useful for inspecting what the retriever actually
    returns."""
    result = retriever.retrieve(body.query, top_k=body.top_k)
    return SearchResponse(
        passages=[PassageOut(**p.to_dict(), doc_id=p.doc_id) for p in result.passages],
        latency_ms=int(result.latency_ms),
        stage_latencies={k: round(v, 1) for k, v in result.stage_latencies.items()},
    )


@app.get("/articles/{article_no}")
def get_article(article_no: str, retriever: Retriever = Depends(get_retriever)) -> list[PassageOut]:
    passages = retriever.get_article(article_no)
    if not passages:
        raise HTTPException(status_code=404, detail=f"no article {article_no!r} in the corpus")
    return [PassageOut(**p.to_dict(), doc_id=p.doc_id) for p in passages]


@app.get("/healthz", response_model=HealthResponse)
def healthz(retriever: Retriever = Depends(get_retriever)) -> HealthResponse:
    documents = sorted({p.get("doc_id", "") for p in retriever.store.all_payloads()} - {""})
    return HealthResponse(
        status="ok",
        profile=retriever.profile.name,
        embedding_model=retriever.profile.embedding_model,
        reranker_model=retriever.profile.reranker_model,
        chunks_indexed=retriever.store.count(),
        collection=retriever.profile.collection_name,
        documents=documents,
        eval_set_verified=False,
        eval_set_note=EVAL_NOTE,
        answer_model=(
            service.answerer.settings.models[service.answerer._key("answer")].id
            if service.answerer
            else "unloaded"
        ),
    )


@app.get("/metrics")
def metrics() -> StreamingResponse:
    """Prometheus text format, hand-rolled to avoid another dependency."""
    answerer = service.answerer
    stats = answerer.summary() if answerer else {}
    lines = [
        "# HELP bilingual_rag_requests_total Questions answered.",
        "# TYPE bilingual_rag_requests_total counter",
        f"bilingual_rag_requests_total {service.requests}",
        "# HELP bilingual_rag_refusals_total Questions the system declined to answer.",
        "# TYPE bilingual_rag_refusals_total counter",
        f"bilingual_rag_refusals_total {service.refusals}",
        "# HELP bilingual_rag_hallucinated_citations_total Citations naming an article never retrieved.",
        "# TYPE bilingual_rag_hallucinated_citations_total counter",
        f"bilingual_rag_hallucinated_citations_total {stats.get('hallucinated_citations', 0)}",
        "# HELP bilingual_rag_cost_usd_total Model spend since start.",
        "# TYPE bilingual_rag_cost_usd_total counter",
        f"bilingual_rag_cost_usd_total {stats.get('total_cost_usd', 0.0)}",
        "# HELP bilingual_rag_uptime_seconds Seconds since start.",
        "# TYPE bilingual_rag_uptime_seconds gauge",
        f"bilingual_rag_uptime_seconds {time.time() - service.started:.0f}",
    ]
    return StreamingResponse(iter(["\n".join(lines) + "\n"]), media_type="text/plain")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(UI_DIR / "index.html")


if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=UI_DIR), name="static")
