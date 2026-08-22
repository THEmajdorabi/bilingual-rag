"""Request and response shapes for the API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from bilingual_rag.qa.answer import Citation


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    lang: str | None = None
    top_k: int = Field(default=10, ge=1, le=20)
    # The agent decides how to retrieve; off by default because it costs more
    # and takes longer, and most questions do not need it.
    use_agent: bool = False
    check_faithfulness: bool = True


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation] = Field(default_factory=list)
    refused: bool
    refusal_reason: str | None = None
    retrieved_count: int
    latency_ms: int
    cost_usd: float
    faithfulness: float | None = None
    claims_checked: int = 0
    unsupported_claims: list[str] = Field(default_factory=list)
    hallucinated_citations: list[str] = Field(default_factory=list)
    lang: str
    agent_steps: int | None = None
    agent_searches: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    """Retrieval only, no model call and no cost."""

    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=10, ge=1, le=50)


class PassageOut(BaseModel):
    chunk_id: str
    doc_id: str
    text: str
    heading_path: list[str]
    article_no: str | None
    page: int
    lang: str
    score: float
    rank: int


class SearchResponse(BaseModel):
    passages: list[PassageOut]
    latency_ms: int
    stage_latencies: dict[str, float] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    profile: str
    embedding_model: str
    reranker_model: str
    chunks_indexed: int
    collection: str
    documents: list[str]
    # Stated in every health check, because it qualifies every number the
    # service reports about itself.
    eval_set_verified: bool
    eval_set_note: str
    answer_model: str = ""
