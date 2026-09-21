"""A small labelled corpus, built here rather than downloaded, so this repository has a
redaction number of its own.

Every figure `boundary.redact` has published so far belongs to project 07. That was honest
but it is not the rule this portfolio is built on, which is that each project ends with a
number a stranger can check by running one command against this repository. So: a generator
that plants known values in prose written to contain none, and therefore knows both what
should be found and what should not.

**What this corpus is.** Synthetic, deterministic from a seed, and shipped as code rather
than data. Values are drawn from pools: forenames and surnames paired at random, a valid
SIN by checksum, a Newfoundland and Labrador MCP number, postcodes, phone numbers, file and
employee identifiers. Surnames are ordinary surnames of this province paired with unrelated
forenames, so no page describes a real person, and every identifier is generated and
belongs to nobody.

**What it is not.** It is not a sample of real records, so every value is well formed and
sits in a sentence somebody wrote for it. A recall figure measured here is an upper bound
on the same figure over real documents, exactly as project 07's is, and the docs say so
next to the number. What it adds to 07's corpus is the other half: prose written to hold
**no** personal values, so a span the engine returns outside a label is a false positive and
can be counted. That is what makes a precision figure possible at all.

**The name pool is the part that matters.** Five orthographic shapes, in the proportions
that broke this library rather than in equal ones: plain ASCII, accented, internal capital
(`MacDonald`, `LeBlanc`), apostrophe (`O'Brien`) and hyphenated (`Jean-Pierre`). Four of
those were invisible to the second pass until 0.5.2, and 07's corpus contains none of them,
which is why its 96.3% could not see the leak.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from boundary.redact.types import EntityType

FORENAMES_PLAIN = (
    "Marie",
    "Wallace",
    "Bernadette",
    "Gerald",
    "Sharon",
    "Patrick",
    "Doreen",
    "Lloyd",
    "Kimberly",
    "Roland",
    "Beverley",
    "Trevor",
)
FORENAMES_ACCENTED = (
    "Émile",
    "Renée",
    "André",
    "Noëlle",
    "Thérèse",
)
SURNAMES_PLAIN = (
    "Chaulk",
    "Penashue",
    "Tuglavina",
    "Hearn",
    "Snook",
    "Pardy",
    "Whiffen",
    "Rideout",
    "Hynes",
    "Bursey",
    "Noseworthy",
    "Squires",
)
SURNAMES_ACCENTED = ("Gagné", "Bérubé", "Thériault", "Légaré")
SURNAMES_INTERNAL_CAPITAL = ("MacDonald", "McCarthy", "LeBlanc", "DeSouza", "MacIsaac", "LeDrew")
SURNAMES_APOSTROPHE = ("O" + chr(39) + "Brien", "O" + chr(39) + "Keefe", "D" + chr(39) + "Arcy")
FORENAMES_HYPHENATED = ("Jean-Pierre", "Anne-Marie", "Marie-Claude")

# The shape mix. Weighted towards what breaks a shape-based pass rather than towards what a
# telephone directory holds: a corpus of plain ASCII names measures the easy half twice.
_SHAPES: tuple[tuple[str, int], ...] = (
    ("plain", 40),
    ("accented", 15),
    ("internal", 20),
    ("apostrophe", 15),
    ("hyphenated", 10),
)

TOWNS = ("Corner Brook", "Happy Valley-Goose Bay", "Gander", "Labrador City")
DEPARTMENTS = (
    "Department of Health and Community Services",
    "Department of Transportation and Infrastructure",
    "Office of the Information and Privacy Commissioner",
)

# Text that looks like an entity and is not one. A precision figure measured on a corpus
# with no distractors in it measures nothing, because everything shaped like a value would
# be a value.
DISTRACTORS = (
    "section 31(1)",
    "the 2024 budget",
    "Q1 2024",
    "page 12 of 40",
    "$1,250.00",
    "2026-03-14",
    "the third request this year",
    "Schedule A",
    "file format PDF",
    "40 percent",
)


@dataclass(frozen=True, slots=True)
class Label:
    """One planted value: where it is and what it is. The ground truth."""

    page: int
    start: int
    end: int
    text: str
    entity_type: EntityType
    shape: str = ""
    # Whether releasing this value in clear would be a leak. A town and a department are
    # entities and are labelled as such, because a detector that finds them is right and a
    # precision figure that called them false positives would be measuring the corpus
    # rather than the engine. They are not personal, so they are not in the leak rate.
    personal: bool = True

    def __post_init__(self) -> None:
        # The same check `Span` makes, for the same reason and with more at stake: a span
        # that lies about its offsets is one bad detection, and a label that lies about its
        # offsets is every number measured against it.
        if self.start < 0 or self.end <= self.start:
            raise ValueError(
                f"label offsets must satisfy 0 <= start < end; got {self.start}, {self.end}"
            )
        if len(self.text) != self.end - self.start:
            raise ValueError(
                f"label text has {len(self.text)} characters but [start, end) spans "
                f"{self.end - self.start}"
            )


@dataclass(frozen=True, slots=True)
class Corpus:
    pages: tuple[str, ...]
    labels: tuple[Label, ...]

    @property
    def values(self) -> frozenset[str]:
        return frozenset(lab.text for lab in self.labels)

    @property
    def personal(self) -> tuple[Label, ...]:
        return tuple(lab for lab in self.labels if lab.personal)


def _luhn_sin(rng: random.Random) -> str:
    """Nine digits passing the Luhn check, which is what the SIN recogniser validates."""
    while True:
        digits = [rng.randrange(1, 9)] + [rng.randrange(0, 10) for _ in range(7)]
        total = 0
        for i, d in enumerate([*digits, 0]):
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        check = (10 - total % 10) % 10
        sin = [*digits, check]
        if len(set(sin)) > 2:
            return "{}{}{} {}{}{} {}{}{}".format(*sin)


def _mcp(rng: random.Random) -> str:
    """A Newfoundland and Labrador MCP number: twelve digits, in groups of three."""
    body = [rng.randrange(1, 10)] + [rng.randrange(0, 10) for _ in range(11)]
    s = "".join(str(d) for d in body)
    return " ".join((s[0:3], s[3:6], s[6:9], s[9:12]))


def _postcode(rng: random.Random) -> str:
    letters = "ABCEGHJKLMNPRSTVXY"
    return (
        f"A{rng.randrange(0, 10)}{rng.choice(letters)} "
        f"{rng.randrange(0, 10)}{rng.choice(letters)}{rng.randrange(0, 10)}"
    )


def _person(rng: random.Random) -> tuple[str, str]:
    shape = rng.choices([s for s, _ in _SHAPES], weights=[w for _, w in _SHAPES])[0]
    if shape == "accented":
        return f"{rng.choice(FORENAMES_ACCENTED)} {rng.choice(SURNAMES_ACCENTED)}", shape
    if shape == "internal":
        return f"{rng.choice(FORENAMES_PLAIN)} {rng.choice(SURNAMES_INTERNAL_CAPITAL)}", shape
    if shape == "apostrophe":
        return f"{rng.choice(FORENAMES_PLAIN)} {rng.choice(SURNAMES_APOSTROPHE)}", shape
    if shape == "hyphenated":
        return f"{rng.choice(FORENAMES_HYPHENATED)} {rng.choice(SURNAMES_PLAIN)}", shape
    return f"{rng.choice(FORENAMES_PLAIN)} {rng.choice(SURNAMES_PLAIN)}", shape


# Each template is one page. A `{slot}` is filled with a value that is labelled; everything
# else in the prose is written to identify nobody, which is what makes an unlabelled
# detection a false positive rather than an argument.
TEMPLATES = (
    """Request {file_number}

