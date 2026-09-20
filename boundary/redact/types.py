"""The shapes `boundary.redact` speaks in. Written to project 07's specification (PLAN.md
section B2.3, amended 2026-09-19), which is a specification from use rather than a guess.

A `Span` is one detected entity on one page. Its offsets are into that page's text, never
document-global, because a redaction workflow walks pages and a global offset is the one
number every consumer has to convert before using. `[start, end)` is half-open so that
`page_text[start:end] == text` holds, and the constructor checks it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class EntityType(StrEnum):
    """The closed vocabulary of what a span can be. Closed on purpose: a consumer switches on
    these, and a type it has never seen is a decision it cannot make. Adding one is a
    version bump and a line in docs/redact.md."""

    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    # Social Insurance Number, checksum-validated.
    SIN = "SIN"
    # A provincial health number. The Newfoundland and Labrador Medical Care Plan number is
    # the one recognised today; the type is provincial so that others slot in beside it.
    HEALTH_NUMBER = "HEALTH_NUMBER"
    POSTAL_CODE = "POSTAL_CODE"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    # A departmental file, case, claim or request number: the identifier that ties a record
    # to a person without naming them.
    FILE_NUMBER = "FILE_NUMBER"
    EMPLOYEE_ID = "EMPLOYEE_ID"
    # A street address (0.5.1). No built-in recogniser yet; project 07 brings its own, and
    # the type exists so that its span outranks a LOCATION for the street inside it.
    ADDRESS = "ADDRESS"
    LOCATION = "LOCATION"
    ORGANISATION = "ORGANISATION"
    URL = "URL"
    # The policy's second pass (policy.py): something shaped like a name or an identifier
    # that no recogniser claimed. Masked because a boundary built on detections inherits
    # every miss, and reported under its own type so that the two passes stay separable.
    NAME_LIKE = "NAME_LIKE"
    ID_LIKE = "ID_LIKE"


@dataclass(frozen=True, slots=True, order=True)
class Span:
    """One detected entity on one page.

    page: the page number as the caller numbers pages. Offsets are into that page's text.
    start, end: half-open character offsets, so page_text[start:end] == text.
    text: the matched substring, exactly as it appears.
    entity_type: from the closed vocabulary above.
    score: the recogniser's confidence in [0, 1].
    recogniser: which recogniser fired ("boundary:email", "presidio:SpacyRecognizer").
        07 publishes an error decomposition that splits leaks into "the detector never
        found it" against "the decision was wrong", and cannot attribute a detector miss
        without this.
    """

    page: int
    start: int
    end: int
    text: str
    entity_type: EntityType
    score: float
    recogniser: str

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError(
                f"span offsets must satisfy 0 <= start < end; got {self.start}, {self.end}"
            )
        if len(self.text) != self.end - self.start:
            raise ValueError(
                f"span text has {len(self.text)} characters but [start, end) spans "
                f"{self.end - self.start}; the offsets and the text disagree"
            )
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be in [0, 1]; got {self.score}")
        if not self.recogniser:
            raise ValueError("a span must name the recogniser that produced it")

    @property
    def length(self) -> int:
        return self.end - self.start

    def overlaps(self, other: Span) -> bool:
        return self.page == other.page and self.start < other.end and other.start < self.end


@runtime_checkable
class Recogniser(Protocol):
    """Anything that finds spans in one page of text. Built-in recognisers, the Presidio
    adapter, and whatever a project brings all look the same to the analyzer."""

    @property
    def id(self) -> str: ...

    def analyze(self, text: str, page: int) -> Sequence[Span]: ...
