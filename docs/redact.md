# `boundary.redact`

Detection and the personal-class policy, as library objects. Built 2026-09-19 to project
07's specification (PLAN.md section B2.3, amended the same day), which was written from a
corpus-wide test rather than from a plan, and pulled forward from Part B because 07 is the
first consumer and had already found what a policy built the obvious way leaks.

Two halves, usable independently, with no gateway, no configuration and no network:

```python
from boundary.redact import Analyzer, Policy, RedactionRefused

spans = Analyzer().analyze(pages)              # every entity in the document, per page
policy = Policy(spans, allow={"Corner Brook"})  # placeholders for every one of them
body = policy.outbound(prompt)                 # substituted text, or RedactionRefused
answer = policy.rehydrate(model_output)        # the values back
```

The gateway does not apply this for you in 0.5. A project builds the outbound request with
`policy.outbound`, sends it through `Gateway.chat(..., data_class="personal")`, and
rehydrates the answer. That is what 07 asked for: a policy it can call in a test and
assert on. The proxy in Part B applies the same object inside the call path.

## Detection

`Analyzer.analyze(pages)` runs every recogniser over every page and returns a flat list of
`Span`s in page and offset order. A span has:

| Field | What it is |
|---|---|
| `page` | The page number as the caller numbers pages (`first_page`, default 1) |
| `start`, `end` | Half-open character offsets into **that page's** text, so `page_text[start:end] == text`. Never document-global |
| `text` | The matched substring, exactly |
| `entity_type` | From the closed vocabulary below |
| `score` | The recogniser's confidence in `[0, 1]` |
| `recogniser` | Which recogniser fired: `boundary:email`, `presidio:SpacyRecognizer`. 07's error decomposition splits leaks into "the detector never found it" against "the decision was wrong", and cannot attribute a miss without this |

The constructor checks the offsets against the text, so a span that lies about itself
cannot be built, and the analyzer checks each recogniser's spans against the page.

**Two guarantees the analyzer gives regardless of what produced a span:**

- **No span crosses a line break.** Presidio returned `Wallace Penashue\nDate` as one
  person. That draws a box over a word that is not personal information, and it poisons any
  placeholder policy downstream, because "Date" becomes an alias for a person. Every span
  from every recogniser is cut at its first line break, trailing whitespace dropped.
- **No two spans overlap.** A span that strictly contains another wins regardless of
  score, because it is the more complete redaction: 07 found the other rule releasing a
  house number when Presidio's `LOCATION` "Bannerman Street" at 0.85 beat an `ADDRESS`
  "14 Bannerman Street" at 0.75, and a partially covered value is a leak wearing a
  redaction (fixed in 0.5.1). Partial overlaps, where neither holds the other, go to the
  higher score, then the longer span, then the recogniser listed first. The `gov.nl.ca`
  Presidio calls a `URL` inside `aaronpenashue@gov.nl.ca.example` is contained in the
  email span and loses to it either way.

### Entity types

Closed. A consumer switches on these; a type it has never seen is a decision it cannot make.

| Type | Found by |
|---|---|
| `PERSON`, `LOCATION`, `ORGANISATION`, `URL` | Presidio (optional, below) |
| `EMAIL` | `boundary:email`: any number of domain labels, score 0.95 |
| `PHONE` | `boundary:phone-na`: North American formats, 0.8 |
| `SIN` | `boundary:sin`: nine digits with the Luhn checksum, 0.95; a run that fails the checksum is not a SIN and is left to the second pass |
| `HEALTH_NUMBER` | `boundary:nl-mcp`: the twelve-digit Newfoundland and Labrador Medical Care Plan number, contiguous or in four groups of three with spaces or hyphens (`123 456 789 012`, the way a form writes it), 0.95 within a few words of an MCP or health-card label, 0.6 bare |
| `ADDRESS` | No built-in recogniser (0.5.1). 07 brings its own; the type exists so that its span outranks a `LOCATION` for the street inside it |
| `POSTAL_CODE` | `boundary:ca-postal-code`: Canadian format with the letters Canada Post excludes per position, 0.85 |
| `DATE_OF_BIRTH` | `boundary:date-of-birth`: a date after a birth label (`date of birth`, `DOB`, `born on`), the span on the date only, 0.9. An unlabelled date is not a date of birth |
| `FILE_NUMBER` | `boundary:file-number`: an identifier after `file`, `case`, `claim`, `reference`, `request`, `docket`, `ticket` and a number word, 0.85 |
| `EMPLOYEE_ID` | `boundary:employee-id`: an identifier after `employee`, `staff`, `payroll`, `personnel` and an id word, 0.85 |
| `NAME_LIKE`, `ID_LIKE` | The policy's second pass, not a recogniser: something shaped like a name or an identifier that nothing claimed |