The applicant, {person1}, asked for all records held by the {department} concerning
the decision of {distractor1}. The request was received and acknowledged in writing.

{person1} may be reached at {email} or {phone}. The file was assigned to {person2},
an analyst in the division, on {distractor2}.

A third party named in the records, {person3}, was notified under the Act and given
twenty days to respond. See {distractor3}.
""",
    """Memorandum

To: {person1}
From: {person2}, {department}
Re: File {file_number}

The record at issue is a claim form completed by {person3}, born {dob}, whose MCP
number is {mcp} and whose social insurance number is {sin}. The form gives an address
in {town} with postal code {postcode}.

Nothing in {distractor1} requires the release of that information. The exception at
{distractor2} was considered and does not apply.
""",
    """Decision letter

Dear {person1},

Your request of {distractor1} is granted in part. The severed record is attached as
{distractor2}.

The employee who prepared the record, {person2}, employee number {employee_id}, has
confirmed that no other copy exists. Questions may be directed to {email} or {phone}.

Yours sincerely,
{person3}
{department}
""",
    """Notes of a meeting

Present: {person1}, {person2} and {person3}.

{person1} said the request covers {distractor1} and no more. {person2} asked whether
the claimant, whose file is {file_number}, had been contacted; {person3} confirmed a
letter had gone to {town}.

