"""Redaction in the proxy: outbound before the call, rehydration on the way back (0.15).

PLAN.md B2.3, the proxy half. A request whose class's policy rule names a `redacted_as` is
redacted here, as one document: the system prompt and every message together, so a person
named in turn one and again in turn four is one placeholder (07's finding 1). The guard is
`Policy.outbound`, which refuses rather than warns: if anything name- or identifier-shaped
survives, nothing is sent. Only then is the call handed to the gateway with `redacted=True`,
and judged by the `redacted_as` class.

The system prompt gains `PRESERVE_LINE`. 0.14.0 measured a line like it cutting placeholder
mutation from 37.0% to 0.6% for Llama 3.3 70B and from 6.8% to 0.0% for Haiku, and also
found Llama copying the measured line's example placeholder into its answers. This line
therefore carries no example. It has not been measured in this wording yet (docs/redact.md).

The vault is the request's `Policy`, in memory for the life of the request, and never
written anywhere: not the ledger, not a span, not a log. The Redis vault under a per-team
key in B2.3 is for a proxy with more than one process, which this is not yet.

Detection is the built-in recognisers plus the policy's second pass, which masks anything
name-shaped a recogniser did not claim, so names become `<NAME_LIKE_n>` rather than
`<PERSON_n>`. That is the fail-closed direction, and it over-masks: on real text it touches
most spans annotators marked safe (docs/redact.md). A detector and an allow list are the
remedies, and neither is configured here yet.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from dataclasses import dataclass

from boundary.redact.analyzer import Analyzer
from boundary.redact.policy import Policy, RedactionRefused
from boundary.redact.sweep import sweep
from boundary.types import ChatRequest

PRESERVE_LINE = (
    "The text contains placeholders in angle brackets, each standing in for a personal "
    "detail that was removed before the text reached you. Copy every placeholder you use "
    "exactly, character for character, angle brackets included. Never invent, renumber, "
    "merge or replace one, and do not guess what it stands for."
)


@dataclass(frozen=True, slots=True)
class Redacted:
    request: ChatRequest
    policy: Policy
    placeholders: int


def redact_request(request: ChatRequest) -> Redacted:
    """The request with every message and the system prompt redacted by one policy, and the
    preserve line added. Raises `RedactionRefused` when the guard will not vouch for any part
    of it; the error carries counts by kind and type, never the values."""
    texts = [request.system or ""] + [str(m["content"]) for m in request.messages]
    policy = Policy(sweep(texts, Analyzer().analyze(texts)))
    out = [policy.outbound(t) for t in texts]
    system = out[0] + "\n\n" + PRESERVE_LINE if request.system else PRESERVE_LINE
    messages = [
        {"role": str(m["role"]), "content": text}
        for m, text in zip(request.messages, out[1:], strict=True)
    ]
    redacted = dataclasses.replace(request, system=system, messages=messages)
    return Redacted(redacted, policy, len(policy.vault))


def leak_counts(error: RedactionRefused) -> dict[str, int]:
    """What the guard found, as counts by kind and type: safe to return and to log."""
    return dict(Counter(f"{leak.kind}:{leak.entity_type.value}" for leak in error.leaks))


# The characters a placeholder's bare form and index are made of. A stream is never cut
# inside a run of them, because the next piece might complete `NAME_LIKE_1` or `_1.2`.
_TAIL = re.compile(r"[A-Za-z0-9_.\-]*\Z")
# How far back an unclosed `<` holds the stream. A placeholder with spaces inside its
# brackets is still far shorter; past this, the `<` was ordinary text.
HOLD = 64


class StreamRehydrator:
    """Rehydrates a streamed answer piece by piece.

    A placeholder can arrive split across pieces (`<NAME_` then `LIKE_1>`), and rehydrating
    each piece alone would miss it. So text is released only up to a point no placeholder
    can straddle: before an unclosed `<` (for up to `HOLD` characters), and before a trailing
    run of the characters a bare placeholder is made of. The rest waits for the next piece or
    for `flush`. The property that matters, and that a test checks over random texts and
    random cuts: everything released, joined, is exactly `policy.rehydrate` of the whole.
    """

    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._held = ""

    @staticmethod
    def _cut(text: str) -> int:
        cut = len(text)
        lt = text.rfind("<")
        if lt != -1 and ">" not in text[lt:] and len(text) - lt <= HOLD:
            cut = lt
        tail = _TAIL.search(text, 0, cut)
        return tail.start() if tail is not None else cut

    def feed(self, piece: str) -> str:
        text = self._held + piece
        cut = self._cut(text)
        self._held = text[cut:]
        return self._policy.rehydrate(text[:cut])

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._policy.rehydrate(text)


__all__ = [
    "HOLD",
    "PRESERVE_LINE",
    "Redacted",
    "RedactionRefused",
    "StreamRehydrator",
    "leak_counts",
    "redact_request",
]
