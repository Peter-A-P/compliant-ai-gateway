# `boundary.redact`

Detection and the personal-class policy, as library objects. Built 2026-09-19 to project
07's specification (PLAN.md section B2.3, amended the same day), which was written from a
corpus-wide test rather than from a plan, and pulled forward from Part B because 07 is the
first consumer and had already found what a policy built the obvious way leaks.

Every name this package exports, with the version each one arrived in, is in
[interface.md](interface.md) section 12. This page is the reasoning behind them.

Two halves, usable independently, with no gateway, no configuration and no network:

```python
from boundary.redact import Analyzer, Policy, RedactionRefused, sweep

spans = Analyzer().analyze(pages)              # every entity in the document, per page
spans = sweep(pages, spans)                    # a name found once, found everywhere
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

### The sweep

`sweep(pages, spans)` is a second detection stage: a person found anywhere in the document
licenses the other whole-word occurrences of that person's name parts everywhere else. It
returns the spans it was given plus the ones it found, overlaps resolved, with the added
ones carrying `recogniser == "boundary:sweep"` so a consumer measuring its detector can
tell the two apart.

```python
spans = Analyzer().analyze(pages)
spans = sweep(pages, spans)
```

The loss it recovers is lexical, not semantic. A model reads "Marie Chaulk applied" and
answers confidently; three pages later the prose says only "Marie", in a position that
carries no signal, and the model declines. The document holds the evidence that the page
does not.

Project 07 built this on its own detector first and measured it on 2026-09-20. Person
recall, same detector, sweep off then on:

| Name profile | Off | On | Gain |
|---|---|---|---|
| plain (published corpus) | 96.2% | 98.8% | +2.6 |
| all five Newfoundland shapes | 93.4% | 98.0% | +4.6 |
| two-token surname | 89.8% | 96.6% | +6.8 |
| bare accented forename | 88.8% | 96.8% | +8.0 |

The two shapes that cost the most gain the most, and the penalty for using this province's
names falls from 2.8 points to 0.8. Intervals are in 07's `results/sweep.md`; the ablation
there is the honest place to read two detectors against each other, because `results/names.md`
now compares an engine with a sweep against one without.

`Policy` has done the same thing at substitution time since 0.5.0 (finding 2 below), so the
bare "Marie" was already masked with or without this. What the sweep adds is the **span**:
a workflow drawing redaction boxes on a page, or an error decomposition attributing a miss
to the detector rather than to the decision, could not see it until now.

Four guards, each of which 07 hit before it had them:

1. **Case-sensitive.** "Drew" is a person and "withdrew" is a verb.
2. **Whole word**, on a boundary that knows about accents rather than the ASCII one used
   elsewhere in this package, since these are exactly the names the sweep is worth most for.
3. **Token runs, not single words.** Claim "Le Drew" whole, or you claim "Drew", release
   "Le", and call the result a redaction.
4. **Vocabulary words refused.** A heading the detector typed as a PERSON would otherwise
   black out an ordinary word on every page. The same guard now applies to the policy's own
   part licensing; see below.

The sweep adds no confidence of its own: a swept span carries the score of the detection
that licensed it, and a run carries the lowest score among its parts.

### Re-typing, and why recall could not see the leak it fixes

The sweep does not only add spans. A span that is **exactly** the name parts of a person the
document attributes, and whose type is one a consumer reads as impersonal (`LOCATION`,
`ORGANISATION`, `NAME_LIKE`, the `retype` argument), is re-typed `PERSON` and carries
`recogniser == "boundary:sweep:<original>"`, so both the correction and the recogniser that
fired stay visible.

07 found this building its own sweep, and it is the more useful half of the exchange.
Presidio typed "Bernadette Tuglavina" a `PERSON` in the introducing sentence and the bare
"Tuglavina" in the list below a `LOCATION`. The span was found, so recall counted it and
every table stayed healthy; then 07's release rule read `LOCATION` as not information about
an identifiable individual and printed that sentence in the schedule beside a third party's
surname. **A miss would have been better, because a miss does not argue for itself.**

| 07, rules only | Before | After |
|---|---|---|
| leak rate | 4.7% | 2.8% |
| leak rate, rules and model | 7.2% | 5.3% |
| over-redaction | 30.1% | 30.3% |
| detector recall | 98.6% | 98.6% |

Recall did not move at all, and that is the finding: **recall asks whether a span was found,
not what it was called**, and in a tool that releases text the label is the decision. It is
entry 4 of 07's `docs/rejected.md`. This library masks every span type, so the same mistype
was not a leak here; it was still a wrong label handed to a consumer that had to act on it,
which is what `boundary.redact` publishes spans for.

"Exactly" is the whole guard. "Hearn" inside "Hearn Building" is a place doing honest work,
even with a Mary Hearn in the document, and re-typing the building would be a worse error
than the one this corrects.

`swept(spans)` and `retyped(spans)` return the two buckets, and `original_recogniser(span)`
gives back the detector that fired whatever the sweep did to the span afterwards. A
consumer that prints `len(retyped(spans))` beside its recall is reporting the half of
detection recall is blind to. 07 split its own leaks three ways with this on 2026-09-20:
**16 never found, 23 found and mislabelled, zero from the decision rules.** The figure it
had been publishing as 50 decision errors was never about decisions at all. Neither project
had that split before the sweep made the second bucket countable, and neither has anything
else that catches a wrong label without a person going looking for one.

### What taking the names away does to the answer

Part B plans to measure the quality cost of redaction through the 03 gate, and the plan
until now assumed there was a cost to bound. Project 07 measured it on 2026-09-20 and the
premise inverted. Same spans, same model, twice, 20 documents, intervals printed:

| Arm | Leak rate | Over-redaction | Escalation |
|---|---|---|---|
| typed placeholders | 30.1% (21.7 to 39.0) | 19.0% | 30.5% |
| raw text | 60.2% (53.7 to 66.7) | 7.4% | 14.1% |

Taking the names away **halves** the leak rate, and the escalation column carries the
mechanism: shown a plausible name the model becomes willing to place the person,
escalation falling 30.5% to 14.1%, and it is then wrong about which side of the line they
are on more often than not. The arms agree on 55.7% of spans, so the name is doing a great
deal of work in the model's reasoning and the work is harmful. Deprived of it the model has
only the structure, which is the evidence that actually decides the question, and it says
so when the structure does not settle it.

Both arms run on a 7B model on 07's own machine, so the absolute figures are not a
pipeline's and not this library's: a guard refuses to construct the raw arm unless the
provider is price-zero, declares single-region residency and answers on loopback, all three
required, because the raw arm sends names and "the personas are invented" does not rescue
a pipeline whose claim is that it cannot send one. The pairing supports direction and rough
size. It is also the only part of either project's model layer a stranger can reproduce
with no key, no account and no spend.

Two consequences here. The plan's B2.8 is now a two-sided comparison rather than a
non-inferiority test (PLAN.md, amended 2026-09-20): a test shaped to bound a cost cannot
report a benefit, and on the evidence so far the benefit is the likelier finding. And 07
counted the requests: **171 distinct payloads for the placeholder arm against 239 for the
raw arm, out of 244 asks each**, a repeat rate of 30% masked against 2% raw. Placeholder
payloads repeat across documents once the names are gone; raw payloads are unique precisely
because the names are. **A redacted corpus is more cacheable than an unredacted one**, which
is an argument for the boundary with nothing to do with privacy, and a prediction Part B's
semantic cache can check.

07 corrected that pair on 2026-09-20, from 240 to 239, when it replaced the hand-written
sentence with a measured column. The method matters more than the digit and carries
straight over to B2.5: **count distinct payloads from the keys a run touches, not from the
size of the cache**, or a warm re-run counts entries it never asked for and inflates the
saving.

**The limit 07 wrote into its plan rather than glossing**, and it applies here identically:
the sweep needs the person found somewhere. A record that never names someone in a position
a detector can read gains nothing. That residual is what a gazetteer would cover, and this
does not. 07 measured it on both engines on 2026-09-20: **23 of its 29 remaining person
misses, 79%, are people its detector never found as a person anywhere in the record, against
28 of 84, 33%, for `boundary.redact`**. Proportionally more of what this library still misses
is reachable by sweeping, and proportionally more of what 07 misses needs a gazetteer.
07 corrected that figure before publishing it: the first version counted a persona as seen
if any span of theirs was found at all, including an email address, which licenses nothing
for a name sweep, and it put the unreachable share at 0%.

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
the same person and rehydrates to exactly what it was. A part the vocabulary allows is not
licensed (0.5.6): "Decision Letter" detected as a person at 0.6, which is the sort of
thing a model does to a heading, used to turn every "Decision" in an access-to-information
record into a placeholder. The detected span itself is still masked, because the detector
said so and this library fails closed; what the policy declines to do is carry one
detection across a document it was never asked about. The cost is a forename that is also
a common word: "Will Grant" is masked whole wherever it appears, and a bare "Will" three
pages later is released, which is the same thing the second pass does with it anyway. A first name the detector also
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
masks anything name-shaped or identifier-shaped unless it is on a vocabulary of terms that
carry decision-relevant meaning and identify nobody. The pass is always on. A policy
without it is not a privacy boundary, and this module does not offer one.

**Name-shaped** is a word of two or more letters that begins with a capital, or three or
more if it is all capitals. A word is a run of letters in any script, joined by hyphens or
apostrophes, and adjacent name-shaped words separated by spaces are one run. So
`MacDonald`, `McCarthy`, `LeBlanc`, `O'Brien`, `Jean-Pierre`, `Côté`, `Émile`, `GAGNÉ` and
`Петров` are all covered, and `OK`, `NL` and any lowercase word are not.

