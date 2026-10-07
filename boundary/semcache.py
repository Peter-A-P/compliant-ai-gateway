"""A semantic cache, with its risk measured before it is switched on (0.25, PLAN.md B2.5).

An exact cache answers a request it has seen byte for byte. A semantic cache answers one it
has seen *something like*: the new request is embedded, the nearest stored request in the
same scope is found, and above a cosine threshold its answer is returned instead of calling
the model. That is where the savings are and where the risk is, because "something like" is
decided by a similarity score, and a confident answer to a different question is worse than
no answer at all.

**Scope.** Two requests are only ever compared if everything but the final user message is
identical: the model, the system prompt, every earlier turn and every generation setting.
Only the final user message is embedded. So a cache hit can only substitute one question
for another, never one system prompt, model or conversation for another.

**What it never caches**: pass-through calls, which the drift record depends on being real;
and, by the data policy's `cache` flag, `personal` and `sensitive` data. The proxy uses it
since 0.27, off unless `boundary serve --semantic-cache` turns it on, with one cache per team
and the threshold this module's measurement chose (docs/cache.md).

Embeddings are bge-small (`BAAI/bge-small-en-v1.5`), run locally through fastembed: the text
never leaves the machine to be embedded, which is the point of a boundary. The store here is
in memory, brute force over normalised vectors; pgvector on the VPS replaces it behind the
same interface. Pure Python on purpose: a few thousand entries is milliseconds, and the
library gains no numeric dependency outside the `cache` extra.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from boundary.types import ChatRequest

MODEL = "BAAI/bge-small-en-v1.5"


Vector = tuple[float, ...]


class Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[Vector]:
        """One unit-length vector per text."""
        ...


class BgeSmall:
    """bge-small through fastembed. Needs the `cache` extra; the model (about 130 MB) is
    downloaded once to fastembed's cache directory on first use."""

    def __init__(self, model: str = MODEL, *, threads: int | None = None) -> None:
        from fastembed import TextEmbedding

        self.model = model
        # ONNX Runtime's threads for one embedding. None is its default, every core, which
        # the proxy may want to lower: its requests embed concurrently, one per worker
        # thread, and each one fanning out over every core makes them contend (0.27).
        if threads is None and os.environ.get("BOUNDARY_EMBED_THREADS"):
            threads = int(os.environ["BOUNDARY_EMBED_THREADS"])
        self.threads = threads
        self._engine = TextEmbedding(model, threads=threads)

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [unit(tuple(float(x) for x in row)) for row in self._engine.embed(list(texts))]


def unit(v: Sequence[float]) -> Vector:
    norm = sum(x * x for x in v) ** 0.5
    return tuple(x / norm for x in v) if norm else tuple(v)


