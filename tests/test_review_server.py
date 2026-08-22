"""The review server, and the guarantee that nothing verifies itself.

The eval set is the ground truth every reported number rests on, so the tests
that matter here are the ones about *not* writing. Reading a question, loading
the page, searching the corpus -- none of it may mark a label verified. A
falsely verified label is worse than an unverified one, because nobody thinks
to re-check it.

This is the browser equivalent of the CLI's "refuse to run when stdin is not a
terminal" guard, and it exists because that failure already happened once:
piped input marked three labels human-verified that no human had read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

import review_server as rs  # noqa: E402
from schema import EvalSet  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A review server pointed at a throwaway copy of the eval set."""
    source = EvalSet.load(ROOT / "eval" / "questions.yaml")
    target = tmp_path / "questions.yaml"
    source.save(target)
    monkeypatch.setattr(rs, "QUESTIONS", target)
    return rs.ReviewState(use_sample=True, include_all=False)


@pytest.fixture
def client(state):
    from fastapi.testclient import TestClient

    return TestClient(rs.build_app(state))


def verified_count(state) -> int:
    return len(EvalSet.load(rs.QUESTIONS).verified())


# ---------------------------------------------------------------------------
# Nothing verifies without a real click
# ---------------------------------------------------------------------------


def test_viewing_a_question_never_marks_it_verified(client, state) -> None:
    before = verified_count(state)
    for index in range(5):
        assert client.get(f"/q/{index}").status_code == 200
    assert verified_count(state) == before


def test_searching_never_marks_anything_verified(client, state) -> None:
    """Search is a GET, and a GET must be free of side effects."""
    before = verified_count(state)
    response = client.get("/q/0", params={"search": "الإجازة"})
    assert response.status_code == 200
    assert verified_count(state) == before


def test_a_post_without_the_token_is_refused(client, state) -> None:
    """The browser equivalent of the terminal guard.

    Without this, a crawler, a link prefetcher or a stray curl could record a
    verification that no human made.
    """
    before = verified_count(state)
    response = client.post("/q/0/accept", data={"token": "not-the-token"})

    assert response.status_code == 403
    assert verified_count(state) == before


def test_a_post_with_no_token_at_all_is_refused(client, state) -> None:
    before = verified_count(state)
    response = client.post("/q/0/accept", data={})

    assert response.status_code == 422  # missing required form field
    assert verified_count(state) == before


def test_every_mutating_route_requires_the_token(client, state) -> None:
    """Enumerated so a new action cannot be added without a guard."""
    before = verified_count(state)
    for action in ("accept", "reject", "skip", "articles", "answerable", "unanswerable"):
        response = client.post(f"/q/0/{action}", data={"token": "wrong"})
        assert response.status_code == 403, f"{action} accepted a bad token"
    assert verified_count(state) == before


# ---------------------------------------------------------------------------
# The actions themselves
# ---------------------------------------------------------------------------


def test_accept_marks_verified_and_advances(client, state) -> None:
    first = state.question(0)
    response = client.post("/q/0/accept", data={"token": rs.TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/q/1"

    saved = EvalSet.load(rs.QUESTIONS)
    assert next(q for q in saved.questions if q.id == first.id).verified_by == "human"


def test_reject_removes_the_question(client, state) -> None:
    first = state.question(0)
    before = len(EvalSet.load(rs.QUESTIONS).questions)

    client.post("/q/0/reject", data={"token": rs.TOKEN}, follow_redirects=False)

    saved = EvalSet.load(rs.QUESTIONS)
    assert len(saved.questions) == before - 1
    assert all(q.id != first.id for q in saved.questions)


def test_skip_advances_without_verifying(client, state) -> None:
    before = verified_count(state)
    response = client.post("/q/0/skip", data={"token": rs.TOKEN}, follow_redirects=False)

    assert response.status_code == 303
    assert verified_count(state) == before


def test_fixing_articles_records_the_correction(client, state) -> None:
    first = state.question(0)
    client.post(
        "/q/0/articles",
        data={"token": rs.TOKEN, "articles": "12, 34"},
        follow_redirects=False,
    )

    saved = next(q for q in EvalSet.load(rs.QUESTIONS).questions if q.id == first.id)
    assert saved.relevant_articles == ["12", "34"]
    assert saved.verified_by == "human"


def test_flipping_an_unanswerable_to_answerable_needs_articles(client, state) -> None:
    """The specific risk the Regulations created.

    Five refusal labels were judged against the Labor Law alone, before the
    Regulations were indexed. Flipping one must attach the articles that make
    it answerable, or the label would claim answerable with nothing to cite.
    """
    index = next(i for i in range(len(state.order)) if not state.question(i).answerable)
    target = state.question(index)

    # No articles supplied: must not flip.
    client.post(
        f"/q/{index}/answerable",
        data={"token": rs.TOKEN, "articles": ""},
        follow_redirects=False,
    )
    saved = next(q for q in EvalSet.load(rs.QUESTIONS).questions if q.id == target.id)
    assert saved.answerable is False
    assert saved.verified_by == "drafted"

    # With articles: flips and verifies.
    client.post(
        f"/q/{index}/answerable",
        data={"token": rs.TOKEN, "articles": "88"},
        follow_redirects=False,
    )
    saved = next(q for q in EvalSet.load(rs.QUESTIONS).questions if q.id == target.id)
    assert saved.answerable is True
    assert saved.relevant_articles == ["88"]
    assert saved.verified_by == "human"


def test_flipping_to_unanswerable_clears_the_labels(client, state) -> None:
    index = next(i for i in range(len(state.order)) if state.question(i).answerable)
    target = state.question(index)

    client.post(f"/q/{index}/unanswerable", data={"token": rs.TOKEN}, follow_redirects=False)

    saved = next(q for q in EvalSet.load(rs.QUESTIONS).questions if q.id == target.id)
    assert saved.answerable is False
    assert saved.relevant_articles == []
    assert saved.verified_by == "human"


# ---------------------------------------------------------------------------
# Rendering: the reason this replaced the terminal CLI
# ---------------------------------------------------------------------------


def test_the_page_is_right_to_left(client) -> None:
    """cmd.exe cannot shape or join Arabic; a browser can, given dir=rtl."""
    body = client.get("/q/0").text

    assert "dir='rtl'" in body
    assert "lang='ar'" in body


def test_the_page_names_an_arabic_capable_font(client) -> None:
    body = client.get("/q/0").text
    assert "Segoe UI" in body or "Tahoma" in body


def test_the_article_text_reaches_the_page(client, state) -> None:
    """The whole point: judging a label requires seeing the article."""
    index = next(i for i in range(len(state.order)) if state.question(i).answerable)
    question = state.question(index)
    body = client.get(f"/q/{index}").text

    key = question.qualified_articles()[0]
    assert key in body


def test_the_sample_is_the_23_question_set(state) -> None:
    assert len(state.order) == 23


def test_an_out_of_range_index_shows_the_done_page(client) -> None:
    body = client.get("/q/999").text
    assert "finished" in body.lower()
