.PHONY: setup test test-integration lint format typecheck ingest serve eval ablate answers review deploy clean

PY := .venv/Scripts/python.exe

setup:  ## venv, dependencies, corpus
	uv venv --python 3.12
	uv pip install -e ".[dev]"
	$(PY) scripts/download_corpus.py
	$(PY) scripts/check_corpus_quality.py data/boe_labor_law.html data/hrsd_regulations_ar.pdf

test:  ## no API calls, no Qdrant, no models
	$(PY) -m pytest -q

test-integration:  ## needs Qdrant and an ingested corpus
	$(PY) -m pytest -q -m integration

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy src/bilingual_rag

ingest:  ## build both indexes; --build-raw also builds the ablation baseline
	$(PY) scripts/ingest.py --recreate --build-raw

serve:  ## API + RTL UI on http://127.0.0.1:8080
	$(PY) scripts/serve.py

review:  ## browser label review, http://127.0.0.1:8777
	$(PY) eval/review_server.py

eval:  ## retrieval metrics; no API key needed
	$(PY) eval/run_eval.py

ablate:  ## the README ablation table; no API key needed
	$(PY) eval/ablate.py --repeats 3

answers:  ## answer quality; COSTS MONEY
	$(PY) eval/run_answer_eval.py

diagnose:  ## is the eval set discriminating?
	$(PY) scripts/diagnose_eval_difficulty.py

deploy:  ## Qdrant + API in containers
	docker compose up -d --build
	@echo "UI: http://localhost:8080/"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache data/.embedding_cache