def cosine(a: Vector, b: Vector) -> float:
    """Of two unit vectors: their dot product."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def scope_of(request: ChatRequest) -> str:
    """Everything a hit must share: the request without its final user message."""
    messages = list(request.messages)
    if not messages or str(messages[-1].get("role")) != "user":
        raise ValueError("a semantic cache key needs a request ending in a user message")
    body: dict[str, Any] = {
        "model": request.model,
        "system": request.system,
        "earlier": [dict(m) for m in messages[:-1]],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
        "stop": list(request.stop) if request.stop is not None else None,
        "extra": dict(request.extra),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def question_of(request: ChatRequest) -> str:
    return str(request.messages[-1]["content"])


def question_sha256(request: ChatRequest) -> str:
    """The question's fingerprint, for the exact match (1.1.0). Compared only within one
    scope, so equal fingerprints mean the whole request is the same."""
    return hashlib.sha256(question_of(request).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Hit:
    entry: int
    similarity: float
    answer: str
    # What the proxy needs to answer from it (0.27): the stored call's `call_uid`, the model
    # that answered it and its finish reason. None when the store was not told.
    source: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    # Found by the question's fingerprint rather than by its embedding (1.1.0): the same
    # question in the same scope, so similarity 1.0 by definition.
    exact: bool = False


def _numpy() -> Any:
    """numpy when it is installed, which the `cache` extra guarantees (fastembed needs it);
    None otherwise, and the store falls back to pure Python."""
    try:
        import numpy
    except ImportError:
        return None
    return numpy


@dataclass
class _Scope:
    vectors: list[Vector] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)
    meta: list[tuple[str | None, str | None, str | None]] = field(default_factory=list)
    # Each question's fingerprint to the first entry stored for it (1.1.0).
    exact: dict[str, int] = field(default_factory=dict)
    # The vectors again as one float32 matrix, grown by doubling, when numpy is present
    # (0.27): a proxy's scope can hold thousands of entries, and scoring them in pure Python
    # costs tens of milliseconds a lookup where a matrix product costs well under one.
    matrix: Any = None

    def add(self, v: Vector, np: Any) -> None:
        self.vectors.append(v)
        if np is None:
            return
        n = len(self.vectors)
        if self.matrix is None:
            self.matrix = np.zeros((16, len(v)), dtype=np.float32)
        elif n > self.matrix.shape[0]:
            grown = np.zeros((self.matrix.shape[0] * 2, self.matrix.shape[1]), dtype=np.float32)
            grown[: n - 1] = self.matrix[: n - 1]
            self.matrix = grown
        self.matrix[n - 1] = v

    def scores(self, v: Vector, np: Any) -> list[float]:
        if np is None or self.matrix is None:
            return [cosine(v, w) for w in self.vectors]
        # einsum, not `@`: a matrix product goes to BLAS, which past about a thousand rows
        # fans out over every core, and with a lookup in each of several worker threads the
        # first 0.27 load test saw the proxy's overhead jump from 16 ms to 90 ms at that size.
        # einsum's own loop stays on the calling thread.
        m = self.matrix[: len(self.vectors)]
        out: list[float] = np.einsum("ij,j->i", m, np.asarray(v, np.float32)).tolist()
        return out


class SemanticCache:
    """In memory. `threshold` is the cosine at or above which a stored answer is returned."""

    def __init__(
        self, embedder: Embedder, *, threshold: float, max_entries: int | None = None
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.embedder = embedder
        self.threshold = threshold
        # A bound on memory for a long-running proxy: past it, nothing more is stored and
        # lookups carry on against what is there. None: no bound, as the measurement wants.
        self.max_entries = max_entries
        self._np = _numpy()
        self._scopes: dict[str, _Scope] = {}
        self._next = 0
        # The proxy looks up and stores from worker threads (0.27). Embedding runs outside
        # the lock; reading and appending a scope run inside it.
        self._lock = threading.Lock()

    def embed(self, request: ChatRequest) -> Vector:
        """The request's question embedded, so a miss can be stored without embedding it
        twice."""
        return self.embedder.embed([question_of(request)])[0]

    def exact(self, request: ChatRequest) -> Hit | None:
        """The entry stored for this very question in this scope, without embedding it; None
        when there is none (1.1.0). The proxy asks this first: a repeat then costs a hash
        rather than an embedding, is answered while the embedder is busy, and is never lost
        to an approximate index."""
        key = scope_of(request)
        h = question_sha256(request)
        with self._lock:
            scope = self._scopes.get(key)
            i = scope.exact.get(h) if scope is not None else None
            if scope is None or i is None:
                return None
            source, model, finish = scope.meta[i]
            return Hit(scope.ids[i], 1.0, scope.answers[i], source, model, finish, exact=True)

    def nearest(self, request: ChatRequest, vector: Vector | None = None) -> Hit | None:
        """The nearest stored request in scope, whatever its similarity; None when the scope
        is empty. `lookup` applies the threshold; the measurement reads this."""
        key = scope_of(request)
        with self._lock:
            if not (key in self._scopes and self._scopes[key].vectors):
                return None
        v = vector if vector is not None else self.embed(request)
        with self._lock:
            scope = self._scopes[key]
            sims = scope.scores(v, self._np)
            i = max(range(len(sims)), key=sims.__getitem__)
            source, model, finish = scope.meta[i]
            return Hit(scope.ids[i], sims[i], scope.answers[i], source, model, finish)

    def lookup(self, request: ChatRequest, vector: Vector | None = None) -> Hit | None:
        hit = self.nearest(request, vector)
        return hit if hit is not None and hit.similarity >= self.threshold else None

    def store(
        self,
        request: ChatRequest,
        answer: str,
        *,
        vector: Vector | None = None,
        source: str | None = None,
        model: str | None = None,
        finish_reason: str | None = None,
    ) -> int | None:
        """The new entry's id, or None when the cache is full."""
        key = scope_of(request)
        h = question_sha256(request)
        v = vector if vector is not None else self.embed(request)
        with self._lock:
            if self.max_entries is not None and self._next >= self.max_entries:
                return None
            scope = self._scopes.setdefault(key, _Scope())
            scope.exact.setdefault(h, len(scope.vectors))
            scope.add(v, self._np)
            scope.answers.append(answer)
            scope.meta.append((source, model, finish_reason))
            entry = self._next
            scope.ids.append(entry)
            self._next += 1
            return entry

    def __len__(self) -> int:
        return self._next


__all__ = [
    "MODEL",
    "BgeSmall",
    "Embedder",
    "Hit",
    "SemanticCache",
    "Vector",
    "cosine",
    "question_of",
    "question_sha256",
    "scope_of",
    "unit",
]