Every built-in recogniser is a regular expression with a golden in `tests/test_redact.py`,
needs no model and no download, and is on by default. A project adds its own with
`Analyzer(extra=[...])`: anything with an `id` and `analyze(text, page) -> spans`.

### Presidio

People, places and organisations need a model. `PresidioRecogniser` wraps Presidio's
analyzer behind the same protocol and names the Presidio recogniser that fired on every
span. It is optional:

```
uv sync --extra redact
uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
```

```python
from boundary.redact.presidio import PresidioRecogniser
spans = Analyzer(extra=[PresidioRecogniser()]).analyze(pages)
```

Constructing it without the extra raises `ConfigError` naming the extra. The adapter maps
`PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`, `LOCATION`, `ORGANIZATION` and `URL` and drops
anything else Presidio reports, because the vocabulary is closed.

**The default engine is built explicitly**, through `NlpEngineProvider` with the model
named and spaCy's labels mapped (`DEFAULT_NLP_CONFIGURATION`; pass `nlp_configuration=` for
another). A bare `AnalyzerEngine()` does not declare `ORGANIZATION` at all, so asking it
for organisations returns nothing, silently: on 07's corpus that read as 19.5% recall on
organisations against 86.7% for a detector using the same spaCy model (fixed in 0.5.1).
The mapping sends `ORG` to `ORGANIZATION` and `GPE`, `LOC` and `FAC` to `LOCATION`, which
is what 07 found gave the most correct output: "Newfoundland" as a location and the real
departments as organisations. It is tested against a
fake engine so the mapping needs no download, and once more live where the model is
installed, which on 2026-09-19 reproduced both of 07's defects exactly: Presidio 2.x found
`gov.nl.ca` as a `URL` and nothing else in the address. Engine start-up is about ten
seconds; build one and keep it.

## The policy

`Policy(spans)` mints one typed placeholder per distinct value, `<PERSON_1>`, `<EMAIL_2>`,
`<SIN_1>`, the same placeholder for the same value everywhere, matched case-insensitively
and with flexible whitespace. `redact(text)` substitutes them; `outbound(text)` substitutes
and then refuses unless the result is clean; `rehydrate(text)` puts the values back;
`vault` is the placeholder-to-value mapping, in memory, never written by the library.

Three findings from 07's corpus-wide test, which put 74 real values past a policy that
looked correct, shape it. Each has a test that fails without the fix.

**1. The policy is built over every entity in the document.** Not the spans of the text
being sent: the sentences around a span are full of entities the rules had already
decided, and those sentences travel in the payload. `Policy` takes the whole document's
spans and applies to any text.

**2. A detected full name licenses its parts.** The detector finds "Marie Chaulk"; the
prose two sentences later says "Marie". Each part of a person's name gets a placeholder
tied to that person, `<PERSON_1.1>` and `<PERSON_1.2>`, so the second mention is visibly
the same person and rehydrates to exactly what it was. A first name the detector also
found alone is folded into the person it belongs to. A part two people share gets its own
top-level placeholder, since the text alone cannot say which. Honorifics are not parts.
Whole values are matched without regard to case; **parts are matched case-sensitively**,
because a name is capitalised in prose and an ordinary word that shares its spelling is
not: "Grant" in "Dr. Grant agreed" is the person, "grant the request" is a verb and stays.
The cost is a sentence-initial "Will" or "May" that is substituted when someone by that
name is in the document, which rehydrates exactly and is the fail-closed direction.

