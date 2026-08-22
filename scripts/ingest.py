"""Ingest the corpus into Qdrant and BM25.

Run: python scripts/ingest.py [--recreate]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilingual_rag.config import QdrantSettings, load_settings  # noqa: E402
from bilingual_rag.ingest.pipeline import ingest, write_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true", help="drop and rebuild the collection")
    parser.add_argument("--source", default=None, help="override the corpus path")
    parser.add_argument("--skip-quality-check", action="store_true")
    parser.add_argument(
        "--build-raw",
        action="store_true",
        help="also build un-normalized indexes, needed by the ablation baseline",
    )
    args = parser.parse_args()

    settings = load_settings()
    report = ingest(
        source=args.source or settings.corpus_path,
        doc_id=settings.doc_id,
        chunks_path=settings.chunks_path,
        qdrant_settings=QdrantSettings(**settings.qdrant.model_dump()),
        recreate=args.recreate,
        check_quality=not args.skip_quality_check,
        build_raw=args.build_raw,
    )

    print()
    print(report.summary())
    print()
    print("chunk size distribution (characters):")
    print(report.histogram)

    write_report(report, "data/ingest_report.json")
    print("\nwrote data/ingest_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
