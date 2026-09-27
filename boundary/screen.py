"""Prompt-injection screening, advisory by default (0.22, PLAN.md B2.6).

A rules screen over what a caller sends: patterns for the moves an injection makes, not for
its topic. It never reads the system prompt, which is the operator's own instruction channel;
it reads the user messages, which is where a user's text and any retrieved document arrive.

Seven kinds of move, each a named rule so that a flag says which one fired:

| Rule | The move |
|---|---|
| `override` | Tell the model to ignore, forget or disregard what it was told before |
| `pivot` | Announce that the old task is over and a new one follows |
| `exfiltrate` | Ask for the prompt, the instructions or what was written above |
| `persona` | Reassign the model's identity: "you are now", "pretend to be", "act as" |
| `delimiter` | A run of escaped or blank lines, or a fence, used to fake the end of a document |
| `canary` | A demand to output one fixed word or phrase and nothing else |
| `obfuscation` | Letters spaced apart so that no word pattern matches them |

English and German, because the public set it was developed on is both; French patterns
are written in for a Canadian deployment and are **not measured**, which docs/screen.md
says wherever the rates are printed.

**How it was developed**, which decides what its figures mean. The rules were written and
revised on the **train** split of deepset/prompt-injections only. Its test split and project
03's red-team suites were read once, after the rules were frozen. A rule changed after
reading those would make them development sets, and the figures would say so.

A rules screen is the floor, not the ceiling: a paraphrase it has no pattern for passes.
It is here because it costs microseconds and no model, and because the false-positive rate
is the number the policy owner needs before choosing to block (B2.6).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from boundary.types import ChatRequest

_I = re.IGNORECASE | re.UNICODE

# What an override tells the model to drop.
_PRIOR_EN = (
    r"(?:all\s+|any\s+|the\s+|your\s+|of\s+the\s+)*"
    r"(?:previous|prior|above|preceding|earlier|foregoing|former|original|initial|system)"
    r"(?:\s+\w+){0,2}?\s+"
    r"(?:instructions?|tasks?|assignments?|information|orders?|commands?|directions?|rules?|"
    r"prompts?|texts?|guidelines?|messages?|context|input|conversation)"
)
_PRIOR_DE = (
    r"(?:alle\s+|die\s+|deine\s+|jegliche\s+)*"
    r"(?:vorherigen|bisherigen|vorangehenden|obigen|vorigen|früheren|ursprünglichen)"
    r"(?:\s+\w+){0,2}?\s+"
    r"(?:anweisungen|aufgaben|aufträge|angaben|informationen|befehle|regeln|texte|ausführungen)"
)

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override",
        re.compile(
            r"\b(?:ignore|disregard|forget|skip|drop|abandon|override|overlook|leave\s+behind)\b"
            rf"\s+(?:{_PRIOR_EN}|(?:about\s+)?(?:all|everything)\b"
            r"(?:\s+(?:before|above|previous(?:ly)?|prior|that|we\s+discussed|else))?)",
            _I,
        ),
    ),
    (
        "override",
        re.compile(
            r"\b(?:ignore|disregard)\s+(?:the\s+)?above\b|\bleave\s+all\s+the\s+previous\b", _I
        ),
    ),
    (
        "override",
        re.compile(
            r"\b(?:ignorier\w*|vergiss|vergessen\s+sie|missachte\w*|lass\w*)\b(?:\s+sie)?"
            rf"\s+(?:{_PRIOR_DE}|(?:alles|alle)\b(?:\s+(?:davor|zuvor|vorher|bisherige))?)",
            _I,
        ),
    ),
    (
        "override",
        re.compile(r"\b(?:die\s+)?obigen\s+ausführungen\s+ignorieren\b", _I),
    ),
    (
        "override",
        re.compile(
            r"\b(?:ignore[zr]?|oublie[zr]?)\s+(?:toutes?\s+)?(?:les\s+)?"
            r"(?:instructions?|consignes?|directives?)\s+(?:précédentes?|ci-dessus|antérieures?)",
            _I,
        ),
    ),
    (
        "override",
        re.compile(
            # Instructions dropped without saying which: "forget all instructions", in the
            # languages the train split carries them in (English, French, Spanish, Russian,
            # Croatian), and the variants that drop the task by other words.
            r"\b(?:ignore|forget|disregard)\s+(?:all\s+)?(?:the\s+|your\s+)?instructions\b|"
            r"\b(?:oubliez|oublie)\s+toutes?\s+les\s+instructions\b|"
            r"\bolvid(?:a|e|ar)\s+(?:todo|todas\s+las\s+instrucciones)\b|"
            r"\bзабудь(?:те)?\s+вс[её]\b|\bzaboravi\s+sve\b|"  # noqa: RUF001  Russian, on purpose
            r"\bdespite\s+what\s+you(?:'ve|\s+have)\s+been\s+told\b|"
            r"\bdisregarding\s+the\s+(?:articles|context|documents?|instructions)\b|"
            r"\bnot\s+(?:by|from)\s+the\s+(?:articles|context|documents?)\b|"
            r"\bnot\s+to\s+remember\s+the\s+things\b|"
            r"\bnicht\s+an\s+die\s+dinge\s+zu\s+erinnern\b|"
            r"\babweichend\s+(?:zu|von)\s+(?:den\s+)?vorherigen\b|"
            r"\b(?:alle\s+)?(?:vorherigen|bisherigen)\s+(?:informationen|anweisungen)\b.{0,60}"
            r"\b(?:irrelevant|unwichtig|nicht\s+ganz)\b|"
            r"\byour\s+(?:new\s+)?instructions\s+are\s+now\b",
            _I,
        ),
    ),
    (
        "pivot",
        re.compile(
            r"\b(?:now|nun|jetzt)\b.{0,50}\b(?:new|another|further|weitere[nr]?|neue[nr]?)\s+"
            r"(?:task|challenge|aufgabe|herausforderung)\b|"
            r"\b(?:stop|stopp)\s*[:\-]\s*\w|\battention\s*-\s*stop\b|"
            r"\b(?:additional|new)\s+instructions?\b|^\s*instruction\s*:|\\n\s*instruction\s*:|"
            r"\bdas\s+ist\s+erledigt\b",
            _I | re.MULTILINE,
        ),
    ),
    (
        "pivot",
        re.compile(
            r"\b(?:now\s+)?(?:focus|concentrate)\s+on\s+(?:your|the|a)\s+new\s+task\b|"
            r"\bnew\s+(?:tasks?|instructions?|assignments?)\s+(?:follow|are\s+followed|now)\b|"
            r"\bstart\s+(?:over|again|from\s+the\s+front)\b.{0,40}\bnew\s+task\b|"
            r"\bthat\s+is\s+(?:enough|done)\.\s+now\b|"
            r"\bremove\s+all\s+previous\s+tasks\b",
            _I,
        ),
    ),
    (
        "pivot",
        re.compile(
            r"\bneue(?:n|r)?\s+aufgabe\b|\bnun\s+folgen\s+(?:neue|weitere)\s+aufgaben\b|"
            r"\bdas\s+(?:genügt|reicht)\.",
            _I,
        ),
    ),
    (
        "exfiltrate",
        re.compile(
            r"\b(?:show|print|reveal|output|repeat|display|give|tell|list|spell\s+check|"
            r"copy|write\s+out)\b(?:\s+\w+){0,4}?\s+"
            r"(?:(?:your|the|all|full|entire|initial|original|previous|above|system|hidden)\s+)+"
            r"(?:prompts?|prompt\s+texts?|instructions?|system\s+messages?|configuration)\b|"
            r"\bwhat\s+(?:was|is)\s+written\s+(?:at\s+the\s+beginning|above|before)\b",
            _I,
        ),
    ),
    (
        "exfiltrate",
        re.compile(
            r"\b(?:zeige?|gib|drucke|wiederhole)\b(?:\s+\w+){0,4}?\s+"
            r"(?:(?:deine[nm]?|den|die|alle|gesamten|vollständigen|ursprünglichen)\s+)+"
            r"(?:prompt(?:-?texte?)?|anweisungen|systemnachricht)\b|"
            r"\bwas\s+(?:stand|steht)\s+(?:am\s+anfang|oben)\b",
            _I,
        ),
    ),
    (
        "persona",
        re.compile(
            r"\b(?:you\s+are\s+now|from\s+now\s+on,?\s+you\s+(?:are|will)|"
            r"pretend\s+(?:you\s+are|to\s+be)|i\s+want\s+you\s+to\s+act\s+as|"
            r"act\s+as\s+(?:if\s+you\s+(?:are|were)|an?\s+(?:evil|unrestricted|unfiltered))|"
            r"now\s+you\s+are\s+\w+|you\s+are\s+completely\s+absorbed\s+in\s+your\s+role|"
            r"stay\s+in\s+(?:their|your)\s+roles?)\b",
            _I,
        ),
    ),
    (
        "persona",
        re.compile(
            r"\b(?:jetzt\s+bist\s+du|du\s+bist\s+jetzt|stell\s+dir\s+vor,?\s+du\s+bist|"
            r"ich\s+möchte,?\s+dass\s+(?:sie|du)\s+als\s+\S+\s+(?:fungieren|agierst|handelst))\b",
            _I,
        ),
    ),
    (
        "persona",
        re.compile(
            r"(?:^|[.!?]\s+)(?:you\s+are|du\s+bist|nun\s+bist\s+du|imagine\s+you\s+are|"
            r"pretend\s+you)\s+(?:the\s+|an?\s+|three\s+)?\w+|"
            r"\bact\s+as\s+an?\b|\brole-?playing\s+as\b|\bim\s+stile?\s+eine[sr]?\b|"
            r"\bin\s+the\s+style\s+of\b|\bstell\s+dir\s+vor\b",
            _I | re.MULTILINE,
        ),
    ),
    (
        "exfiltrate",
        re.compile(
            r"\b(?:sentences?|lines?|words?)\s+before\s+the\s+text\b|"
            r"\binformation\s+in\s+the\s+prompt\b",
            _I,
        ),
    ),
    (
        "obfuscation",
        # Letters spaced out to slip past a word match: "S a y t h a t".
        re.compile(r"\b(?:[^\W\d_]\s){6,}[^\W\d_]\b", re.UNICODE),
    ),
    (
        "delimiter",
        re.compile(r"\$documents?\s*:|\bcontext\s*:\s*\{", _I),
    ),
    (
        "delimiter",
        re.compile(r"(?:\\\s?n\s*){2,}|(?:\n\s*){6,}|^-{5,}\s*$|={5,}|#{5,}", _I | re.MULTILINE),
    ),
    (
        "canary",
        re.compile(
            r"\b(?:say|output|print|respond\s+with|reply\s+with|answer\s+with|write)\s+"
            r"(?:only\s+)?[\"'“][^\"'”]{1,40}[\"'”](?:\s+and\s+nothing\s+else|\s+only)?|"
            r"\bhaha\s+pwned\b",
            _I,
        ),
    ),
)

RULE_NAMES: tuple[str, ...] = tuple(dict.fromkeys(name for name, _ in RULES))


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule firing on one span. Carries offsets, never the matched text, so a finding
    can go into a log or a header without carrying what the caller wrote."""

    rule: str
    message: int
    start: int
    end: int


def screen_text(text: str, *, message: int = 0) -> list[Finding]:
    return [
        Finding(name, message, m.start(), m.end())
        for name, pattern in RULES
        for m in pattern.finditer(text)
    ]


def screen_messages(messages: Iterable[str]) -> list[Finding]:
    return [f for i, text in enumerate(messages) for f in screen_text(text, message=i)]


def screen_request(request: ChatRequest) -> list[Finding]:
    """The user messages of `request`, where a user's text and retrieved content arrive.
    Never the system prompt: it is the operator's, and an operator's instructions are
    injection-shaped by nature."""
    return screen_messages(
        str(m["content"]) for m in request.messages if str(m.get("role")) == "user"
    )


def rules_fired(findings: Iterable[Finding]) -> list[str]:
    return sorted({f.rule for f in findings}, key=RULE_NAMES.index)


__all__ = [
    "RULES",
    "RULE_NAMES",
    "Finding",
    "rules_fired",
    "screen_messages",
    "screen_request",
    "screen_text",
]