**Identifier-shaped** is a token with five or more digits, or letters mixed with two or
more digits. Pieces joined by single spaces or dots are judged as one value: `A1B 2C3`,
`123 456 789 012`, `709.555.0199`. An all-digit run needs eight digits before it is masked,
so a short list of small numbers stays readable; a run carrying letters is judged by shape
alone, because a postcode is only six characters. A piece carrying no digit ends a run, and
a piece the vocabulary allows breaks it, so `12 of 40` and `Q1 2024` stay readable.

Both shapes are stricter than they were, and every widening came from a leak somebody
found rather than from a guess:

| Written as | Before | Since |
|---|---|---|
| `123 456 789 012` | released, no refusal | masked (0.5.1, reported by 07) |
| `MacDonald`, `McCarthy`, `LeBlanc` | released, no refusal | masked (0.5.2, found by probing) |
| `Côté`, `Émile`, `GAGNÉ` | released, no refusal | masked (0.5.2, found by probing) |
| `O'Brien` | `O'` released, `Brien` masked | masked whole (0.5.2) |
| `A1B 2C3` | `A1B` released, `2C3` masked | masked whole (0.5.2) |
| `HCS-2024-0881` | masked twice, rehydrated twice | masked once (0.5.2) |

The three in the middle are the ones worth dwelling on. The pass used to look for
`[A-Z][a-z]+`, which cannot see a letter outside ASCII and splits a word at an internal
capital. In a province with French, Innu and Mi'kmaq names, and where `Mac` and `Mc`
surnames are ordinary, the layer whose entire job is to catch what the detector missed was
blind to a large share of the names it exists for. Nothing reported it: 07's corpus is
synthetic and ASCII, so its 96.3% could not have seen it either. It was found by probing
this file with the names the corpus does not contain, which is the argument for doing that
to a privacy boundary rather than trusting a number.

