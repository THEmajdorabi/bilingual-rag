"""The evaluation set's schema and loader.

`eval/questions.yaml` is the most valuable file in this repository. Every
number the README reports is measured against it, so its provenance matters as
much as its content.

**Provenance is a required field, not a nicety.** Each question records
`verified_by`, which is one of:

  * `human`   -- a person read the cited article and confirmed the question is
                 answerable from it, and that the labelled chunks are the right
                 ones. Only these questions produce a number that means
                 anything about system quality.
  * `drafted` -- generated from real corpus text by a model, awaiting review.
                 Useful for exercising the pipeline; **not** evidence.

The distinction exists because an eval set written by the model under test
flatters itself. `run_eval.py` reports the two populations separately and
refuses to headline a figure computed over unverified questions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Difficulty = Literal["easy", "medium", "hard", "multi_hop"]
VerifiedBy = Literal["human", "drafted"]


class Question(BaseModel):
    """One evaluation question."""

    id: str
    question_ar: str | None = None
    question_en: str | None = None

    # Chunk ids that contain the answer. Empty for unanswerable questions.
    relevant_chunks: list[str] = Field(default_factory=list)
    # Article numbers, which survive re-chunking when chunk ids do not.
    relevant_articles: list[str] = Field(default_factory=list)
    # Which document those article numbers belong to. Required now that the
    # index holds both the Labor Law and its Implementing Regulations: both
    # number an "article 15", so a bare number is ambiguous.
    doc_id: str = "saudi_labor_law"

    answerable: bool = True
    category: str = "general"
    difficulty: Difficulty = "medium"
    tags: list[str] = Field(default_factory=list)

    verified_by: VerifiedBy = "drafted"
    notes: str | None = None

    @model_validator(mode="after")
    def check_shape(self) -> Question:
        if not self.question_ar and not self.question_en:
            raise ValueError(f"{self.id}: needs a question in at least one language")
        if self.answerable and not (self.relevant_chunks or self.relevant_articles):
            raise ValueError(
                f"{self.id}: answerable questions must label the relevant article or chunk"
            )
        if not self.answerable and (self.relevant_chunks or self.relevant_articles):
            raise ValueError(f"{self.id}: unanswerable questions must not label relevant passages")
        return self

    @property
    def is_cross_lingual(self) -> bool:
        """An English question against the Arabic corpus."""
        return bool(self.question_en) and not self.question_ar

    def qualified_articles(self) -> list[str]:
        """Labels as `doc_id:article`, which is what metrics compare.

        A bare article number stopped being unique when the Implementing
        Regulations joined the index.
        """
        return [f"{self.doc_id}:{a}" for a in self.relevant_articles]

    @property
    def query(self) -> str:
        """The text actually sent to the retriever."""
        return self.question_ar or self.question_en or ""

    @property
    def is_multi_hop(self) -> bool:
        """Multi-hop is a tag, not a bucket: it overlaps the other categories."""
        return self.difficulty == "multi_hop" or len(self.relevant_articles) > 1


class EvalSet(BaseModel):
    questions: list[Question]

    @classmethod
    def load(cls, path: str | Path) -> EvalSet:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        items = raw.get("questions", raw) if isinstance(raw, dict) else raw
        return cls(questions=[Question.model_validate(q) for q in items])

    def save(self, path: str | Path) -> None:
        payload = {"questions": [q.model_dump(exclude_none=True) for q in self.questions]}
        Path(path).write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )

    # ---- populations ----------------------------------------------------

    def verified(self) -> list[Question]:
        return [q for q in self.questions if q.verified_by == "human"]

    def drafted(self) -> list[Question]:
        return [q for q in self.questions if q.verified_by == "drafted"]

    def answerable(self) -> list[Question]:
        return [q for q in self.questions if q.answerable]

    def unanswerable(self) -> list[Question]:
        return [q for q in self.questions if not q.answerable]

    def cross_lingual(self) -> list[Question]:
        """Answerable English questions against the Arabic corpus.

        Answerable only. An unanswerable English question measures refusal,
        not cross-lingual retrieval, and counting it here would inflate the
        cross-lingual bucket against the spec's target of 20.
        """
        return [q for q in self.questions if q.is_cross_lingual and q.answerable]

    def multi_hop(self) -> list[Question]:
        return [q for q in self.questions if q.is_multi_hop and q.answerable]

    def composition(self) -> dict[str, int]:
        """The counts the spec asks for, so drift from the target is visible."""
        return {
            "total": len(self.questions),
            "human_verified": len(self.verified()),
            "drafted_unverified": len(self.drafted()),
            "answerable_arabic": len([q for q in self.answerable() if q.question_ar]),
            "answerable_cross_lingual": len(self.cross_lingual()),
            "unanswerable": len(self.unanswerable()),
            "multi_hop": len(self.multi_hop()),
        }
