"""A minimal completion interface for the answering layer.

`agent-core`'s Provider protocol is built around a tool-calling loop: it takes
a message list and tool schemas and returns tool calls. The answer layer needs
neither, it sends one prompt and reads one string back.

Rather than bend one interface to cover both, this wraps agent-core's provider
in the two-argument shape the answer layer actually wants. The dependency
direction stays as the spec requires (02 imports 01), and the agent path in
`qa/agent.py` uses agent-core directly and unwrapped.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.errors import ProviderError
from agent_core.providers import build_provider as build_agent_provider
from agent_core.providers.base import Message
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


@dataclass
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


class SimpleProvider:
    """One prompt in, one string out."""

    def __init__(self, provider: str, model_id: str) -> None:
        self.model_id = model_id
        self._inner = build_agent_provider(provider, model_id)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(ProviderError),
        reraise=True,
    )
    def complete(self, prompt: str, system: str, max_tokens: int = 2048) -> Completion:
        result = self._inner.complete(
            messages=[Message.user_text(prompt)],
            tools=[],
            system=system,
            max_tokens=max_tokens,
        )
        return Completion(
            text=result.text,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model=self.model_id,
        )


def build_provider(provider: str, model_id: str) -> SimpleProvider:
    return SimpleProvider(provider, model_id)