The default vocabulary (`DECISION_VOCABULARY`) is function words, calendar words, the words
of the access-to-information workflow and legislation, and the jurisdiction's own names. A
project extends it with `allow=` for its terms (its department and programme names) and
`allow_patterns=` for shapes it needs intact (an ISO date, a dollar amount, a section
reference). Everything else that looks like a name or a number goes, and the results table
will report the two passes separately so a reader can see what detection alone would have
leaked.

**What this makes readable and what it does not.** A bare year, a percentage, a small count,
an ordinal and a short list of numbers all stay. So a **full date goes**: `2024-03-15`
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

**Where the second pass cannot help, stated rather than discovered.** It judges a word by
its capital, so a script without case carries no signal it can read: Chinese, Japanese,
Korean, Arabic and Hebrew are left alone, as are Inuktitut syllabics. Masking every word in
those scripts would be the fail-closed move and would also make such a document unreadable,
so the pass does neither and says so here instead. A caller whose records contain those
scripts brings a detector for them, and the policy substitutes what it is given exactly as
it does for any other span. Cased scripts beyond ASCII, which is most of them, need no
detector. There is a test asserting each half of this, so the limit is known rather than
assumed.

This mattered less than it first looked for the case that prompted it. Project 07 checked
on 2026-09-20: the Labrador Inuttitut in its records is written in Latin script, so the
pass reads it like any other name. The gap that was actually losing values there was
narrower and more ordinary, and it is the one 0.5.2 fixed.

