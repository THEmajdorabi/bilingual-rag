# The API and UI. Qdrant runs as a separate service; see docker-compose.yml.
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

COPY pyproject.toml README.md config.yaml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -e . --extra-index-url https://download.pytorch.org/whl/cpu

COPY scripts/ ./scripts/
COPY eval/ ./eval/

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
