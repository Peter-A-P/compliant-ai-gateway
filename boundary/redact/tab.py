"""The Text Anonymization Benchmark (TAB): the first public corpus, and the first real text.

Every other redaction figure in this repository comes from a corpus generated here or by
project 07, which measures the engine against shapes somebody thought to generate. TAB is
1,268 judgments of the European Court of Human Rights, annotated by hand, some documents by
as many as ten annotators, with every mention marked DIRECT (identifies a person on its
own), QUASI (identifies in combination) or NO_MASK (safe to leave). Pilan, Lison, Ovrelid,
Papadopoulou, Sanchez and Batet, "The Text Anonymization Benchmark (TAB)", Computational
Linguistics 48(4), 2022, arXiv:2202.00443. Released under the MIT licence
(https://github.com/NorskRegnesentral/text-anonymization-benchmark); the notice is kept by
citing it here and in docs/redact.md.

The test split is for reporting. Fixes are developed on the train split and checked on dev
before test is run again (0.9.0, 0.10.0), and every test run is kept beside the ones before
it in docs/redact.md. Each file is downloaded at a pinned commit, checked against a fixed SHA-256, and kept in `.cache/`,
which is gitignored: nothing from the corpus is committed, and only aggregate numbers are
published, never a name from a judgment.

Scoring follows the benchmark's own `evaluation.py`, so the figures mean what TAB's mean:

- An **entity** counts as masked only if every mention of it that needs masking is masked.
- A **mention** counts as masked when every character is covered, ignoring punctuation and
  the words the benchmark ignores (titles such as "Mr", and function words).
- With several annotators, counts are **micro-averaged over annotators**: each annotator's
  entities are counted separately.
- **Precision** is TAB's token-level precision with uniform weights: of the tokens masked,
  the share the annotators also masked, averaged over annotators.

One deviation, stated: TAB ignores function words by their spaCy part of speech, and this
module uses a fixed word list instead so that it needs no model. The list is below.

Intervals are a bootstrap over documents, not Wilson intervals, because entities in one
judgment are not independent of each other: one missed recogniser misses a name in every
paragraph, and an interval that treated those as separate draws would be too narrow.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
import re
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import truststore

from boundary.redact.analyzer import Analyzer
from boundary.redact.policy import Policy
from boundary.redact.sweep import sweep
from boundary.redact.types import Recogniser

TAB_COMMIT = "558e09e26d6b36f5f78440074e6a233946d98bd9"
TAB_URL = "https://raw.githubusercontent.com/NorskRegnesentral/text-anonymization-benchmark"
# split -> (file, SHA-256 of the file at TAB_COMMIT). A different file is refused, because a
# benchmark whose contents can change under a fixed name is not a benchmark.
SPLITS: dict[str, tuple[str, str]] = {
    "test": ("echr_test.json", "cd0f0f15f84a8739654c7cf30c6be8ce27b051ef73974d39d792a0cb8c846379"),
    # For developing a fix. The test split is for reporting one, once.
    "train": (
        "echr_train.json",
        "4aba41f8ac305ff9e93dd6f0bbc16756e57e9ace396c827931fab70e18d8c6a6",
    ),
    "dev": ("echr_dev.json", "8c3c7306f46b8d54debeb38ae11d8b0b8bcf4bdccbc3b6f13c12ad7be16893ec"),
}
DEFAULT_DIR = Path(".cache/tab")

README_START = "<!-- tab:start -->"
README_END = "<!-- tab:end -->"

# From TAB's evaluation.py: characters and tokens that do not count against a mask.
CHARACTERS_TO_IGNORE = frozenset(
    " ,.-;:/&()[]'\"" + chr(0x2013) + chr(0x2019) + chr(0x201C) + chr(0x201D)
)
TOKENS_TO_IGNORE = frozenset({"mr", "mrs", "ms", "no", "nr", "about"})
# In place of TAB's spaCy parts of speech ADP, PART, CCONJ and DET. Kept as one string
# because a literal of thirty-seven words formats one per line and cannot be read.
_FUNCTION_WORDS = (
    "a an the this that these those of in on at to for from by with into onto upon "
    "under over between against during before after within without per via "
    "and or but nor not v hereinafter"
)
FUNCTION_WORDS = frozenset(_FUNCTION_WORDS.split())
_TOKEN = re.compile(r"\w+", re.UNICODE)


@dataclass
class Entity:
    """One annotator's entity: every mention of one person, place, date or code."""

    annotator: str
    entity_type: str
    is_direct: bool
    need_masking: bool
    # (start, end, this mention needs masking)
    mentions: list[tuple[int, int, bool]]