07 answered the rest of it with a corpus rather than a claim on 2026-09-20. Its Labrador
name profile, Innu surnames from Sheshatshiu and Natuashish and Inuit surnames from the
Nunatsiavut communities, is plain ASCII throughout: no accent, no apostrophe, no internal
capital. Orthographically those names are Doucette. They are found beside a forename and
missed standing alone, exactly like the shape profiles, and the sweep recovers 3.1 points
on them. So the mechanism was never the alphabet, it was the vocabulary: a name is lost
because nothing knows it, not because of the letters it is spelled with. Syllabics remain
outside what any of this reaches, on either side.

### The guard

`outbound` runs `check` on its own output: any vault value present in clear, and any shape
the second pass would have masked, is a `Leak`. Any leak raises `RedactionRefused` rather
than returning text the policy cannot vouch for. By construction it should never fire; it
exists because "should never" is not a guarantee. The exception's message carries counts
by kind and type only; the details are on `.leaks`, so a traceback is safe to paste.

### Text that already looks like a placeholder

A document can contain `<PERSON_7>` for real. An access-to-information office's own
procedure manual is about redaction; a record may already have been redacted by another
hand. Left alone, that text comes back from `rehydrate` as though this policy had written
it, and the record released at the end names somebody it never mentioned. That is
fabrication rather than disclosure, and it is the one failure in this module that makes the
output wrong instead of merely unsafe.

Two cases, because only one of them is answerable:

- **A placeholder this policy did not mint** is ordinary text that happens to look like a
  placeholder. It is masked whole, as one opaque token, and rehydrates to itself. Masking
  only the word inside it would leave brackets and a number wrapped around a placeholder of
  ours, which reads as a nested placeholder and is exactly the sort of thing a model tidies
  up on your behalf.
- **A placeholder this policy did mint** cannot be told from its own substitution, so there
  is no correct answer on the way back. `outbound` refuses, with `minted_placeholders_in`
  as the reason. This makes `outbound` a method for source text: handing it its own output
  raises rather than silently redacting twice. `redact` keeps no such guard and stays
  idempotent, so a caller that genuinely wants the second pass over redacted text can have
  it.

### Rehydration

Tolerant of the ways models mutate a placeholder: any case (`<person_1>`), spaces inside
the brackets, or the brackets dropped (`PERSON_1`). Possessives and punctuation after a
placeholder are untouched. A placeholder the vault does not hold is left as it is and
`unresolved(text)` lists them, which is how a model's invention is caught rather than
silently rendered.

## What has been measured, and by whom

### Measured here, from 0.6.0

    boundary redact eval                  # rules only: no key, no account, no network, no model
    boundary redact eval --presidio       # with the model, for the PERSON rows

200 generated pages holding 1,700 labelled entities, 1,450 of them personal, from seed
20260920. The corpus is code (`boundary/redact/corpus.py`), so the same command gives the
same numbers on any machine with a checkout.

| | Rules only | With Presidio |
|---|---|---|
| Detection recall, all entities | 35.3% (33.1 to 37.6) | 94.6% (93.5 to 95.6) |
| Found and typed correctly | 100.0% (99.4 to 100) | 97.2% (96.3 to 97.9) |
| Detection precision | 100.0% (99.4 to 100) | 93.2% (91.9 to 94.3) |
| **Leak rate after `outbound`** | **0.0% (0.0 to 0.3)** | **0.0% (0.0 to 0.3)** |
| Over-redaction | 2.3% (2.1 to 2.6) | 3.5% (3.2 to 3.9) |
| Round trip | 100.0% (98.1 to 100) | 100.0% (98.1 to 100) |
| Latency per page, detect | 0.2 ms p50, 0.3 p95 | 18.4 ms p50, 22.3 p95 |
| Latency per page, `outbound` | 1.1 ms p50, 2.1 p95 | 1.7 ms p50, 2.1 p95 |

Person recall is 100% (99.5 to 100) with Presidio and 0% without, since there is no PERSON
recogniser in the built-in set. Every other entity type is at 100% in both columns except
`file_number`, at 66.7% rules-only and 76.7% with the model, and `location`, at 74.0%.

**The first column against the fourth is the point of the whole design.** Rules-only
detection finds 35.3% of the entities and leaks none of them, because the policy's second
pass masks what no detector claimed and `outbound` refuses to return text it cannot vouch
for. A table reporting detection alone would have called this engine a third as good as it
is; a table reporting the leak rate alone would have hidden that the model is carrying the
detection. The cost is the over-redaction row: about one word in forty is masked that did
not need to be, mostly place names the vocabulary does not carry, which is the fail-closed
direction and is chosen on purpose.

