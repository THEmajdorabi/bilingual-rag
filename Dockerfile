# The API and UI. Qdrant runs as a separate service; see docker-compose.yml.
#
# IMPORTANT: the build context is the PARENT directory, not this one:
#
#     docker build -f bilingual-rag/Dockerfile ..
#
# `docker compose up` does this for you. The reason is `agent-core`: this repo
# depends on it as a local path, `[tool.uv.sources]` is a uv mechanism that pip
# inside the image does not read, and a sibling directory is not reachable from
# a build context rooted here. Building from the parent puts both repos in
# scope, which is the smallest honest fix. See DECISIONS.md.
FROM python:3.12-slim

# OCR system packages: tesseract with the Arabic language pack, and poppler for
# pdf2image. Needed only for scanned PDFs, but a clean clone must not fail at
# step one of the definition of done, so they are installed here rather than
# left as a README instruction.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-ara poppler-utils curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Repo 01, installed first: repo 02 imports it, and pip cannot resolve the
# path source that uv uses.
COPY agent-core/pyproject.toml agent-core/README.md /opt/agent-core/
COPY agent-core/src/ /opt/agent-core/src/
RUN pip install --no-cache-dir -e /opt/agent-core

COPY bilingual-rag/pyproject.toml bilingual-rag/README.md bilingual-rag/config.yaml ./
COPY bilingual-rag/src/ ./src/
RUN pip install --no-cache-dir -e . --extra-index-url https://download.pytorch.org/whl/cpu

COPY bilingual-rag/scripts/ ./scripts/
COPY bilingual-rag/eval/ ./eval/

# The container has no GPU, so it runs the cpu profile: multilingual-e5-base at
# 768 dims. That is a DIFFERENT collection from a cuda ingest, which is why
# collection names encode the model -- a mismatch fails loudly at startup
# instead of returning nonsense.
ENV BILINGUAL_RAG_PROFILE=cpu \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models

VOLUME ["/models"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8080"]
