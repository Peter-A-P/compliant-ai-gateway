"""The semantic cache (0.25): scope, threshold and the measurement's arithmetic, with a
deterministic embedder so nothing is downloaded."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

import pytest

from boundary.semcache import SemanticCache, Vector, cosine, scope_of, unit
from boundary.semcache_eval import Curve, Point, choose
from boundary.types import ChatRequest


class _Words:
    """A bag-of-words embedder: two texts are as similar as the words they share."""

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.lower().replace("?", "").split():
                v[int(hashlib.sha256(w.encode()).hexdigest(), 16) % 64] += 1.0
            out.append(unit(v))
        return out


def _req(text: str, **kw: object) -> ChatRequest:
    return ChatRequest(model="m", messages=[{"role": "user", "content": text}], **kw)  # type: ignore[arg-type]


def test_a_near_question_hits_and_a_far_one_does_not() -> None:
    cache = SemanticCache(_Words(), threshold=0.7)
    cache.store(_req("how long can a bank hold my cheque"), "up to 8 days")
    hit = cache.lookup(_req("how long can the bank hold my cheque"))
    assert hit is not None and hit.answer == "up to 8 days"
    assert cache.lookup(_req("what is an index fund")) is None


def test_scope_keeps_system_prompts_models_and_settings_apart() -> None:
    cache = SemanticCache(_Words(), threshold=0.1)
    cache.store(_req("hold period", system="A"), "x")
    assert cache.lookup(_req("hold period", system="B")) is None
    assert cache.lookup(_req("hold period", system="A", max_tokens=5)) is None
    assert cache.lookup(_req("hold period", system="A")) is not None
    assert scope_of(_req("q1", system="A")) == scope_of(_req("q2", system="A"))


class _Counting(_Words):
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        self.calls += 1
        return super().embed(texts)


def test_the_same_question_is_found_by_its_fingerprint_without_embedding() -> None:
    """1.1.0: an exact repeat is found by the question's sha256 within its scope, before and
    without an embedding; anything else, however near, is left to the embedding."""
    embedder = _Counting()
    cache = SemanticCache(embedder, threshold=0.7, max_entries=2)
    q = _req("how long can a bank hold my cheque", system="A")
    assert cache.exact(q) is None
    cache.store(q, "up to 8 days", source="uid-1")
    cache.store(q, "a second answer")
    embedded = embedder.calls
    hit = cache.exact(q)
    assert hit is not None and hit.exact and hit.similarity == 1.0
    assert (hit.answer, hit.source) == ("up to 8 days", "uid-1"), "the first stored is kept"
    assert embedder.calls == embedded, "nothing was embedded"
    assert cache.exact(_req("how long can a bank hold my cheque", system="B")) is None
    near = _req("How long can a bank hold my cheque?", system="A")
    assert cache.exact(near) is None
    found = cache.lookup(near)
    assert found is not None and not found.exact
    assert cache.store(_req("full now", system="A"), "x") is None
    assert cache.exact(_req("full now", system="A")) is None


def test_the_threshold_is_checked() -> None:
    with pytest.raises(ValueError):
        SemanticCache(_Words(), threshold=0.0)


def test_cosine_of_unit_vectors() -> None:
    assert cosine(unit([1.0, 0.0]), unit([2.0, 0.0])) == pytest.approx(1.0)
    assert cosine(unit([1.0, 0.0]), unit([0.0, 3.0])) == pytest.approx(0.0)


def test_choose_takes_the_lowest_threshold_within_the_false_hit_limit() -> None:
    c = Curve("chat", "odd", "paraphrase")
    c.points = [
        Point(0.8, queries=100, seen=50, correct=50, false=10),
        Point(0.85, queries=100, seen=50, correct=49, false=1),
        Point(0.9, queries=100, seen=50, correct=40, false=0),
    ]
    assert choose(c) == 0.85
    assert c.at(0.85).false_hit_rate.hits == 1


def test_the_numpy_store_scores_as_the_pure_one_does_as_it_grows() -> None:
    """0.27: with numpy, a scope keeps its vectors as a matrix grown by doubling. Its scores
    must be the pure-Python cosines, through every growth step."""
    import random

    import pytest as _pytest

    from boundary.semcache import _numpy, _Scope, cosine, unit

    np = _numpy()
    if np is None:
        _pytest.skip("numpy is the cache extra's")
    rng = random.Random(7)
    fast, slow = _Scope(), _Scope()
    for _ in range(40):
        v = unit([rng.uniform(-1, 1) for _ in range(24)])
        fast.add(v, np)
        slow.add(v, None)
    q = unit([rng.uniform(-1, 1) for _ in range(24)])
    assert fast.matrix is not None and fast.matrix.shape[0] == 64
    assert fast.scores(q, np) == _pytest.approx(slow.scores(q, None), abs=1e-6)
    assert slow.scores(q, None) == [cosine(q, w) for w in slow.vectors]
