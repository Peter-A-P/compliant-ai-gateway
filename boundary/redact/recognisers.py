"""Built-in recognisers: the ones stock Presidio lacks and access-to-information records
need, plus an email recogniser that does not miss a government address.

Every one is a regular expression with an optional validator, needs no model, no download
and no network, and is exercised by a golden in tests/test_redact.py. Each carries its own
id so that a span says which one fired.

The email recogniser exists because of a specific miss. Presidio classified only the
`gov.nl.ca` inside `aaronpenashue@gov.nl.ca.example` and called it a URL. Multi-label
government domains are exactly what these records contain, and a missed work address is a
leak. This one takes any number of labels.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from boundary.redact.types import EntityType, Span

# A validator sees the match and returns the score to report, or None to reject the match.
Validator = Callable[[re.Match[str]], float | None]


@dataclass(frozen=True, slots=True)
class RegexRecogniser:
    """A recogniser defined by a pattern. `group` is the capture group that is the span,
    which lets a labelled pattern ("date of birth: 1971-04-02") report only the value."""

    id: str
    entity_type: EntityType
    pattern: re.Pattern[str]
    score: float
    group: int = 0
    validate: Validator | None = None

    def analyze(self, text: str, page: int) -> Sequence[Span]:
        out: list[Span] = []
        for m in self.pattern.finditer(text):
            score = self.score
            if self.validate is not None:
                validated = self.validate(m)
                if validated is None:
                    continue
                score = validated
            start, end = m.span(self.group)
            if end <= start:
                continue
            out.append(
                Span(
                    page=page,
                    start=start,
                    end=end,
                    text=text[start:end],
                    entity_type=self.entity_type,
                    score=score,
                    recogniser=self.id,
                )
            )
        return out


def luhn_ok(digits: str) -> bool:
    """The checksum a Social Insurance Number carries."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _sin(m: re.Match[str]) -> float | None:
    digits = "".join(m.groups())
    # A run of nine digits that fails the checksum is not reported as a SIN. The policy's
    # second pass still masks it as an identifier, so nothing is lost by being exact here.
    return 0.95 if luhn_ok(digits) else None


_MCP_LABEL = re.compile(r"(?:\bMCP\b|medical care plan|health card|health number)", re.I)


def _mcp(m: re.Match[str]) -> float | None:
    """Twelve digits is the Newfoundland and Labrador Medical Care Plan format. Labelled
    within a few words it is near-certain; bare, it is still more likely a health number
    than anything else twelve digits long in these records."""
    window = m.string[max(0, m.start() - 40) : m.start()]
    return 0.95 if _MCP_LABEL.search(window) else 0.6


# An address, with the letters written in any script. `[^\W_]` is a word character that is
# not an underscore, which on a str pattern is any Unicode letter or digit.
#
# It was `[A-Za-z0-9._%+-]` until 0.6.0, and the evaluation harness found the leak on its
# first run: `therese.gagne@example.gov.nl.ca` spelled with its accents matched nothing
# here, and the policy's second pass does not mask it either, because an address carrying
# no digit is not identifier-shaped and a lowercase word is not name-shaped. So it left in
# clear with no refusal. Internationalised addresses are ordinary (RFC 6531), and in a
# province with French, Innu and Mi'kmaq names a government address carrying an accent is
# not an edge case. Same class of defect as the accented-name leak fixed in 0.5.2, in a
# different layer, found by measuring rather than by being told.
_LOCAL = r"[^\W_](?:[^\W_]|[._%+\-])*"
_LABEL = r"[^\W_](?:[^\W_]|-)*"

EMAIL = RegexRecogniser(
    id="boundary:email",
    entity_type=EntityType.EMAIL,
    pattern=re.compile(_LOCAL + r"@" + _LABEL + r"(?:\." + _LABEL + r")+"),
    score=0.95,
)

