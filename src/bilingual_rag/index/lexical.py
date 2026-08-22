"""BM25 over the normalized, stemmed text.

Separate from Qdrant on purpose: Qdrant holds the dense vectors only, and the
two rankings are fused client-side in `fusion.py`. One fusion path, one
constant, one place to reason about.

`bm25s` rather than `rank_bm25`: the latter is pure Python and in-memory, and
becomes the bottleneck as a corpus grows. `bm25s` is memory-mapped and fast.

The text indexed here is `text_norm` -- normalized by the *same* function that
normalizes queries -- then light-stemmed. Indexing raw text while querying
normalized text is the silent failure this whole module is arranged to avoid.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from bilingual_rag.arabic.normalize import normalize_ar
from bilingual_rag.arabic.stem import light_stem

log = structlog.get_logger(__name__)

# Word characters in any script, excluding digits and punctuation. Arabic
# needs \w with the Unicode flag; a naive [a-z]+ finds no Arabic at all, which
# is benchmark task 07 in repo 01 for a reason.
TOKEN = re.compile(r"[^\W\d_]+", re.UNICODE)


def tokenize(text: str, stem: bool = True) -> list[str]:
    """Normalize, split into words, and optionally light-stem.

    Always goes through `normalize_ar`, so the index and the query are folded
    identically. `tests/test_lexical.py` asserts that property directly.
    """
    tokens = TOKEN.findall(normalize_ar(text))
    return [light_stem(t) for t in tokens] if stem else tokens


class BM25Index:
    """A BM25 index over chunk ids."""

    def __init__(self, stem: bool = True) -> None:
        self.stem = stem
        self._retriever = None
        self._ids: list[str] = []

    def build(self, chunk_ids: list[str], texts: list[str]) -> None:
        """Index a corpus. `texts` should be the normalized chunk text."""
        import bm25s

        self._ids = list(chunk_ids)
        corpus_tokens = [tokenize(text, self.stem) for text in texts]

        self._retriever = bm25s.BM25()
        # show_progress=False: this is library code, and a progress bar written to
        # stdout corrupts any caller that is producing machine-readable output.
        self._retriever.index(corpus_tokens, show_progress=False)
        log.info("bm25.built", documents=len(self._ids), stemmed=self.stem)

    def search(self, query: str, top_k: int = 50) -> list[tuple[str, float]]:
        """Return (chunk_id, score), best first."""
        if self._retriever is None:
            raise RuntimeError("BM25 index has not been built or loaded")

        query_tokens = tokenize(query, self.stem)
        if not query_tokens:
            return []

        k = min(top_k, len(self._ids))
        if k == 0:
            return []

        indices, scores = self._retriever.retrieve([query_tokens], k=k, show_progress=False)
        return [
            (self._ids[int(indices[0][i])], float(scores[0][i])) for i in range(indices.shape[1])
        ]

    def search_ids(self, query: str, top_k: int = 50) -> list[str]:
        return [chunk_id for chunk_id, _ in self.search(query, top_k)]

    # ---- persistence ----------------------------------------------------

    def save(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if self._retriever is None:
            raise RuntimeError("nothing to save: index has not been built")

        self._retriever.save(str(directory))
        (directory / "chunk_ids.json").write_text(
            json.dumps({"ids": self._ids, "stem": self.stem}, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("bm25.saved", path=str(directory), documents=len(self._ids))

    @classmethod
    def load(cls, directory: str | Path) -> BM25Index:
        import bm25s

        directory = Path(directory)
        meta = json.loads((directory / "chunk_ids.json").read_text(encoding="utf-8"))

        index = cls(stem=meta.get("stem", True))
        index._ids = meta["ids"]
        index._retriever = bm25s.BM25.load(str(directory))
        log.info("bm25.loaded", path=str(directory), documents=len(index._ids))
        return index

    def __len__(self) -> int:
        return len(self._ids)