@dataclass
class Document:
    doc_id: str
    text: str
    annotators: list[str]
    entities: list[Entity]
    # (annotator, start, end) of every NO_MASK mention
    safe: list[tuple[str, int, int]]


def fetch(split: str = "test", *, directory: Path = DEFAULT_DIR) -> Path:
    """The split's file, downloaded at the pinned commit if it is not already here, and
    refused unless its SHA-256 is the one recorded above."""
    name, digest = SPLITS[split]
    path = directory / name
    if path.is_file() and _sha256(path.read_bytes()) == digest:
        return path
    ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with httpx.Client(verify=ctx, timeout=120.0, follow_redirects=True) as client:
        resp = client.get(f"{TAB_URL}/{TAB_COMMIT}/{name}")
        resp.raise_for_status()
        body = resp.content
    got = _sha256(body)
    if got != digest:
        raise ValueError(f"{name} at {TAB_COMMIT[:12]} has SHA-256 {got}, expected {digest}")
    directory.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def load(path: Path) -> list[Document]:
    """Documents with entities built the way TAB's evaluation builds them."""
    docs: list[Document] = []
    for raw in json.loads(path.read_text(encoding="utf-8")):
        text = raw["text"]
        entities: list[Entity] = []
        safe: list[tuple[str, int, int]] = []
        for annotator, ann in raw["annotations"].items():
            by_id: dict[str, Entity] = {}
            for m in ann["entity_mentions"]:
                s, e, kind = int(m["start_offset"]), int(m["end_offset"]), m["identifier_type"]
                if not 0 <= s < e <= len(text):
                    raise ValueError(f"{raw['doc_id']}: bad offsets {s} to {e}")
                need = kind in ("DIRECT", "QUASI")
                if kind == "NO_MASK":
                    safe.append((annotator, s, e))
                ent = by_id.get(m["entity_id"])
                if ent is None:
                    by_id[m["entity_id"]] = Entity(
                        annotator, m["entity_type"], kind == "DIRECT", need, [(s, e, need)]
                    )
                else:
                    ent.mentions.append((s, e, need))
                    # TAB: an entity masked inconsistently across its mentions needs masking.
                    ent.need_masking = ent.need_masking or need
            entities.extend(by_id.values())
        docs.append(Document(raw["doc_id"], text, list(raw["annotations"]), entities, safe))
    return docs


def mention_masked(text: str, masked: bytearray, start: int, end: int) -> bool:
    """TAB's rule: every character covered, ignoring punctuation, titles and function words."""
    uncovered = {i for i in range(start, end) if not masked[i]}
    if not uncovered:
        return True
    for m in _TOKEN.finditer(text, start, end):
        word = m.group(0).lower()
        # The possessive "'s" is a particle to TAB; a bare "S" is an initial and is not.
        possessive = word == "s" and m.start() > 0 and text[m.start() - 1] in "'" + chr(0x2019)
        # And a capital letter on its own is an initial ("Ms A."), not the article.
        initial = len(m.group(0)) == 1 and m.group(0).isupper()
        if word in TOKENS_TO_IGNORE or (word in FUNCTION_WORDS and not initial) or possessive:
            uncovered.difference_update(range(m.start(), m.end()))
    return all(text[i] in CHARACTERS_TO_IGNORE or text[i].isspace() for i in uncovered)