**The harness found a leak on its first run**, which is the argument for having built it
rather than reasoning about it. `therese.gagne@example.gov.nl.ca` spelled with its accents
matched neither the EMAIL recogniser, whose pattern was ASCII, nor any shape in the second
pass, because an address carrying no digit is not identifier-shaped and a lowercase word is
not name-shaped. Both layers missed the same value and it left in clear with no refusal.
Fixed in both in 0.6.0: the recogniser reads letters in any script, and the second pass
masks anything written as an address whether or not a recogniser claimed it.

**What these numbers are not.** The corpus is synthetic, so every value is well formed and
sits in a sentence written for it, and every recall figure is an upper bound on the same
figure over real records. Precision is measured against planted labels in prose written to
contain no personal values, which is what makes a precision figure possible at all and also
what bounds it: a false positive here is a span over text this corpus knows to be nothing,
not over the messy near-values a real record holds. The `file_number` rows are honest
misses, and both are covered by the second pass rather than leaked, which is why the leak
rate does not move. There is still no measurement of what a model does to a placeholder in
flight; that is Part B's rehydration mutation rate and it needs the proxy.

### The Canadian identifier set, from 0.6.1

    boundary redact eval --identifiers

PLAN.md B10 asks for the Canadian set beside the corpora. There is no public corpus of
Canadian government identifiers, and one is not needed for this half: a Social Insurance
Number, a provincial health number and a postal code have exact published shapes, so the
suite writes each one in every form a clerk writes it in, and pairs it with the things that
look like it and are not. 1,150 cases from seed 20260920, built-in recognisers only.

**Claimed, and found** (every row is 100% detected and 100% masked): SIN spaced,
hyphenated and bare; health number in the same three forms; postal code spaced, unspaced
and in lower case; phone hyphenated, bracketed, dotted, with a country code and as long
distance; email in ASCII and accented; date of birth slashed, ISO and written out, each
after a label; employee id after a label.

**Claimed, and half found**: `file_number` at 50.0% (40.4 to 59.6). The recogniser wants a
label word next to the value ("File ATIPP-2024-0153") and half the cases write "whose file
is ATIPP-2024-0153", which is ordinary English. Every one of them is masked, by the second
pass rather than the recogniser. This is the same gap the prose corpus shows at 66.7%, on a
harder split.

**Not claimed at all, and still not released**:

| Family | Detected | Masked by the policy |
|---|---|---|
| business number | 8.0% (3.2 to 18.8) | 100.0% (92.9 to 100) |
| driver licence | 0.0% (0.0 to 7.1) | 100.0% (92.9 to 100) |
| passport | 0.0% (0.0 to 7.1) | 100.0% (92.9 to 100) |

This library has no recogniser for any of the three, and their rows say so. They are in the
suite because leaving them out would make the table describe the recognisers rather than
the boundary, and the boundary is what a reader is deciding whether to trust. What the
second column shows is the second pass carrying values nothing here was written to find.
The 8% on business numbers is the SIN recogniser: a business number's first nine digits pass
the Luhn check about one time in ten, so it is masked under the wrong type, which the
identifier set makes visible and the leak rate does not.

**The column that matters most: the second pass on its own.** Project 07 made the point on
2026-09-20 and it applies here exactly: its EMAIL recogniser found every address, so its
corpus never asked the second pass whether it could, and the backstop was untested rather
than working. So every case is also run with **every recogniser taken away**, and the table
carries that column. It found two holes the recognisers were covering:

- **`(709) 555-0199`**. The bracket ended the code run, `555-0199` was masked on its own,
  and the area code was published beside the placeholder. Half a value masked is the failure
  0.5.2 fixed for `A1B 2C3`, still here in another shape. Fixed in 0.6.2: a bracketed group
  is part of a run, and brackets cannot widen what is masked, because an all-digit run still
  needs eight digits, which is what keeps `section 31(1)` readable.
- **A date written in words.** `14 March 1978` is not masked by the second pass, so a date
  of birth in that form is masked only because the recogniser sees the label in front of it.
  The column says 66.7% rather than 100% and that is the honest number: there is no backstop
  for a written-out date. Masking every one of them would black out the dates a decision
  turns on, which is the same judgement the DATE_OF_BIRTH recogniser makes by requiring a
  label. It is a stated limit with a test on it, not an oversight.

