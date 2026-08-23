"""Environment overrides for deployment-varying settings.

The one that matters: `localhost` is right on a laptop and wrong inside a
container, where Qdrant is a sibling service. Getting it wrong produced
`[Errno 111] Connection refused` at startup with nothing pointing at the cause.
"""

from __future__ import annotations

from bilingual_rag.config import load_settings


def test_qdrant_url_comes_from_the_environment_when_set(monkeypatch):
    load_settings.cache_clear()
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    assert load_settings().qdrant.url == "http://qdrant:6333"
    load_settings.cache_clear()


def test_qdrant_url_falls_back_to_config_yaml(monkeypatch):
    load_settings.cache_clear()
    monkeypatch.delenv("QDRANT_URL", raising=False)
    assert load_settings().qdrant.url.startswith("http://")
    load_settings.cache_clear()
