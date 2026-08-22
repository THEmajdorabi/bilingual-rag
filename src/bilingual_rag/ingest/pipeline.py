"""Ingestion, end to end and idempotent.

    extract -> chunk -> embed -> Qdrant (dense)
                             \\-> BM25 (lexical)

Idempotent in the sense that matters: chunk ids are content hashes, Qdrant
point ids derive from them, and embeddings are cached by the same hash. Running
this twice on an unchanged corpus re-writes identical data and spends nothing
on the model.

The corpus quality gate runs first. Two of the three candidate sources for this
project extracted cleanly and were silently corrupt, so "it parsed" is not
evidence that a document is fit to index.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import structlog

from bilingual_rag.config import Profile, QdrantSettings, active_profile
from bilingual_rag.index.embedder import Embedder
from bilingual_rag.index.lexical import BM25Index
from bilingual_rag.index.vectors import VectorStore
from bilingual_rag.ingest.chunk import Chunk, chunk_document, size_histogram
from bilingual_rag.ingest.extract import extract

log = structlog.get_logger(__name__)

BM25_DIR = Path("data/bm25_index")
BM25_RAW_DIR = Path("data/bm25_index_raw")


@dataclass
class IngestReport:
    """What ingestion produced, for the README and for `/healthz`."""

    doc_id: str
    chunks: int
    articles: int
    points_written: int
    bm25_documents: int
    profile: str
    embedding_model: str
    collection: str
    histogram: str

    def summary(self) -> str:
        return (
            f"{self.chunks} chunks from {self.articles} articles -> "
            f"{self.points_written} vectors in {self.collection} "
            f"({self.embedding_model}, profile {self.profile})"
        )


def load_chunks(path: str | Path) -> list[Chunk]:
    """Read chunks back from JSONL."""
    return [
        Chunk.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def save_chunks(chunks: list[Chunk], path: str | Path) -> None:
    """Write chunks to JSONL, so the eval and ablation can reuse them."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(chunk.model_dump_json() + "\n")
    log.info("ingest.chunks_saved", path=str(target), count=len(chunks))


def build_bm25(chunks: list[Chunk], directory: Path = BM25_DIR) -> BM25Index:
    """Build and persist the lexical index over normalized chunk text."""
    index = BM25Index(stem=True)
    index.build([c.id for c in chunks], [c.text_norm for c in chunks])
    index.save(directory)
    return index


def build_raw_indexes(
    chunks: list[Chunk],
    embedder: Embedder,
    profile: Profile,
    qdrant_settings: QdrantSettings | None,
    recreate: bool,
) -> int:
    """Build the un-normalized sibling indexes used by the ablation baseline.

    Without these, the ablation's "no normalization" row silently queries the
    normalized index and reports that normalization is worth nothing -- which
    is a wrong number, not a missing one.

    Not built by default: it doubles ingestion cost and is only needed to
    produce one row of one table.
    """
    raw_vectors = embedder.embed_passages([c.embedding_text() for c in chunks], normalize=False)
    raw_store = VectorStore(profile=profile, settings=qdrant_settings)
    raw_store.collection_override = profile.raw_collection_name
    raw_store.ensure_collection(recreate=recreate)
    written = raw_store.upsert(chunks, raw_vectors)

    # BM25 over raw text, unstemmed and unnormalized, so the row is a true
    # baseline rather than a half-normalized hybrid.
    raw_bm25 = BM25Index(stem=False)
    raw_bm25.build([c.id for c in chunks], [c.text for c in chunks])
    raw_bm25.save(BM25_RAW_DIR)

    log.info("ingest.raw_indexes_built", points=written, collection=raw_store.collection)
    return written


def ingest(
    source: str | Path,
    doc_id: str,
    chunks_path: str | Path = "data/chunks.jsonl",
    profile: Profile | None = None,
    qdrant_settings: QdrantSettings | None = None,
    recreate: bool = False,
    check_quality: bool = True,
    build_raw: bool = False,
) -> IngestReport:
    """Run the whole pipeline and return what it produced."""
    profile = profile or active_profile()
    source = Path(source)

    if check_quality:
        _gate_on_quality(source)

    document = extract(source, doc_id=doc_id)
    chunks = chunk_document(document)
    if not chunks:
        raise RuntimeError(f"no chunks produced from {source}: check the article heading pattern")

    save_chunks(chunks, chunks_path)

    # Dense vectors. The embedding text carries the heading path prefix.
    embedder = Embedder(profile=profile)
    vectors = embedder.embed_passages([c.embedding_text() for c in chunks])
    embedder.save_cache()
    # Free VRAM before anything else loads a model: on the 8 GB profile the
    # embedder and the reranker do not fit at the same time.
    embedder.unload()

    store = VectorStore(profile=profile, settings=qdrant_settings)
    store.ensure_collection(recreate=recreate)
    written = store.upsert(chunks, vectors)

    bm25 = build_bm25(chunks)

    if build_raw:
        # Re-load the embedder: it was unloaded above to free VRAM.
        raw_embedder = Embedder(profile=profile)
        build_raw_indexes(chunks, raw_embedder, profile, qdrant_settings, recreate)
        raw_embedder.save_cache()
        raw_embedder.unload()

    report = IngestReport(
        doc_id=doc_id,
        chunks=len(chunks),
        articles=len({c.article_no for c in chunks if c.article_no}),
        points_written=written,
        bm25_documents=len(bm25),
        profile=profile.name,
        embedding_model=profile.embedding_model,
        collection=profile.collection_name,
        histogram=size_histogram(chunks),
    )
    log.info("ingest.done", **{"summary": report.summary()})
    return report


def _gate_on_quality(source: Path) -> None:
    """Refuse to index a source that fails the corpus quality checks."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    try:
        from check_corpus_quality import analyse
    except ImportError:
        log.warning("ingest.quality_check_unavailable", hint="scripts/ not importable")
        return

    report = analyse(source)
    if not report.usable:
        raise RuntimeError(
            f"{source.name} failed the corpus quality check and must not be indexed:\n  - "
            + "\n  - ".join(report.problems)
            + "\nSee data/README.md for why this gate exists."
        )
    log.info("ingest.quality_ok", source=source.name)


def write_report(report: IngestReport, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(
            {
                "doc_id": report.doc_id,
                "chunks": report.chunks,
                "articles": report.articles,
                "points": report.points_written,
                "bm25_documents": report.bm25_documents,
                "profile": report.profile,
                "embedding_model": report.embedding_model,
                "collection": report.collection,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