def _mask_array(n: int, spans: Sequence[tuple[int, int]]) -> bytearray:
    out = bytearray(n)
    for s, e in spans:
        out[s:e] = b"\x01" * (e - s)
    return out


@dataclass
class Counts:
    """Numerator and denominator per document, so the interval can resample documents."""

    hits: list[int] = field(default_factory=list)
    totals: list[int] = field(default_factory=list)

    def add(self, hits: int, total: int) -> None:
        self.hits.append(hits)
        self.totals.append(total)

    @property
    def value(self) -> float:
        t = sum(self.totals)
        return sum(self.hits) / t if t else 0.0

    def interval(self, *, n: int = 1000, seed: int = 20260923) -> tuple[float, float]:
        k = len(self.hits)
        if k == 0 or sum(self.totals) == 0:
            return (0.0, 0.0)
        rng = random.Random(seed)
        samples: list[float] = []
        for _ in range(n):
            idx = [rng.randrange(k) for _ in range(k)]
            t = sum(self.totals[i] for i in idx)
            samples.append(sum(self.hits[i] for i in idx) / t if t else 0.0)
        samples.sort()
        return samples[int(0.025 * n)], samples[int(0.975 * n) - 1]

    def __str__(self) -> str:
        lo, hi = self.interval()
        return f"{self.value:.1%} ({lo:.1%} to {hi:.1%})"


