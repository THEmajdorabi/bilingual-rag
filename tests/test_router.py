"""The optional dialect router.

Every test here is about the same property: repo 04 being absent, slow or
broken must not change what repo 02 returns.
"""

from __future__ import annotations

import httpx
import pytest

from bilingual_rag.qa.router import DialectRouter, RouteDecision


def test_routing_is_off_when_the_url_is_not_set(monkeypatch):
    monkeypatch.delenv("DIALECT_SERVICE_URL", raising=False)
    router = DialectRouter()
    assert not router.enabled

    decision = router.classify("كم مدة الإشعار؟")
    assert not decision.available
    assert decision.dialect is None
    assert decision.route == "default"


def test_an_unreachable_service_degrades_instead_of_raising(monkeypatch):
    """A router outage must not become an answer outage."""
    # Port 9 is the discard port: nothing listens, and the connection fails fast.
    router = DialectRouter(url="http://127.0.0.1:9", timeout_s=0.5)
    decision = router.classify("ايه الاخبار")

    assert not decision.available
    assert decision.route == "default"
    assert "unreachable" in decision.reason


def test_a_slow_service_times_out_rather_than_blocking(monkeypatch):
    def slow_post(*args, **kwargs):
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(httpx, "post", slow_post)
    decision = DialectRouter(url="http://example.invalid").classify("نص")
    assert not decision.available
    assert "ReadTimeout" in decision.reason


def test_a_confident_classification_is_passed_through(monkeypatch):
    def fake_post(url, json, timeout):  # noqa: ANN001, A002
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "dialect": "EG",
                "confidence": 0.93,
                "route": "dialect",
                "reason": "classified as EG",
            },
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    decision = DialectRouter(url="http://example.invalid").classify("ايه الاخبار")

    assert decision.available
    assert decision.dialect == "EG"
    assert decision.confidence == pytest.approx(0.93)


def test_the_services_own_refusal_to_route_is_honoured(monkeypatch):
    """Repo 04 applies the confidence floor; this client does not read past it."""

    def fake_post(url, json, timeout):  # noqa: ANN001, A002
        return httpx.Response(
            200,
            json={
                "dialect": None,
                "confidence": 0.31,
                "route": "default",
                "reason": "below the floor",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    decision = DialectRouter(url="http://example.invalid").classify("نص غامض")

    assert decision.available
    assert decision.dialect is None
    assert decision.route == "default"


def test_a_500_is_treated_as_unavailable(monkeypatch):
    def fake_post(url, json, timeout):  # noqa: ANN001, A002
        return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    decision = DialectRouter(url="http://example.invalid").classify("نص")
    assert not decision.available


def test_unavailable_decisions_are_uniform():
    decision = RouteDecision.unavailable("any reason")
    assert (decision.dialect, decision.route, decision.available) == (None, "default", False)
