"""Optional dialect routing, over HTTP, from repo 04.

Repo 04 classifies a question's dialect. A question in Gulf colloquial and one
in formal MSA are different retrieval problems, and knowing which one arrived
is useful.

Three properties, in order of importance:

**This service being down must not take this one down.** The client has a short
timeout, catches everything, and returns "unknown", so an unreachable, slow
or broken classifier degrades the answer path to exactly what it was before
routing existed. It is off unless `DIALECT_SERVICE_URL` is set.

**A low-confidence classification is reported as unknown**, not as a guess.
Repo 04's own `/route` applies a confidence floor and this client honours the
verdict rather than reading past it to the raw label.

**No retrieval improvement is claimed.** The dialect is attached to the
response and recorded; whether routing on it improves recall has NOT been
measured, and until it has, this is an integration point rather than a feature.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

DEFAULT_TIMEOUT_S = 1.5


@dataclass
class RouteDecision:
    dialect: str | None
    confidence: float
    route: str
    reason: str
    available: bool

    @classmethod
    def unavailable(cls, reason: str) -> RouteDecision:
        return cls(dialect=None, confidence=0.0, route="default", reason=reason, available=False)


class DialectRouter:
    """A thin client for repo 04's `/route`."""

    def __init__(self, url: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.url = url or os.environ.get("DIALECT_SERVICE_URL", "")
        self.timeout_s = timeout_s

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def classify(self, question: str, min_confidence: float = 0.60) -> RouteDecision:
        if not self.enabled:
            return RouteDecision.unavailable("DIALECT_SERVICE_URL is not set; routing is off")

        import httpx

        try:
            response = httpx.post(
                f"{self.url.rstrip('/')}/route",
                json={"question": question, "min_confidence": min_confidence},
                timeout=self.timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - a router outage is not an answer outage
            log.warning("router.unavailable", error=f"{type(exc).__name__}: {exc}")
            return RouteDecision.unavailable(f"dialect service unreachable: {type(exc).__name__}")

        return RouteDecision(
            dialect=body.get("dialect"),
            confidence=float(body.get("confidence", 0.0)),
            route=body.get("route", "default"),
            reason=body.get("reason", ""),
            available=True,
        )