@dataclass
class TabResults:
    boundary_version: str
    ran_utc: str
    detector: str
    split: str
    documents: int
    characters: int
    direct: Counts
    quasi: Counts
    precision: Counts
    safe_touched: Counts
    chars_masked: Counts
    by_type: dict[str, Counts]
    ms_per_doc: list[float]

    def table(self) -> str:
        n_direct, n_quasi = sum(self.direct.totals), sum(self.quasi.totals)
        lines = [
            f"boundary {self.boundary_version}, TAB {self.split} split at {TAB_COMMIT[:12]}: "
            f"{self.documents} judgments, {self.characters:,} characters; {self.detector}",
            "",
            f"direct identifiers masked   {self.direct}   ({n_direct:,} annotator-entities)",
            f"quasi identifiers masked    {self.quasi}   ({n_quasi:,} annotator-entities)",
            f"precision (TAB, uniform)    {self.precision}",
            f"safe spans touched          {self.safe_touched}   (NO_MASK mentions with any "
            "masked word)",
            f"characters masked           {self.chars_masked}",
            f"time per judgment           {_median(self.ms_per_doc):.0f} ms median",
            "",
            "entities masked by type (direct and quasi together):",
        ]
        for kind, c in sorted(self.by_type.items(), key=lambda kv: -sum(kv[1].totals)):
            lines.append(f"  {kind:<10} {c!s:<26} of {sum(c.totals):,}")
        lines += [
            "",
            "Intervals: 95% bootstrap over judgments. Entity-level, micro-averaged over "
            "annotators, as TAB's evaluation.py scores it.",
        ]
        return "\n".join(lines)

    def readme_cells(self) -> str:
        return (
            f"| {self.detector} | {self.direct} | {self.quasi} | {self.precision} | "
            f"{self.safe_touched} |"
        )


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    return s[len(s) // 2] if s else 0.0


# The entity types a derived allow list may draw on. Never PERSON, so no name of anybody can
# reach the list whatever the annotators did; never MISC, because a cited case such as
# "Goodwin v. the United Kingdom" is MISC and carries a person's name inside it; and never
# CODE, DATETIME or QUANTITY, which the second pass does not mask by shape anyway.
ALLOW_TYPES = frozenset({"ORG", "LOC", "DEM"})


def derive_allow(
    path: Path, *, min_docs: int = 2, max_masked_share: float = 0.1, words: bool = True
) -> list[str]:
    """The terms a jurisdiction's allow list would hold, read off labelled data.

    Two kinds, both mechanical so that nobody chose a word after looking at a result, and
    both meant to be run on the train split only:

    - **Phrases** annotators left in clear: an organisation, place, demonym or other named
      thing marked NO_MASK in at least `min_docs` judgments, and masked by annotators in no
      more than `max_masked_share` of its mentions. "United Kingdom" as the respondent
      state is one; an applicant's home town is not.
    - **Words** (`words=True`): a capitalised word that appears in at least `min_docs`
      judgments and never inside any annotated mention at all. That is the
      domain's own vocabulary, `Article`, `Chamber`, `Registrar`, and the sentence openers
      the default vocabulary lacks, `However`, `According`.

    The defaults, two judgments and a tenth, were chosen on the dev split by a rule written
    before the test split was run: the highest precision whose direct recall stays within
    half a point, and quasi recall within three points, of no list (docs/redact.md).

    Neither PERSON nor MISC is a source, and a word that ever sits inside an annotated
    mention is excluded, so no name of anybody can reach the list whatever the annotators
    did. This stands in for what an operator writes for their own jurisdiction.
    """
    safe_docs: dict[str, set[str]] = {}
    safe_n: dict[str, int] = {}
    masked_n: dict[str, int] = {}
    word_docs: dict[str, set[str]] = {}
    tainted: set[str] = set()
    for raw in json.loads(path.read_text(encoding="utf-8")):
        text = raw["text"]
        blocked = bytearray(len(text))
        for ann in raw["annotations"].values():
            for m in ann["entity_mentions"]:
                phrase = " ".join(m["span_text"].split())
                key = phrase.casefold()
                if m["identifier_type"] in ("DIRECT", "QUASI"):
                    masked_n[key] = masked_n.get(key, 0) + 1
                elif m["entity_type"] in ALLOW_TYPES and any(c.isupper() for c in phrase):
                    safe_docs.setdefault(phrase, set()).add(raw["doc_id"])
                    safe_n[key] = safe_n.get(key, 0) + 1
                # Any annotated mention at all blocks its words from the word list, so a
                # surname inside a cited case is never mistaken for domain vocabulary.
                s, e = m["start_offset"], m["end_offset"]
                blocked[s:e] = b"\x01" * (e - s)
        if words:
            for w in _TOKEN.finditer(text):
                tok = w.group(0)
                if not (tok[0].isupper() and tok[0].isalpha()):
                    continue
                if any(blocked[w.start() : w.end()]):
                    tainted.add(tok)
                else:
                    word_docs.setdefault(tok, set()).add(raw["doc_id"])
    out = set()
    for phrase, docs in safe_docs.items():
        key = phrase.casefold()
        share = masked_n.get(key, 0) / (masked_n.get(key, 0) + safe_n[key])
        if len(docs) >= min_docs and share <= max_masked_share:
            out.add(phrase)
    for tok, docs in word_docs.items():
        if len(docs) >= min_docs and tok not in tainted and len(tok) > 1:
            out.add(tok)
    return sorted(out)


def read_allow(path: Path) -> list[str]:
    """One term or phrase per line; blank lines and lines starting with # are skipped."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]


def run(
    docs: Sequence[Document],
    *,
    extra: Sequence[Recogniser] = (),
    detector: str = "built-in recognisers only",
    split: str = "test",
    allow: Sequence[str] = (),
) -> TabResults:
    """Redact each judgment as one document and score it against every annotator.

    `allow` is passed to the policy as a caller's own terms would be: what a deployment in
    this jurisdiction adds to the default vocabulary.
    """
    from boundary import __version__

    analyzer = Analyzer(extra=extra)
    direct, quasi, precision, safe_touched, chars = Counts(), Counts(), Counts(), Counts(), Counts()
    by_type: dict[str, Counts] = {}
    ms: list[float] = []
    for doc in docs:
        t0 = time.perf_counter()
        spans = sweep([doc.text], analyzer.analyze_page(doc.text, 1))
        _, masked_spans = Policy(spans, allow=allow).redact_with_spans(doc.text)
        ms.append((time.perf_counter() - t0) * 1000)
        masked = _mask_array(len(doc.text), masked_spans)
        chars.add(sum(masked), len(doc.text))

        d_hit = d_tot = q_hit = q_tot = 0
        type_counts: dict[str, list[int]] = {}
        for ent in doc.entities:
            if not ent.need_masking:
                continue
            ok = all(
                mention_masked(doc.text, masked, s, e) or not need for s, e, need in ent.mentions
            )
            if ent.is_direct:
                d_tot += 1
                d_hit += ok
            else:
                q_tot += 1
                q_hit += ok
            tc = type_counts.setdefault(ent.entity_type, [0, 0])
            tc[0] += ok
            tc[1] += 1
        direct.add(d_hit, d_tot)
        quasi.add(q_hit, q_tot)
        for kind in {e.entity_type for e in doc.entities} | set(by_type):
            h, t = type_counts.get(kind, [0, 0])
            by_type.setdefault(kind, Counts()).add(h, t)

        touched = sum(
            1
            for _, s, e in doc.safe
            if any(masked[m.start() : m.end()].count(1) for m in _TOKEN.finditer(doc.text, s, e))
        )
        safe_touched.add(touched, len(doc.safe))

        # TAB's token precision, uniform weights: each masked token scores the number of
        # annotators who masked all of it, out of the number of annotators.
        covers = {
            a: _mask_array(
                len(doc.text),
                [
                    (s, e)
                    for ent in doc.entities
                    if ent.annotator == a
                    for s, e, need in ent.mentions
                    if need
                ],
            )
            for a in doc.annotators
        }
        tp = total = 0
        for s, e in masked_spans:
            for m in _TOKEN.finditer(doc.text, s, e):
                total += len(doc.annotators)
                tp += sum(1 for a in doc.annotators if all(covers[a][m.start() : m.end()]))
        precision.add(tp, total)

    # A type first seen late has no entries for the documents before it; pad them with
    # zeros so every Counts resamples the same documents.
    for c in by_type.values():
        pad = len(docs) - len(c.hits)
        c.hits[:0] = [0] * pad
        c.totals[:0] = [0] * pad
    return TabResults(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        detector=detector,
        split=split,
        documents=len(docs),
        characters=sum(len(d.text) for d in docs),
        direct=direct,
        quasi=quasi,
        precision=precision,
        safe_touched=safe_touched,
        chars_masked=chars,
        by_type=by_type,
        ms_per_doc=ms,
    )


def to_json(results: TabResults) -> str:
    def cell(c: Counts) -> dict[str, object]:
        lo, hi = c.interval()
        return {"hits": sum(c.hits), "total": sum(c.totals), "value": c.value, "ci": [lo, hi]}

    return json.dumps(
        {
            "boundary_version": results.boundary_version,
            "ran_utc": results.ran_utc,
            "corpus": f"TAB {results.split} split at {TAB_COMMIT}",
            "detector": results.detector,
            "documents": results.documents,
            "characters": results.characters,
            "direct": cell(results.direct),
            "quasi": cell(results.quasi),
            "precision": cell(results.precision),
            "safe_touched": cell(results.safe_touched),
            "chars_masked": cell(results.chars_masked),
            "by_type": {k: cell(v) for k, v in sorted(results.by_type.items())},
            "ms_per_doc_median": _median(results.ms_per_doc),
        },
        indent=2,
    )


def write_readme(readme: Path, rows: Sequence[str]) -> None:
    text = readme.read_text(encoding="utf-8")
    start = text.index(README_START)
    end = text.index(README_END)
    block = "\n".join(rows)
    readme.write_text(
        text[: start + len(README_START)] + "\n" + block + "\n" + text[end:], encoding="utf-8"
    )


__all__ = [
    "SPLITS",
    "TAB_COMMIT",
    "Counts",
    "Document",
    "Entity",
    "TabResults",
    "derive_allow",
    "fetch",
    "load",
    "mention_masked",
    "read_allow",
    "run",
    "to_json",
    "write_readme",
]