Every other family is at 100% with no recogniser at all, including the three this library
claims no recogniser for.

**The near-misses: no recogniser fired on one.** Nine digits failing the Luhn check, a
postcode carrying a letter Canada Post does not use in that position, a fiscal year written
`2024-2025`, an unlabelled date, a count of staff, a handle with no domain: 0% detected in
every family, 700 cases. That is the precision figure for the identifier half, and it is
the one a regular expression can actually be held to.

**What it costs.** The second pass masks most of those near-misses anyway: 100% of the
invalid SINs, the short order numbers, the invalid postcodes, the unlabelled dates and the
fiscal years. `2024-2025` becoming a placeholder in every ATIPP record is a real readability
cost, and it is the fail-closed direction chosen deliberately: eight digits in two groups is
a shape this library will not release on the argument that this particular one is a year.
A caller who wants them back passes an `allow_patterns` entry matching a year range,
which is what that argument is for. The default does not, because a default that releases is
a default nobody reviewed.

### Measured by project 07

**07 measured this engine's detection recall on 2026-09-19**, first against 0.5.0
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

On 2026-09-20 07 added the sweep to its own detector and its published figure went to
**98.6%**, with detector misses 25 to 16 and its leak rate 5.4 to 4.7 percent rules-only.
That column now measures an engine with a sweep against a number measured here without one,
because both the 96.3% and the 98.6% are what each project ships rather than what each
model does. Neither is wrong and the pair is no longer like for like. 07's `results/sweep.md`
holds the ablation, and with the sweep off the two engines are within a point on every name
profile. This library gained the same stage in 0.5.6 and has not been re-measured with it;
the figure that would move is person recall, and by how much is not known here.

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

**A second name pool, and the first number anybody has on these shapes.** Project 07 built
a second corpus profile on 2026-09-20, `sever eval-names`, identical to the first except
for the pool the names are drawn from, and measured 0.5.4 against both. On the new pool
person recall is **94.2%** against 96.5%, and overall **95.4%** against 96.3%. The drop is
small and it is real, and it says the second pass was not the whole story: the first-pass
detector loses ground on these shapes too, which no fix to the fallback can recover.

Two things follow. A recall figure is about a name pool as much as about an engine, so the
pool belongs beside the number. And the apostrophes, the `Mac` and `Mc` prefixes and the
accents that 0.5.2 was about are still costing something after the fix, in the part of the
pipeline that does the finding rather than the part that catches misses.

**What the 96.3% could not see.** The corpus is synthetic and its names are ASCII, so no
row in the table exercises an accented name, an internal-capital surname or a postcode
judged in pieces, and every one of those was releasing values in clear when the table was
produced. They were found the next day by probing the second pass with the names the
corpus does not contain, and fixed in 0.5.2. A measured number is evidence about the inputs
that were measured; it is not a statement about the inputs nobody thought of, and the
distance between those two is where this class of bug lives.

That cuts both ways, and 07 reported the other half on 2026-09-20: **0.5.4 measures
identically to 0.5.1 on the original corpus, to the decimal, on every entity type.** Four
releases of leak fixes moved nothing there, which is exactly what a corpus with no accent,
no apostrophe and no `Mac` in it should show. 07 also found the same three blind spots in
its own detector-independent pass, the component its published argument rests on, which is
the case for running two implementations against each other rather than one against a
number.

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

**A policy holds two kinds of value and only one of them is derivable.** The values that
come from `spans` are derived, so rebuilding a policy over the same spans mints the same
placeholders: `<PERSON_1>` is the same person in both, and the guard against
placeholder-shaped source text still fires. The values the **second pass** finds are
discovered while redacting, not derived from anything, so a policy rebuilt between
redacting and rehydrating has never heard of them. It leaves them in the text, and
`unresolved` is the only thing that says so.

Pass the earlier vault to close that:

```python
first = Policy(spans)
body = first.outbound(prompt)
# ... another process, another day ...
later = Policy(spans, vault=stored_vault)
answer = later.rehydrate(model_output)     # resolves the second pass's values too
```

A restored entry keeps its placeholder, the counters move past every index restored so a
new value cannot be handed a placeholder that already means something else, and an entry is
stored under the one spelling `rehydrate` reads however the caller wrote it. A key that
names no entity type this version knows is refused rather than restored quietly.