Action: write to the claimant at {email}. Review {distractor2} before releasing.
""",
)

_SLOT_TYPES = {
    "email": EntityType.EMAIL,
    "phone": EntityType.PHONE,
    "sin": EntityType.SIN,
    "mcp": EntityType.HEALTH_NUMBER,
    "postcode": EntityType.POSTAL_CODE,
    "dob": EntityType.DATE_OF_BIRTH,
    "file_number": EntityType.FILE_NUMBER,
    "employee_id": EntityType.EMPLOYEE_ID,
}


def _slots(rng: random.Random) -> tuple[dict[str, str], dict[str, str]]:
    """One page's slot values, and the orthographic shape of each person in it."""
    # Three different people. Two slots that happen to draw the same name would make a
    # page where one person is the applicant, the analyst and the third party at once, and
    # every metric over it would be measuring a document nobody could write.
    people: list[tuple[str, str]] = []
    while len(people) < 3:
        candidate = _person(rng)
        if all(candidate[0] != p[0] for p in people):
            people.append(candidate)
    plain = people[0][0]
    for ch in ("'", chr(0x2019), "-"):
        plain = plain.replace(ch, "")
    first, last = plain.split()[0].lower(), plain.split()[-1].lower()
    slots = {
        "person1": people[0][0],
        "person2": people[1][0],
        "person3": people[2][0],
        "email": f"{first}.{last}@example.gov.nl.ca",
        "phone": f"709-{rng.randrange(200, 999)}-{rng.randrange(1000, 9999):04d}",
        "sin": _luhn_sin(rng),
        "mcp": _mcp(rng),
        "postcode": _postcode(rng),
        "dob": f"{rng.randrange(1, 29):02d}/{rng.randrange(1, 13):02d}/{rng.randrange(1945, 2005)}",
        "file_number": f"ATIPP-{rng.randrange(2020, 2027)}-{rng.randrange(1, 9999):04d}",
        "employee_id": f"EMP-{rng.randrange(10000, 99999)}",
        "department": rng.choice(DEPARTMENTS),
        "town": rng.choice(TOWNS),
    }
    for i, distractor in enumerate(rng.sample(DISTRACTORS, 3), start=1):
        slots[f"distractor{i}"] = distractor
    shapes = {f"person{i + 1}": people[i][1] for i in range(3)}
    return slots, shapes


# Slots that are entities but not personal ones. Labelled, so that a detector finding them
# is not punished for it, and marked so that the leak rate stays a count of personal values.
_IMPERSONAL = {"town": EntityType.LOCATION, "department": EntityType.ORGANISATION}


def build(*, pages: int = 200, seed: int = 20260920) -> Corpus:
    """`pages` pages and a label for every value planted in them.

    Deterministic: the same seed gives the same corpus, so a number measured on it can be
    reproduced by anybody with this repository and no network.
    """
    rng = random.Random(seed)
    out_pages: list[str] = []
    labels: list[Label] = []
    for i in range(pages):
        page = i + 1
        slots, shapes = _slots(rng)
        text = TEMPLATES[i % len(TEMPLATES)]
        for name, value in slots.items():
            text = text.replace("{" + name + "}", value)
        out_pages.append(text)
        for name, value in slots.items():
            if name.startswith("distractor"):
                continue
            if name.startswith("person"):
                kind, personal = EntityType.PERSON, True
            elif name in _IMPERSONAL:
                kind, personal = _IMPERSONAL[name], False
            else:
                kind, personal = _SLOT_TYPES[name], True
            start = 0
            # A value planted twice is labelled at both occurrences, because a boundary that
            # masks the first mention and releases the second has not masked it.
            while (at := text.find(value, start)) >= 0:
                labels.append(
                    Label(page, at, at + len(value), value, kind, shapes.get(name, ""), personal)
                )
                start = at + len(value)
    return Corpus(tuple(out_pages), tuple(labels))


__all__ = ["Corpus", "Label", "build"]
