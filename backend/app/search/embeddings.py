"""Embedding and reranking models behind small interfaces, so the backend never depends on a
specific library (§18: "the model should be replaceable without changing the core backend").

All calls are synchronous and CPU-bound; callers run them in a thread.
"""

import hashlib
import math
import re
import threading
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings

EMBEDDING_DIM = 384  # fixed by the vector(384) column; a different size needs a migration


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    model_name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """Relevance of each text to the query; higher is better."""
        ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str, cache_dir: Path | None, batch_size: int) -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name
        self.batch_size = batch_size
        self._model = TextEmbedding(model_name, cache_dir=str(cache_dir) if cache_dir else None)
        self.dim = len(next(iter(self._model.embed(["probe"]))))
        self._lock = threading.Lock()  # the ONNX session is shared across threads

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            vectors = self._model.embed(list(texts), batch_size=self.batch_size)
            return [v.tolist() for v in vectors]

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            # query_embed adds the model's query instruction (bge: "Represent this sentence...").
            vector: Any = next(iter(self._model.query_embed(text)))
            return list(vector.tolist())


class FastEmbedReranker:
    def __init__(self, model_name: str, cache_dir: Path | None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.model_name = model_name
        self._model = TextCrossEncoder(model_name, cache_dir=str(cache_dir) if cache_dir else None)
        self._lock = threading.Lock()

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        with self._lock:
            return [float(s) for s in self._model.rerank(query, list(texts))]


_TOKEN = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder:
    """Deterministic bag-of-words vectors via feature hashing. Texts sharing words get
    similar vectors, which is enough to test retrieval plumbing without a model download.
    Not semantic: "revenue" and "income" are unrelated here."""

    model_name = "hashing-v1"

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@lru_cache(maxsize=4)
def _cached_embedder(backend: str, model: str, cache_dir: Path | None, batch: int) -> Embedder:
    if backend == "hashing":
        return HashingEmbedder()
    return FastEmbedEmbedder(model, cache_dir, batch)


@lru_cache(maxsize=4)
def _cached_reranker(backend: str, model: str, cache_dir: Path | None) -> Reranker | None:
    if backend == "none":
        return None
    return FastEmbedReranker(model, cache_dir)


def get_embedder(settings: Settings) -> Embedder:
    """One model instance per process (loading takes ~1s and ~100 MB)."""
    embedder = _cached_embedder(
        settings.embedding_backend,
        settings.embedding_model,
        settings.model_cache_dir,
        settings.embedding_batch_size,
    )
    if embedder.dim != EMBEDDING_DIM:
        raise RuntimeError(
            f"{embedder.model_name} produces {embedder.dim}-d vectors; the schema stores "
            f"{EMBEDDING_DIM}-d. Changing models of a different size needs a migration."
        )
    return embedder


def get_reranker(settings: Settings) -> Reranker | None:
    return _cached_reranker(
        settings.reranker_backend, settings.reranker_model, settings.model_cache_dir
    )
