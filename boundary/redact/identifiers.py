"""The Canadian identifier set: every shape these recognisers claim, in every form it is
written in, and the near-misses that must not fire.

PLAN.md B10 asks for precision and recall "on public corpora and the Canadian set". This is
the Canadian set, built here because there is no public corpus of Canadian government
identifiers and inventing one is the honest half of the job: a Social Insurance Number, a
provincial health number and a postal code have exact published shapes, so a suite that
writes each of them in every form a clerk writes it in, and pairs it with the things that
look like it and are not, measures something a prose corpus cannot.

Three columns come out of it, and the third is the one that matters:

1. **Detected**: a recogniser returned a span over the value.
2. **Typed**: it returned the right entity type.
3. **Masked**: the value did not survive `Policy.outbound`, whether a recogniser found it
   or the second pass did. A family with 0% detected and 100% masked is not a leak; it is
   an identifier this library has no recogniser for and does not release anyway.

**Families this library does not claim are in here on purpose.** A business number, a
driver's licence and a passport number have no recogniser, and their rows say 0% detected.
Leaving them out would make the table describe the recognisers rather than the boundary,
and the boundary is what a reader is deciding whether to trust. What their rows then show
is the second pass doing the work, which is the same argument the prose corpus makes and a
harder case: these are values nothing in this repository was written to find.

**The negatives are the point of the exercise.** Nine digits that fail the Luhn check are
not a SIN. A postcode with a letter Canada Post does not use in that position is not a
postcode. A fiscal year written `2024-2025` is not a phone number. Each family carries its
own near-misses, and a recogniser that fires on them is reported as firing on them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from boundary.redact.types import EntityType

# Letters Canada Post uses. The first position excludes D, F, I, O, Q, U, W and Z; the
# others exclude D, F, I, O, Q and U.
_FIRST = "ABCEGHJKLMNPRSTVXY"
_REST = "ABCEGHJKLMNPRSTVWXYZ"
# Letters Canada Post does not use, for the negatives.
_NOT_FIRST = "DFIOQUWZ"
_NOT_REST = "DFIOQU"


@dataclass(frozen=True, slots=True)
class Case:
    """One identifier written one way, in the sentence it would appear in.

    `expect` says what this case is for:

    - `detect`: a recogniser must return `entity_type` over the value, and the policy must
      mask it.
    - `mask only`: a real identifier this library claims no recogniser for. Nothing is owed
      on detection; the policy must still not release it.
    - `ignore`: a string that looks like this family and is not one. No recogniser should
      fire on it, and masking it is over-redaction rather than a win.
    """

    family: str
    written_as: str
    text: str
    value: str
    entity_type: EntityType | None
    expect: str = "detect"

    @property
    def positive(self) -> bool:
        return self.expect != "ignore"

    def __post_init__(self) -> None:
        if self.value not in self.text:
            raise ValueError(f"case value {self.value!r} is not in its own sentence")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _valid_sin(rng: random.Random) -> str:
    while True:
        body = "".join(str(rng.randrange(0, 10)) for _ in range(8))
        body = str(rng.randrange(1, 10)) + body[1:]
        for check in range(10):
            if _luhn_ok(body + str(check)):
                return body + str(check)


def _invalid_sin(rng: random.Random) -> str:
    while True:
        digits = str(rng.randrange(1, 10)) + "".join(str(rng.randrange(0, 10)) for _ in range(8))
        if not _luhn_ok(digits):
            return digits


def _group(digits: str, sizes: tuple[int, ...], sep: str) -> str:
    out, at = [], 0
    for size in sizes:
        out.append(digits[at : at + size])
        at += size
    return sep.join(out)


def _cases_sin(rng: random.Random, n: int) -> list[Case]:
    out = []
    for _ in range(n):
        d = _valid_sin(rng)
        for form, value in (
            ("spaced", _group(d, (3, 3, 3), " ")),
            ("hyphenated", _group(d, (3, 3, 3), "-")),
            ("bare", d),
        ):
            out.append(
                Case(
                    "sin", form, f"Social insurance number {value} on file.", value, EntityType.SIN
                )
            )
        bad = _group(_invalid_sin(rng), (3, 3, 3), " ")
        out.append(
            Case("sin", "checksum fails", f"Purchase order {bad} was raised.", bad, None, "ignore")
        )
    return out


def _cases_health(rng: random.Random, n: int) -> list[Case]:
    out = []
    for _ in range(n):
        d = str(rng.randrange(1, 10)) + "".join(str(rng.randrange(0, 10)) for _ in range(11))
        for form, value in (
            ("spaced", _group(d, (3, 3, 3, 3), " ")),
            ("hyphenated", _group(d, (3, 3, 3, 3), "-")),
            ("bare", d),
        ):
            out.append(
                Case(
                    "health_number",
                    form,
                    f"The MCP number is {value} as given.",
                    value,
                    EntityType.HEALTH_NUMBER,
                )
            )
        # Eleven digits is not an MCP number, and a run of small numbers is not one either.
        short = _group(d[:8], (4, 4), " ")
        out.append(
            Case("health_number", "too short", f"Order {short} was filled.", short, None, "ignore")
        )
    return out


def _cases_postal(rng: random.Random, n: int) -> list[Case]:
    out = []
    for _ in range(n):
        code = (
            f"{rng.choice(_FIRST)}{rng.randrange(0, 10)}{rng.choice(_REST)}"
            f" {rng.randrange(0, 10)}{rng.choice(_REST)}{rng.randrange(0, 10)}"
        )
        out.append(
            Case(
                "postal_code",
                "spaced",
                f"Mailed to {code} last week.",
                code,
                EntityType.POSTAL_CODE,
            )
        )
        tight = code.replace(" ", "")
        out.append(
            Case(
                "postal_code",
                "unspaced",
                f"Mailed to {tight} last week.",
                tight,
                EntityType.POSTAL_CODE,
            )
        )
        lower = code.lower()
        out.append(
            Case(
                "postal_code",
                "lower case",
                f"Mailed to {lower} last week.",
                lower,
                EntityType.POSTAL_CODE,
            )
        )
        # A letter Canada Post does not use in that position.
        bad = rng.choice(_NOT_FIRST) + code[1:]
        out.append(
            Case("postal_code", "letter not used", f"Part {bad} was ordered.", bad, None, "ignore")
        )
        bad2 = code[:2] + rng.choice(_NOT_REST) + code[3:]
        out.append(
            Case(
                "postal_code", "letter not used", f"Part {bad2} was ordered.", bad2, None, "ignore"
            )
        )
    return out


def _cases_phone(rng: random.Random, n: int) -> list[Case]:
    out = []
    for _ in range(n):
        a, b, c = rng.randrange(200, 999), rng.randrange(200, 999), rng.randrange(0, 10000)
        forms = {
            "hyphenated": f"{a}-{b}-{c:04d}",
            "bracketed": f"({a}) {b}-{c:04d}",
            "dotted": f"{a}.{b}.{c:04d}",
            "with country code": f"+1 {a} {b}-{c:04d}",
            "long distance": f"1-{a}-{b}-{c:04d}",
        }
        for form, value in forms.items():
            out.append(
                Case("phone", form, f"Reached on {value} during the day.", value, EntityType.PHONE)
            )
        year = rng.randrange(2018, 2030)
        span = f"{year}-{year + 1}"
        out.append(
            Case("phone", "fiscal year", f"The {span} budget was tabled.", span, None, "ignore")
        )
    return out


def _cases_email(rng: random.Random, n: int) -> list[Case]:
    locals_ = (
        "marie.chaulk",
        "w.penashue",
        "the" + chr(0x00E9) + "r" + chr(0x00E8) + "se.gagn" + chr(0x00E9),
        "j.o" + chr(39) + "brien",
        "info+atipp",
    )
    domains = ("gov.nl.ca", "example.gov.nl.ca", "health-nl.example.ca")
    out = []
    for _ in range(n):
        value = f"{rng.choice(locals_)}@{rng.choice(domains)}"
        form = "accented" if any(ord(ch) > 127 for ch in value) else "ascii"
        out.append(Case("email", form, f"Write to {value} for a copy.", value, EntityType.EMAIL))
        handle = "@" + rng.choice(("Health", "ATIPP", "Records"))
        out.append(
            Case("email", "handle, no domain", f"Ask {handle} for a copy.", handle, None, "ignore")
        )
    return out


def _cases_dob(rng: random.Random, n: int) -> list[Case]:
    out = []
    months = (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    )
    for _ in range(n):
        d, m, y = rng.randrange(1, 29), rng.randrange(1, 13), rng.randrange(1940, 2006)
        forms = {
            "slashed, labelled": (f"born {d:02d}/{m:02d}/{y}", f"{d:02d}/{m:02d}/{y}"),
            "iso, labelled": (f"Date of birth: {y}-{m:02d}-{d:02d}", f"{y}-{m:02d}-{d:02d}"),
            "written, labelled": (f"d.o.b. {d} {months[m - 1]} {y}", f"{d} {months[m - 1]} {y}"),
        }
        for form, (sentence, value) in forms.items():
            out.append(
                Case(
                    "date_of_birth",
                    form,
                    f"The claimant, {sentence}, applied in person.",
                    value,
                    EntityType.DATE_OF_BIRTH,
                )
            )
        # An unlabelled date is a date. Requiring the label is the design: a record is full
        # of dates that belong to the file rather than to a person.
        plain = f"{d:02d}/{m:02d}/{y}"
        out.append(
            Case(
                "date_of_birth",
                "no label",
                f"The meeting of {plain} was minuted.",
                plain,
                None,
                "ignore",
            )
        )
    return out


def _cases_file(rng: random.Random, n: int) -> list[Case]:
    labels = ("File", "Case", "Reference", "Ref", "Claim", "Docket", "Request")
    out = []
    for _ in range(n):
        value = f"{rng.choice(('ATIPP', 'HCS', 'TI', 'OIPC'))}-{rng.randrange(2018, 2027)}-{rng.randrange(1, 9999):04d}"
        label = rng.choice(labels)
        out.append(
            Case(
                "file_number",
                f"after {label.lower()}",
                f"{label} {value} was opened.",
                value,
                EntityType.FILE_NUMBER,
            )
        )
        # The gap the prose corpus found: ordinary English between the label and the value.
        out.append(
            Case(
                "file_number",
                "label not adjacent",
                f"The claimant, whose file is {value}, was written to.",
                value,
                EntityType.FILE_NUMBER,
            )
        )
    return out


def _cases_employee(rng: random.Random, n: int) -> list[Case]:
    out = []
    for _ in range(n):
        value = f"EMP-{rng.randrange(10000, 99999)}"
        out.append(
            Case(
                "employee_id",
                "labelled",
                f"Employee number {value} prepared the record.",
                value,
                EntityType.EMPLOYEE_ID,
            )
        )
        count = str(rng.randrange(3, 40))
        out.append(
            Case(
                "employee_id",
                "a count of staff",
                f"A staff of {count} was assigned.",
                count,
                None,
                "ignore",
            )
        )
    return out


def _cases_unclaimed(rng: random.Random, n: int) -> list[Case]:
    """Identifiers this library has no recogniser for. Their rows show 0% detected, and
    what the third column then shows is the second pass carrying them."""
    out = []
    for _ in range(n):
        bn = f"{rng.randrange(100000000, 999999999)} RC{rng.randrange(1, 9999):04d}"
        out.append(
            Case(
                "business number",
                "with programme account",
                f"The supplier's business number is {bn} on the invoice.",
                bn,
                None,
                "mask only",
            )
        )
        licence = f"{rng.choice('ABCDEFGHJKLMNPRSTVWXY')}{rng.randrange(100000, 999999)}-{rng.randrange(10000, 99999)}"
        out.append(
            Case(
                "driver licence",
                "provincial",
                f"The licence number given was {licence} at the counter.",
                licence,
                None,
                "mask only",
            )
        )
        passport = f"{rng.choice('ABGHJ')}{rng.choice('ABGHJ')}{rng.randrange(100000, 999999)}"
        out.append(
            Case(
                "passport",
                "canadian",
                f"A passport numbered {passport} was produced as identification.",
                passport,
                None,
                "mask only",
            )
        )
    return out


def build_cases(*, per_family: int = 50, seed: int = 20260920) -> list[Case]:
    """Every family, positives and negatives, deterministic from `seed`."""
    rng = random.Random(seed)
    cases: list[Case] = []
    for maker in (
        _cases_sin,
        _cases_health,
        _cases_postal,
        _cases_phone,
        _cases_email,
        _cases_dob,
        _cases_file,
        _cases_employee,
        _cases_unclaimed,
    ):
        cases.extend(maker(rng, per_family))
    return cases


__all__ = ["Case", "build_cases"]
