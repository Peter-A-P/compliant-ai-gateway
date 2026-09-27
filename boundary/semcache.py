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
and, by the data policy's `cache` flag, `personal` and `sensitive` data. The proxy wiring is
the next step; this module and its measurement come first so that the threshold the proxy
ships with is one a stranger can reproduce (docs/cache.md).

Embeddings are bge-small (`BAAI/bge-small-en-v1.5`), run locally through fastembed: the text
never leaves the machine to be embedded, which is the point of a boundary. The store here is
in memory, brute force over normalised vectors; pgvector on the VPS replaces it behind the
same interface. Pure Python on purpose: a few thousand entries is milliseconds, and the
library gains no numeric dependency outside the `cache` extra.
"""

from __future__ import annotations

import hashlib
import json
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

    def __init__(self, model: str = MODEL) -> None:
        from fastembed import TextEmbedding

        self.model = model
        self._engine = TextEmbedding(model)

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


@dataclass(frozen=True, slots=True)
class Hit:
    entry: int
    similarity: float
    answer: str


@dataclass
class _Scope:
    vectors: list[Vector] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    ids: list[int] = field(default_factory=list)


class SemanticCache:
    """In memory. `threshold` is the cosine at or above which a stored answer is returned."""

    def __init__(self, embedder: Embedder, *, threshold: float) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        self.embedder = embedder
        self.threshold = threshold
        self._scopes: dict[str, _Scope] = {}
        self._next = 0

    def nearest(self, request: ChatRequest) -> Hit | None:
        """The nearest stored request in scope, whatever its similarity; None when the scope
        is empty. `lookup` applies the threshold; the measurement reads this."""
        scope = self._scopes.get(scope_of(request))
        if scope is None or not scope.vectors:
            return None
        v = self.embedder.embed([question_of(request)])[0]
        sims = [cosine(v, w) for w in scope.vectors]
        i = max(range(len(sims)), key=sims.__getitem__)
        return Hit(scope.ids[i], sims[i], scope.answers[i])

    def lookup(self, request: ChatRequest) -> Hit | None:
        hit = self.nearest(request)
        return hit if hit is not None and hit.similarity >= self.threshold else None

    def store(self, request: ChatRequest, answer: str) -> int:
        scope = self._scopes.setdefault(scope_of(request), _Scope())
        scope.vectors.append(self.embedder.embed([question_of(request)])[0])
        scope.answers.append(answer)
        entry = self._next
        scope.ids.append(entry)
        self._next += 1
        return entry


__all__ = [
    "MODEL",
    "BgeSmall",
    "Embedder",
    "Hit",
    "SemanticCache",
    "Vector",
    "cosine",
    "question_of",
    "scope_of",
    "unit",
]
