"""The decision layer: where `agent-core` earns its place.

A search box retrieves once and stops. This registers three tools on an
agent-core agent and lets the model decide for itself:

    search_corpus(query, top_k, lang), retrieve; may be called repeatedly
                                          with different sub-queries
    get_article(article_no), look up a specific article by number
    list_chapters(): see the corpus structure before
                                          deciding what to search for

That decision, search once, decompose into sub-queries and search several
times, look up an article the user named, or declare the question out of scope
-- is the thing that separates this from a search box, and the reason repo 01
was built first.

Guardrails are deliberately tight: six steps, a cost ceiling per question, and
a tool allowlist covering exactly those three tools. A question is not worth an
unbounded number of retrievals.
"""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from agent_core.agent import Agent
from agent_core.guardrails import Guardrails
from agent_core.providers import build_provider
from agent_core.registry import ToolRegistry

from bilingual_rag.config import Settings, load_settings
from bilingual_rag.index.retriever import Passage, Retriever

log = structlog.get_logger(__name__)

AGENT_SYSTEM = """\
You research questions about Saudi labour law over an indexed corpus of the
Labor Law and its Implementing Regulations.

You have three tools. Use them to GATHER passages; you are not writing the
final answer here.

Decide, do not guess:
  * A single clear question -> search_corpus once.
  * A question with two parts ("how much notice, and what if they skip it")
    -> search_corpus separately for each part. One retrieval will find one
    half and miss the other.
  * The user names an article number -> get_article.
  * You are unsure what the corpus covers -> list_chapters first.
  * Nothing relevant comes back after two honest attempts with different
    wording -> stop and say the corpus does not cover it. Do not keep
    searching.

Users write in Gulf colloquial with misspellings and describe situations
rather than legal concepts. Translate the situation into the legal terms the
corpus would use before searching: "my boss told me to leave tomorrow" is
about notice periods and contract termination.

When you have enough passages, stop and summarise which articles are relevant
and why. Do not write the final user-facing answer; another step does that.
"""


class CorpusTools:
    """The three tools, bound to a retriever.

    A class rather than closures so the collected passages can be read back
    after the run: the answer layer needs the actual passage objects, not the
    strings the model saw.
    """

    def __init__(self, retriever: Retriever, top_k: int = 10) -> None:
        self.retriever = retriever
        self.top_k = top_k
        self.collected: dict[str, Passage] = {}
        self.searches: list[str] = []

    def _record(self, passages: list[Passage]) -> None:
        for passage in passages:
            self.collected.setdefault(passage.chunk_id, passage)

    def register(self, registry: ToolRegistry) -> None:
        tools = self

        @registry.register(timeout_s=60, idempotent=True)
        def search_corpus(
            query: Annotated[str, "What to search for, in Arabic or English."],
            top_k: Annotated[int, "How many passages to return. Default 10."] = 10,
        ) -> str:
            """Search the Saudi labour law corpus and return matching passages with their article numbers."""
            tools.searches.append(query)
            result = tools.retriever.retrieve(query, top_k=min(top_k, tools.top_k))
            tools._record(result.passages)
            if not result.passages:
                return "No passages matched that query."
            return "\n\n".join(
                f"[article {p.article_no} · {p.doc_id}] {p.text[:600]}" for p in result.passages
            )

        @registry.register(timeout_s=30, idempotent=True)
        def get_article(
            article_no: Annotated[str, "The article number, e.g. '75' or '15_bis'."],
        ) -> str:
            """Fetch one article by its number, when the user names a specific article."""
            passages = tools.retriever.get_article(article_no)
            tools._record(passages)
            if not passages:
                return f"No article numbered {article_no} is in the corpus."
            return "\n\n".join(f"[article {p.article_no}] {p.text}" for p in passages)

        @registry.register(timeout_s=30, idempotent=True)
        def list_chapters() -> str:
            """List the chapters and sections of the corpus, to see what it covers."""
            seen: list[str] = []
            for payload in tools.retriever.store.all_payloads():
                path = payload.get("heading_path") or []
                if len(path) >= 2:
                    label = " > ".join(path[:2])
                    if label not in seen:
                        seen.append(label)
            return "\n".join(sorted(seen)[:60]) or "(no structure recorded)"


class ResearchAgent:
    """Runs the decision loop and hands back the passages it gathered."""

    def __init__(
        self,
        retriever: Retriever,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.retriever = retriever

    def gather(self, question: str) -> tuple[list[Passage], dict[str, Any]]:
        """Let the agent decide how to retrieve. Returns (passages, trace info)."""
        registry = ToolRegistry()
        tools = CorpusTools(self.retriever, top_k=self.settings.retrieval.top_k)
        tools.register(registry)

        spec = self.settings.models["answer"]
        agent = Agent(
            provider=build_provider(spec.provider, spec.id),
            registry=registry,
            system=AGENT_SYSTEM,
            guardrails=Guardrails(
                max_steps=self.settings.answer.max_steps,
                max_cost_usd=self.settings.answer.max_cost_usd,
                max_wall_s=120.0,
                tool_allowlist={"search_corpus", "get_article", "list_chapters"},
            ),
            settings=_agent_settings(self.settings),
            max_tokens=spec.max_tokens,
        )

        result = agent.run(question)

        # Rank by the retriever's own ordering where available, then cut to
        # top_k so the answer layer sees the same budget as the direct path.
        passages = sorted(tools.collected.values(), key=lambda p: p.rank)[
            : self.settings.retrieval.top_k
        ]

        info = {
            "steps": result.steps,
            "searches": tools.searches,
            "tool_calls": result.tool_calls,
            "cost_usd": result.cost_usd,
            "stop_reason": result.stop_reason,
            "trace": result.trace,
        }
        log.info(
            "agent.gathered",
            question=question[:60],
            searches=len(tools.searches),
            passages=len(passages),
            steps=result.steps,
        )
        return passages, info


def _agent_settings(settings: Settings):  # noqa: ANN202
    """Translate this repo's settings into agent-core's Settings shape.

    agent-core owns cost accounting, so it needs the price table in its own
    format. Building it here keeps the two configs from having to match.
    """
    from agent_core.config import Price
    from agent_core.config import Settings as AgentSettings

    return AgentSettings(
        models={},
        pricing={
            model_id: Price(input=prices["input"], output=prices["output"])
            for model_id, prices in settings.pricing.items()
        },
    )
