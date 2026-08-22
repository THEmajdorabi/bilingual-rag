"""Grounded answering: citations, refusal, and a faithfulness check.

Three things happen here that a plain "stuff the passages in a prompt" RAG
does not do:

  * **Citations are parsed back out and validated.** A citation naming an
    article that was never retrieved is dropped and counted, because a model
    that invents a citation is doing the one thing this system exists to
    prevent.
  * **Refusal is detected and reported**, not inferred from vibes. It is a
    measured metric.
  * **Every factual sentence is checked against the passage it cites**, by a
    cheap second model call. That yields the faithfulness number and flags
    unsupported claims to the user.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from pydantic import BaseModel, Field

from bilingual_rag.config import Settings, load_settings
from bilingual_rag.index.retriever import Passage
from bilingual_rag.qa.prompts import (
    ANSWER_SYSTEM,
    FAITHFULNESS_SYSTEM,
    build_answer_prompt,
    build_faithfulness_prompt,
)

log = structlog.get_logger(__name__)

# `[المادة 75]`, `[article 75]`, `[75]`, `[المادة 15 مكرر]`.
CITATION = re.compile(
    r"\[\s*(?:المادة|article|art\.?)?\s*([0-9]{1,3}(?:_bis)?|[0-9]{1,3}\s*مكرر)\s*\]",
    re.IGNORECASE,
)

# Sentence boundaries for Arabic and English.
SENTENCE = re.compile(r"(?<=[.!?؟])\s+")

# The model is instructed to open a refusal with this exact token. Detecting
# refusal by phrase matching was tried first and was wrong in both directions:
# "does not contain" appears inside good answers ("the contract does not
# contain a fixed term"), and a model can refuse while citing related
# passages, which a naive "refused iff no citations" rule scores as an answer.
REFUSAL_TOKEN = "NO_ANSWER"


class Citation(BaseModel):
    passage_id: str
    doc_id: str
    article_no: str | None
    page: int
    quoted: str
    heading_path: list[str] = Field(default_factory=list)
    # Set by the faithfulness check.
    supported: bool | None = None


class Answer(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    retrieved_count: int = 0
    latency_ms: int = 0
    # Total spend, and the split. They are recorded separately because the two
    # calls scale differently: answer generation is one call per question, the
    # faithfulness check is one call per CLAIM. Reporting only the total hides
    # which half a cost projection is actually sensitive to.
    cost_usd: float = 0.0
    answer_cost_usd: float = 0.0
    faithfulness_cost_usd: float = 0.0
    answer_input_tokens: int = 0
    answer_output_tokens: int = 0
    faithfulness_input_tokens: int = 0
    faithfulness_output_tokens: int = 0
    # Wall time of retrieval alone. No API cost -- the embedder and the
    # cross-encoder run locally -- but it is the machine-time half of the bill.
    retrieval_ms: int = 0
    # Faithfulness, measured per claim.
    claims_checked: int = 0
    claims_supported: int = 0
    unsupported_claims: list[str] = Field(default_factory=list)
    # Citations the model produced that name an article it was never given.
    hallucinated_citations: list[str] = Field(default_factory=list)
    lang: str = "ar"

    @property
    def faithfulness(self) -> float | None:
        """Fraction of checked claims supported by their cited passage.

        None rather than 1.0 when nothing was checked: a refusal has no claims,
        and reporting perfect faithfulness for saying nothing would be a lie of
        exactly the kind this project is built to avoid.
        """
        if not self.claims_checked:
            return None
        return self.claims_supported / self.claims_checked


@dataclass
class AnswerStats:
    """Running totals across a session, for `/metrics` and the eval."""

    answers: int = 0
    refusals: int = 0
    hallucinated_citations: int = 0
    cost_usd: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)


def detect_language(text: str) -> str:
    arabic = sum(1 for c in text if "؀" <= c <= "ۿ")
    return "ar" if arabic > len(text) * 0.3 else "en"


def parse_refusal(text: str) -> tuple[bool, str]:
    """Detect the refusal sentinel and strip it from the displayed text.

    Returns (refused, cleaned_text). The token is removed so the user never
    sees the machinery.
    """
    stripped = text.lstrip()
    if stripped.upper().startswith(REFUSAL_TOKEN):
        remainder = stripped[len(REFUSAL_TOKEN) :]
        return True, remainder.lstrip(" :-\n\r\t")
    return False, text


def split_claims(text: str) -> list[str]:
    """Factual sentences: those carrying at least one citation.

    A sentence with no citation is not checked, because there is nothing to
    check it against. Those sentences are the ones the prompt tells the model
    not to write, and `hallucinated_citations` plus the citation count are what
    surface when it does anyway.
    """
    return [s.strip() for s in SENTENCE.split(text) if s.strip() and CITATION.search(s)]


def extract_citations(text: str, passages: list[Passage]) -> tuple[list[Citation], list[str]]:
    """Parse citation markers and match them to passages actually retrieved.

    Returns (valid citations, article numbers the model cited but never saw).
    The second list is the important one: it is the model inventing a source.
    """
    by_article: dict[str, Passage] = {}
    for passage in passages:
        if passage.article_no:
            by_article.setdefault(passage.article_no, passage)

    seen: list[Citation] = []
    invented: list[str] = []

    for match in CITATION.finditer(text):
        raw = match.group(1).strip().replace(" ", "").replace("مكرر", "_bis")
        passage = by_article.get(raw)
        if passage is None:
            if raw not in invented:
                invented.append(raw)
            continue
        if any(c.passage_id == passage.chunk_id for c in seen):
            continue
        seen.append(
            Citation(
                passage_id=passage.chunk_id,
                doc_id=passage.doc_id,
                article_no=passage.article_no,
                page=passage.page,
                quoted=passage.text[:400],
                heading_path=passage.heading_path,
            )
        )

    return seen, invented


class Answerer:
    """Turns retrieved passages into a grounded, cited answer or a refusal."""

    def __init__(self, settings: Settings | None = None, model_set: str = "") -> None:
        self.settings = settings or load_settings()
        self._answer_provider = None
        self._check_provider = None
        self.stats = AnswerStats()
        # A model set selects a whole row of config.yaml at once: "" uses
        # `answer` and `faithfulness`, "openai" uses `answer_openai` and
        # `faithfulness_openai`. Swapping providers is then a config lookup,
        # not an edit, which is what makes a cross-provider comparison
        # reproducible rather than a thing someone did once by hand.
        self.model_set = model_set or os.environ.get("BILINGUAL_RAG_MODEL_SET", "")

    def _key(self, role: str) -> str:
        key = f"{role}_{self.model_set}" if self.model_set else role
        if key not in self.settings.models:
            known = ", ".join(sorted(self.settings.models))
            raise KeyError(
                f"no model {key!r} in config.yaml (model set {self.model_set!r}). Known: {known}"
            )
        return key

    # ---- providers, built lazily so importing this module needs no key ----

    def _provider(self, key: str):  # noqa: ANN202
        from bilingual_rag.qa.provider import build_provider

        spec = self.settings.models[key]
        return build_provider(spec.provider, spec.id)

    @property
    def answer_provider(self):  # noqa: ANN201
        if self._answer_provider is None:
            self._answer_provider = self._provider(self._key("answer"))
        return self._answer_provider

    @property
    def check_provider(self):  # noqa: ANN201
        if self._check_provider is None:
            self._check_provider = self._provider(self._key("faithfulness"))
        return self._check_provider

    def _cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        price = self.settings.pricing.get(model_id, {})
        return (
            input_tokens * price.get("input", 0.0) + output_tokens * price.get("output", 0.0)
        ) / 1_000_000

    # ---- answering -------------------------------------------------------

    def answer(
        self,
        question: str,
        passages: list[Passage],
        check_faithfulness: bool = True,
    ) -> Answer:
        started = time.perf_counter()
        language = detect_language(question)

        if not passages:
            # Nothing retrieved is an unambiguous refusal; do not spend a model
            # call to discover that.
            return Answer(
                text=(
                    "لم أجد في النصوص المتاحة ما يجيب عن هذا السؤال."
                    if language == "ar"
                    else "I could not find anything in the available passages that answers this."
                ),
                refused=True,
                refusal_reason="no passages retrieved",
                retrieved_count=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                lang=language,
            )

        spec = self.settings.models[self._key("answer")]
        completion = self.answer_provider.complete(
            prompt=build_answer_prompt(
                question, [p.to_dict() | {"doc_id": p.doc_id} for p in passages]
            ),
            system=ANSWER_SYSTEM,
            max_tokens=spec.max_tokens,
        )
        cost = self._cost(spec.id, completion.input_tokens, completion.output_tokens)

        refused, cleaned = parse_refusal(completion.text)
        citations, invented = extract_citations(cleaned, passages)

        answer = Answer(
            text=cleaned,
            citations=citations,
            refused=refused,
            refusal_reason="model found no supporting passage" if refused else None,
            retrieved_count=len(passages),
            cost_usd=cost,
            answer_cost_usd=cost,
            answer_input_tokens=completion.input_tokens,
            answer_output_tokens=completion.output_tokens,
            hallucinated_citations=invented,
            lang=language,
        )

        if invented:
            # Loud, because this is the failure the whole design targets.
            log.warning("answer.invented_citation", articles=invented, question=question[:80])

        if check_faithfulness and not refused:
            self._check_claims(answer, passages)

        answer.latency_ms = int((time.perf_counter() - started) * 1000)

        self.stats.answers += 1
        self.stats.refusals += int(answer.refused)
        self.stats.hallucinated_citations += len(invented)
        self.stats.cost_usd += answer.cost_usd
        self.stats.latencies_ms.append(answer.latency_ms)
        return answer

    def _check_claims(self, answer: Answer, passages: list[Passage]) -> None:
        """Verify each cited sentence against the passage it cites.

        One cheap call per claim. This is the uncommon part of the pipeline and
        the source of the faithfulness number.
        """
        by_article = {p.article_no: p for p in passages if p.article_no}
        spec = self.settings.models[self._key("faithfulness")]

        for claim in split_claims(answer.text):
            match = CITATION.search(claim)
            if not match:
                continue
            article = match.group(1).strip().replace(" ", "").replace("مكرر", "_bis")
            passage = by_article.get(article)
            if passage is None:
                continue

            try:
                verdict = self.check_provider.complete(
                    prompt=build_faithfulness_prompt(claim, passage.text),
                    system=FAITHFULNESS_SYSTEM,
                    max_tokens=spec.max_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                # A failed check must not fail the answer. It is recorded as
                # unchecked rather than counted as supported, so the metric
                # never flatters itself on an outage.
                log.warning("faithfulness.check_failed", error=str(exc))
                continue

            claim_cost = self._cost(spec.id, verdict.input_tokens, verdict.output_tokens)
            answer.cost_usd += claim_cost
            answer.faithfulness_cost_usd += claim_cost
            answer.faithfulness_input_tokens += verdict.input_tokens
            answer.faithfulness_output_tokens += verdict.output_tokens
            answer.claims_checked += 1

            if "SUPPORTED" in verdict.text.upper() and "UNSUPPORTED" not in verdict.text.upper():
                answer.claims_supported += 1
                for citation in answer.citations:
                    if citation.article_no == article:
                        citation.supported = True
            else:
                answer.unsupported_claims.append(claim)
                for citation in answer.citations:
                    if citation.article_no == article:
                        citation.supported = False

    def summary(self) -> dict[str, Any]:
        latencies = sorted(self.stats.latencies_ms)
        p95 = latencies[max(0, int(0.95 * len(latencies)) - 1)] if latencies else 0
        return {
            "answers": self.stats.answers,
            "refusals": self.stats.refusals,
            "hallucinated_citations": self.stats.hallucinated_citations,
            "total_cost_usd": round(self.stats.cost_usd, 4),
            "p95_latency_ms": p95,
        }