CA_POSTAL_CODE = RegexRecogniser(
    id="boundary:ca-postal-code",
    entity_type=EntityType.POSTAL_CODE,
    # The letters Canada Post does not use in each position are excluded, which is what
    # keeps this from firing on ordinary words such as "A1B 2C3"-shaped product codes only
    # rarely and on prose never.
    pattern=re.compile(
        r"(?<![A-Za-z0-9])[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z] ?\d[ABCEGHJ-NPRSTV-Z]\d(?![A-Za-z0-9])",
        re.I,
    ),
    score=0.85,
)

SIN = RegexRecogniser(
    id="boundary:sin",
    entity_type=EntityType.SIN,
    pattern=re.compile(r"(?<!\d)(\d{3})[ -]?(\d{3})[ -]?(\d{3})(?!\d)"),
    score=0.95,
    validate=_sin,
)

NL_MCP = RegexRecogniser(
    id="boundary:nl-mcp",
    entity_type=EntityType.HEALTH_NUMBER,
    # Four groups of three, contiguous or separated by a space or a hyphen. On a form the
    # number is written "123 456 789 012", and 0.5.0 wanted twelve contiguous digits: on
    # 07's corpus that let 57 of 210 pages leave with a health number in them.
    pattern=re.compile(r"(?<!\d)\d{3}(?:[ -]?\d{3}){3}(?![ -]?\d)"),
    score=0.6,
    validate=_mcp,
)

PHONE = RegexRecogniser(
    id="boundary:phone-na",
    entity_type=EntityType.PHONE,
    pattern=re.compile(r"(?<![\d-])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}(?![\d-])"),
    score=0.8,
)

# The date shapes a labelled date of birth takes in these records: 1971-04-02, 02/04/1971,
# April 2, 1971, 2 April 1971, Apr. 2 1971.
_DATE = (
    r"\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
)

DATE_OF_BIRTH = RegexRecogniser(
    id="boundary:date-of-birth",
    entity_type=EntityType.DATE_OF_BIRTH,
    pattern=re.compile(
        r"(?:date\s+of\s+birth|birth\s?date|d\.?o\.?b\.?|born(?:\s+on)?)\s*[:\-]?\s*("
        + _DATE
        + r")",
        re.I,
    ),
    score=0.9,
    group=1,
)

# An identifier is a run of letters, digits, slashes and hyphens that contains at least one
# digit. The lookahead demands the digit; the label demands the context.
_IDENTIFIER = r"((?=[A-Za-z0-9/-]*\d)[A-Za-z0-9][A-Za-z0-9/-]{3,})"

FILE_NUMBER = RegexRecogniser(
    id="boundary:file-number",
    entity_type=EntityType.FILE_NUMBER,
    pattern=re.compile(
        r"\b(?:file|case|reference|ref|matter|claim|docket|request|ticket)\s*"
        r"(?:no\.?|number|num\.?|#|id)?\s*[:\-]?\s*" + _IDENTIFIER,
        re.I,
    ),
    score=0.85,
    group=1,
)

EMPLOYEE_ID = RegexRecogniser(
    id="boundary:employee-id",
    entity_type=EntityType.EMPLOYEE_ID,
    pattern=re.compile(
        r"\b(?:employee|emp\.?|staff|payroll|personnel)\s*(?:id|no\.?|number|num\.?|#)\s*[:\-]?\s*"
        + _IDENTIFIER,
        re.I,
    ),
    score=0.85,
    group=1,
)

# The order matters only for ties in the analyzer's overlap resolution, where an earlier
# recogniser wins. The specific labelled ones come before the bare numeric ones.
DEFAULT_RECOGNISERS: tuple[RegexRecogniser, ...] = (
    EMAIL,
    DATE_OF_BIRTH,
    FILE_NUMBER,
    EMPLOYEE_ID,
    SIN,
    NL_MCP,
    PHONE,
    CA_POSTAL_CODE,
)

__all__ = [
    "CA_POSTAL_CODE",
    "DATE_OF_BIRTH",
    "DEFAULT_RECOGNISERS",
    "EMAIL",
    "EMPLOYEE_ID",
    "FILE_NUMBER",
    "NL_MCP",
    "PHONE",
    "SIN",
    "RegexRecogniser",
    "luhn_ok",
]