**3. Detector recall is not 100 percent, and a boundary built on detections inherits every
miss.** After the first two fixes, seven names still left 07's boundary because Presidio
had not found them. So the policy has a second pass, independent of every detector, that
masks anything **name-shaped** (a run of capitalised words, or an all-capitals word of
three letters or more) or **identifier-shaped** (a token with five or more digits, letters
mixed with two or more digits, or a run of digit groups separated by spaces, hyphens or
dots carrying eight or more digits) unless it is on a vocabulary of terms that carry
decision-relevant meaning and identify nobody. The grouped-digits shape is the 0.5.1 fix
for the live leak 07 found: `123 456 789 012` matched neither the twelve-contiguous-digit
recogniser nor a token-by-token second pass, and 57 of 210 pages left `outbound()` with a
health number in them and no refusal. The second pass is the part that is supposed to make
recall irrelevant, so it was fixed first and the recogniser second. The pass is always on. A policy without it
is not a privacy boundary, and this module does not offer one.

The default vocabulary (`DECISION_VOCABULARY`) is function words, calendar words, the words
of the access-to-information workflow and legislation, and the jurisdiction's own names. A
project extends it with `allow=` for its terms (its department and programme names) and
`allow_patterns=` for shapes it needs intact (an ISO date, a dollar amount, a section
reference). Everything else that looks like a name or a number goes, and the results table
will report the two passes separately so a reader can see what detection alone would have
leaked.

**What this makes readable and what it does not.** A bare year, a percentage, a small count,
an ordinal and a short list of numbers all stay. Anything with five or more digits in one
token, or eight or more across grouped digits, goes. So a **full date goes**: `2024-03-15`
and `1998-03-14` are `<ID_LIKE_1>`, and so is a fiscal-year range written `2024-25`. That is
the same rule catching the same shape, and it is deliberate rather than a gap, because a
twelve-digit health number written with separators is indistinguishable from a date until
something knows which it is looking at. Where a date has to stay readable, an
`allow_patterns` entry for `\d{4}-\d{2}-\d{2}` excuses it; where a decision turns on a date,
make that decision on the raw text before the request is built, which is what project 07
does for the exclusion that depends on a date of death. A place name the vocabulary does not
carry is masked whether or not it identifies anyone. All of this is the fail-closed
direction: the cost of an over-mask is a placeholder the model has to reason around, and the
cost of an under-mask is a leak.

### The guard

`outbound` runs `check` on its own output: any vault value present in clear, and any shape
the second pass would have masked, is a `Leak`. Any leak raises `RedactionRefused` rather
than returning text the policy cannot vouch for. By construction it should never fire; it
exists because "should never" is not a guarantee. The exception's message carries counts
by kind and type only; the details are on `.leaks`, so a traceback is safe to paste.

### Rehydration

Tolerant of the ways models mutate a placeholder: any case (`<person_1>`), spaces inside
the brackets, or the brackets dropped (`PERSON_1`). Possessives and punctuation after a
placeholder are untouched. A placeholder the vault does not hold is left as it is and
`unresolved(text)` lists them, which is how a model's invention is caught rather than
silently rendered.

## What has been measured, and by whom

This repository has no evaluation harness for redaction yet; that is Part B's table, with
precision and recall per entity type on public corpora and a Canadian identifier set, and
the rehydration mutation rate. Nothing here claims a precision.

**Project 07 measured this engine's detection recall on 2026-09-19**, first against 0.5.0
and then again against 0.5.1, using its persona corpus: 5,355 values over 210 synthetic
pages with labels known by construction, reproducible in 07's repository with
`sever eval-detector --documents 210`. Recall only; precision is not claimed, because the
labels cover inserted values and not the prose around them. A synthetic corpus means
well-formed values, so every row is an upper bound. Intervals are 95%.

| Entity type | Values | 0.5.0 | 0.5.1 |
|---|---|---|---|
| person | 2380 | 93.1% (91.7 to 94.4) | 96.5% (95.4 to 97.4) |
| location | 105 | 57.1% (47.6 to 66.7) | 63.8% (54.3 to 72.4) |
| email | 420 | 100% | not re-reported |
| phone | 560 | 100% | not re-reported |
| postal_code | 70 | 100% | not re-reported |
| date_of_birth | 70 | 100% | not re-reported |
| employee_id | 70 | 100% | not re-reported |
| address | 280 | 99.3% (98.2 to 100) | 100% (100 to 100) |
| file_number | 210 | 83.3% (78.1 to 88.1) | 83.8% (78.6 to 88.6) |
| organisation | 210 | 19.5% (13.2 to 25.9) | 96.2% (93.5 to 98.6) |
| health_number | 105 | 20.0% (10.3 to 30.8) | 100% (100 to 100) |
| date | 35 | 0% | 0%, no such entity type |
| all | 5355 | 90.0% (89.1 to 90.9) | 96.3% (95.7 to 96.9) |

07 re-reported the rows that moved and the total; the rows marked so were measured but not
restated, and they were at 100% before. **Overall recall went from 90.0% to 96.3%**, and 07
confirmed each fix directly as well as statistically: every written form of a health number
types correctly, `Policy([]).outbound(...)` masks one with no detection at all, departments
come back as organisations with "Newfoundland" under location, and the house number stays
inside the address. 07 has dropped the score it had raised to 0.9 to work around the overlap
rule; containment does the work, and its address recogniser is back at an honest 0.75.

**Against 07's own detector.** On the same pages 07's detector reached 92.6% (91.8 to 93.3)
per entity with spaCy's `en_core_web_sm`, which is the model behind the 0.5.0 comparison
that used to sit in this table: person 87.2%, location 38.1%, organisation 86.7%,
health_number 100%, file_number 100%, date 100%. 07 has since moved it to `en_core_web_lg`
and it reaches **97.4% overall**, so the honest current comparison is 96.3% here against
97.4% there, both on the large model. Most of what the old gap measured was the model, which
is why the column is named for it. The remaining gap is 07's street-address and job-title
recognisers plus the `DATE` type this vocabulary excludes by design.

Three things 07 reported alongside the numbers, none of which changed the code:

- **Found is not the same as fully covered.** Organisations are now found at 96.2% but only
  40.0% of them are covered end to end, because spaCy splits a name like "Newfoundland and
  Labrador Health Services" into a location and an organisation. Recall alone cannot see
  that, which is why 07's evaluation has a coverage column: a partially covered value is a
  leak wearing a redaction. It is not a leak **here**, because the pieces are each
  substituted and anything left between them is name-shaped and taken by the second pass,
  so nothing of the name goes out in clear. It does mean one name arrives as two
  placeholders of different types. A gazetteer of Newfoundland and Labrador organisation
  and place names is the fix, and it is still Part B's (B2.3).
- **The grouped-digits rule masks a full ISO date**, so "died on 1998-03-14" becomes
  `<ID_LIKE_1>`, the same way a fiscal-year range like `2024-25` does. 07 checked it against
  its own workflow and reported it benign: its scrub does the same, a bare year survives in
  both, and the exclusion that turns on a date of death is decided by a local rule on raw
  text before any model call, so no evidence is lost. An `allow_patterns` entry excuses it
  where a date has to stay readable.
- **The label-word patterns do not match inside a longer word.** 07 probed them after fixing
  a bug of its own where a case-number recogniser matched "ref" inside "referred" and
  captured "erred". These return nothing for `referred 2024`, `referenced 4471`,
  `staff 12345` and `claimant 90210`, because the identifier's digit lookahead cannot reach
  past the space that the missing word boundary would otherwise have allowed. There is now a
  golden for it, on 07's suggestion, so the property is asserted rather than lucky.

**One bug in 07's corpus that this engine found**, worth recording because it is what running
two implementations against each other is for: 07's personas carried Social Insurance Numbers
that fail the Luhn checksum, so `boundary:sin` correctly refused them and nine personas in ten
had never exercised the SIN path at all. They are Luhn-valid now, checked against the
published specimen 046 454 286.
- Where a model does the work (person, location, and type accuracy at 97.5% against 89.3%),
  this engine was ahead; on the Canadian and domain identifiers it was behind, which is
  the direction the built-in recognisers exist to close.

07 keeps its own scrub layered on top, running after this policy, with a test that asserts
the layered result is clean rather than that this one is dirty. That is defence in depth,
and it is the right call for a consumer whose whole claim is that nothing personal leaves.

## Vault

In memory, per policy, never written anywhere by the library. Part B moves it to Redis
under a per-team key with a short TTL for the proxy, where a request and its answer are in
different processes; the library object does not need that, and a project that does can
serialise `policy.vault` itself, deliberately, into a place it controls.
