# Changelog

Versions follow the plan's handover table (PLAN.md section 7). Interface changes within a
major version are additive only; see docs/interface.md.

## Unreleased

## 0.6.4 (2026-09-21)

**Rehydration fidelity, measured**, which is the last of Part B's redaction rows that can be
answered without the proxy. `boundary redact eval --rehydration` applies fifteen mutation
forms to a redacted page, the ways a model rewrites markup around a placeholder, and asks
whether the values come back.

- **Fourteen forms restore 100% of the values (99.8 to 100)** over 2,007 placeholders: as
  minted, lower case, spaces inside the brackets, brackets dropped, square and round
  brackets, bold markdown, backticks, a possessive, a hyphen or a space in place of the
  underscore, a zero-padded index, and a line break wrapped either before the number or
  inside the name.

- **Four of those resolved to nothing before this release**, and the measurement is what
  found them. The tolerance is widened **inside the brackets only**: any run of separators
  there is one underscore, and a zero-padded index is the index. Outside the brackets the
  exact `TYPE_n` spelling is still required.

- **`PLACEHOLDER_EMAIL_1` is 0% and stays there.** That is text around a placeholder rather
  than a mutation of one, and tolerating a prefix means resolving anything ending in
  `TYPE_n`. The asymmetry decides it: an unresolved placeholder costs a reader one value and
  is listed in `unresolved`; an over-resolved one puts somebody's name into a record that
  never mentioned them.

- **Fabrications: 0 of 16.** Every entity type with an index this policy never minted, plus
  a type that does not exist, resolves to nothing. That row must stay at zero, and there is
  a test asserting the widened pattern did not widen it.

## 0.6.3 (2026-09-21)

**Two files in this package were contradicting each other**, and the contradiction was
introduced here three releases ago. Project 07 named the class on 2026-09-21 after finding
one of its own, where a job title was sent to a vendor on the grounds that it is not
personal information and boxed in the same document on the grounds that it might be. Its
point is the useful part: the defect is not in a component, it is in two components that
are each defensible alone, and nothing that tests a module in isolation can see it.

- `types.py` promised that second-pass output is reported under its own entity type "so
  that the two passes stay separable". 0.6.0 had the second pass mint `EMAIL` for an
  address no recogniser claimed, which is the accurate type and the right thing for a model
  to read, and from that moment a consumer counting `<EMAIL_n>` could not tell a detection
  from a guess. Project 07's error decomposition is built on exactly that distinction.

- **`Policy.second_pass`** is the fix, and it gives up neither side: the type stays the most
  accurate one available, and the policy records which pass minted each placeholder. A span
  typed `NAME_LIKE` by somebody else's detector is that detector's work and is not in the
  set, which is checked, because 07 returns shape-based spans of its own.

- A policy rebuilt from an earlier one's `vault` reports an empty set, because a vault
  carries values and not provenance, which is the same reason it has to be passed at all.
  Stated in the docstring and the docs rather than left to be discovered, since an empty
  set there does not mean no fallback work happened.

## 0.6.2 (2026-09-20)

**The identifier set now measures the second pass with every recogniser taken away**, and
that column found two holes the recognisers were covering. The idea is project 07's, from
its own version of the email leak: its EMAIL recogniser found every address, so its corpus
never asked the second pass whether it could, and the backstop was untested rather than
working. A masking column where a recogniser is doing the work says nothing about the layer
that exists for when a recogniser is wrong.

- **`(709) 555-0199` was half masked.** The bracket ended the code run, `555-0199` was
  masked on its own, and the area code was published in clear beside the placeholder. This
  is the `A1B 2C3` failure of 0.5.2 in another shape, and the recogniser had been hiding it.
  A bracketed group is part of a run now. Brackets cannot widen what is masked on their own,
  because an all-digit run still needs eight digits before it is masked, which is what keeps
  `section 31(1)` and `page 12 of 40` readable, and there is a test for each.

- **A date written in words has no backstop, and the number now says so.** `14 March 1978`
  is not masked by the second pass, so a date of birth in that form is masked only because
  the recogniser sees the label in front of it. The column reads 66.7% rather than 100%.
  Masking every written date would black out the dates a decision turns on, which is the
  judgement the DATE_OF_BIRTH recogniser already makes by requiring a label, so this is
  recorded as a limit with a test rather than closed.

## 0.6.1 (2026-09-20)

**The Canadian identifier set**, `boundary redact eval --identifiers`, which is the other
half of the measurement PLAN.md B10 asks for. There is no public corpus of Canadian
government identifiers and one is not needed here: a SIN, a provincial health number and a
postal code have exact published shapes, so the suite writes each in every form a clerk
writes it in and pairs it with the things that look like it and are not. 1,150 cases from a
seed, built-in recognisers only, no network.

- **Every claimed shape is found and masked at 100%** except `file_number` at 50.0% (40.4
  to 59.6), which is the gap the prose corpus already showed at 66.7% on an easier split:
  the recogniser wants a label word adjacent, and half the cases write "whose file is
  ATIPP-2024-0153". Every one is masked by the second pass regardless.

- **No recogniser fired on a single near-miss**, over 700 of them: nine digits failing the
  Luhn check, a postcode carrying a letter Canada Post does not use in that position, a
  fiscal year written `2024-2025`, an unlabelled date, a count of staff, a handle with no
  domain. That is the precision figure for the half of this engine a regular expression can
  actually be held to.

- **Three families this library claims no recogniser for are in the suite on purpose**:
  business number, driver's licence and passport. Their rows say 0% detected, and 100%
  masked by the policy. Leaving them out would make the table describe the recognisers
  rather than the boundary. The 8% detected on business numbers is the SIN recogniser: a
  business number's first nine digits pass the Luhn check about one time in ten, so it is
  masked under the wrong type. The identifier set makes that visible and the leak rate
  cannot.

- **What the fail-closed default costs, measured**: the second pass masks 100% of the
  invalid SINs, short order numbers, invalid postcodes, unlabelled dates and fiscal years
  in the near-miss set. `2024-2025` becoming a placeholder in every record is a real
  readability cost and it is chosen rather than overlooked; a caller who wants year ranges
  back passes an `allow_patterns` entry, and the default does not, because a default that
  releases is a default nobody reviewed.

## 0.6.0 (2026-09-20)

**This repository can now measure its own redaction.** Every figure `boundary.redact` had
published belonged to project 07, which was honest and was not the rule this portfolio is
built on: each project ends with a number a stranger can check by running one command
against the repository. `boundary redact eval` is that command. It needs no key, no
account, no network and no model.

- **`boundary.redact.corpus`**: a labelled corpus generated from a seed, shipped as code
  rather than data. Values are planted in prose written to contain none, which is what makes
  a **precision** figure possible: a span outside a label is a false positive and can be
  counted, and no number this library published before had that. The name pool carries the
  five orthographic shapes in the proportions that broke this library rather than in equal
  ones, four of which 07's corpus contains none of. Towns and departments are labelled as
  `LOCATION` and `ORGANISATION` but marked impersonal, so a detector that finds them is not
  punished for being right and the leak rate stays a count of personal values.

- **`boundary.redact.evaluate`**: detection recall, how often a found span was **typed**
  correctly, precision, the leak rate after `outbound`, over-redaction, the round trip and
  latency, each with a Wilson 95% interval. Wilson rather than the normal approximation
  because several rows sit at exactly 0 and exactly 1, where the textbook interval claims
  certainty or leaves the unit interval.

  200 pages, 1,700 labelled entities, 1,450 personal. Rules only: detection recall 35.3%
  (33.1 to 37.6), precision 100% (99.4 to 100), **leak rate 0.0% (0.0 to 0.3)**,
  over-redaction 2.3%, round trip 100%, 1.1 ms a page. With Presidio: recall 94.6% (93.5 to
  95.6), precision 93.2%, leak rate unchanged, 18.4 ms a page. The first column against the
  fourth is the case for the second pass, stated as a measurement for the first time: a
  third of the entities are detected and none of them leak.

- **A leak the harness found on its first run**, which is why it was worth building rather
  than reasoning about. An email address spelled with accents matched neither the EMAIL
  recogniser, whose pattern was ASCII, nor any shape in the second pass, because an address
  with no digit is not identifier-shaped and a lowercase word is not name-shaped. Both
  layers missed the same value and it left in clear with no refusal. The recogniser now
  reads letters in any script, and the second pass masks address-shaped text whether or not
  a recogniser claimed it: that layer exists for when a recogniser is wrong, and it was not
  holding. Same class of defect as the accented-name leak of 0.5.2, one layer down.

- **Two detection gaps recorded rather than fixed**: `file_number` at 66.7% rules-only,
  because the recogniser wants a label word adjacent ("File ATIPP-2024-0153") and the corpus
  also writes "whose file is ATIPP-2024-0153"; and `location` at 74.0% with the model. Both
  are covered by the second pass, so neither moves the leak rate, and that is exactly the
  distinction the table now makes visible.

- **Documentation pass, 2026-09-20.** No code changed. The README's release narrative had
  grown into a changelog sitting between the one-liner and the results tables, which is the
  opposite of the order this repository's own rule sets, so it is a short status block and a
  link to this file now, and the honest limitation moved up above the supporting detail.
  `docs/explained.md` was still describing 0.1: it said the library does not stream and
  does not inspect content, both of which stopped being true in 0.3 and 0.5. The version
  history in `docs/interface.md` was one run-on paragraph that stopped at 0.5.1 and is a
  table through 0.5.8 now, with `BatchItemResult`, `__version__` and the eight redaction
  names that were exported but undocumented. PLAN.md's handover table was out of order and
  missing three releases. Dates on 0.5.7 and 0.5.8 were written a day ahead and are
  corrected to 2026-09-20. `docs/rejected.md` gains a third entry, judging a detector by
  recall alone, which is 07's measurement and is recorded here because it is about a number
  this repository publishes.

- **`tests/test_docs.py`**: the checks that would have caught the drift above. Every name
  exported from `boundary` and `boundary.redact` appears in the frozen interface document;
  every released version in this file is accounted for there; the newest entry here and
  `boundary.__version__` agree; no page carries a typographic dash, a curly quote or an
  ellipsis character; and every relative link resolves. Five of those failed against the
  tree as it stood this morning.

- **Correction, 2026-09-20**: the raw arm of 07's ablation is 239 distinct payloads of 244
  asks, not 240. 07 found it when it replaced a hand-written sentence with a measured
  column, which is the better reason to trust the new figure than the size of the change.
  The repeat rates it puts beside it are the useful pair: 30% masked against 2% raw. The
  method note carries into PLAN.md B2.5, because it will bite this cache harder than it bit
  07: count distinct payloads from the keys a run touches, not from the size of the cache,
  or a warm re-run counts entries it never asked for and inflates the saving. The 0.5.8
  entry below keeps the number it was written with.

## 0.5.8 (2026-09-20)

Two things from project 07, one of which changes the plan.

- **`swept(spans)`, `retyped(spans)` and `original_recogniser(span)`**: the sweep's two
  buckets, countable. 07 split its leaks three ways with the same distinction and got 16
  never found, 23 found and mislabelled, zero from the decision rules, which means the
  figure it had been publishing as 50 decision errors was never about decisions. A consumer
  that prints `len(retyped(spans))` beside its recall is reporting the half of detection
  that recall is blind to. Neither project has anything else that catches a wrong label
  without a person going looking for one.

- **PLAN.md B2.8 is a two-sided test now, not a paired non-inferiority one**, amended in
  this commit with the reason, per the rule in CLAUDE.md. The plan assumed redaction costs
  quality and set out to bound the cost. 07 ran the ablation and the premise inverted:
  typed placeholders leaked 30.1% (21.7 to 39.0) against 60.2% (53.7 to 66.7) for raw text,
  because a plausible name makes the model willing to place the person and the confidence
  is misplaced. Escalation fell 30.5% to 14.1% with the name present, and the two arms
  agree on only 55.7% of spans. Twenty documents on a 7B model running on 07's own machine,
  behind a guard that refuses to build the raw arm unless the provider is price-zero,
  single-region and on loopback, so the direction and rough size rather than the figure. A
  one-sided test would have recorded that as "no worse than", which is true and useless.

- **B2.5 will report the cache hit rate for redacted and raw payloads separately.** 07
  counted 171 distinct requests for the placeholder arm against 240 for the raw arm over
  the same spans: placeholder payloads repeat across documents once the names are gone, raw
  payloads are unique precisely because the names are. If it holds at portfolio scale,
  redaction raises the hit rate, which is an argument for the boundary with nothing to do
  with privacy. A blended number would hide it.

## 0.5.7 (2026-09-20)

Project 07 built the sweep on its own detector, hit a leak this library's version would
have inherited, and sent the fix back the same day.

- **The sweep re-types a bare name the detector called a place.** A span that is exactly
  the name parts of a person the document attributes, and whose type is one a consumer
  reads as impersonal (`LOCATION`, `ORGANISATION`, `NAME_LIKE`), is now re-typed `PERSON`.
  Presidio typed "Bernadette Tuglavina" a `PERSON` in the introducing sentence and the bare
  "Tuglavina" in the list below a `LOCATION`. The span is found, so recall counts it and
  every table stays healthy, and then 07's release rule read `LOCATION` as not information
  about an identifiable individual and printed that sentence in the schedule beside a third
  party's surname. A miss would have been better, because a miss does not argue for itself.
  On 07's corpus the fix moved the leak rate 4.7 to 2.8 percent rules-only, 7.2 to 5.3 with
  the model, over-redaction 30.1 to 30.3, and **detector recall not at all**. That is the
  finding, and it is entry 4 of 07's rejected list: recall asks whether a span was found,
  not what it was called, and the label is what decides whether the text is released.

  This library masks every span type, so the same mistype was never a leak here. It was a
  wrong label handed to a consumer that had to act on it, which is what publishing spans is
  for. A re-typed span carries `recogniser == f"{SWEEP_ID}:{original}"`, so the correction
  and the recogniser that fired are both visible; `retype=()` switches it off. "Exactly" is
  the whole guard: "Hearn" inside "Hearn Building" is a place doing honest work.

- **The caseless-script limit, answered with a corpus instead of an assertion.** 07's
  Labrador name profile, Innu surnames from Sheshatshiu and Natuashish and Inuit surnames
  from the Nunatsiavut communities, is plain ASCII throughout and still loses 3.1 points
  without the sweep. The mechanism was never the alphabet, it was the vocabulary: a name is
  lost because nothing knows it. Syllabics stay outside what either engine reaches.

- **The gazetteer residual measured on both engines**, which is the limit this library
  named when the sweep landed. 23 of 07's 29 remaining person misses, 79%, are people its
  detector never found as a person anywhere in the record, against 28 of 84, 33%, here. More
  of what this library still misses is reachable by sweeping; more of what 07 misses needs a
  gazetteer. 07 caught an error in its own first version of that figure before publishing it,
  which had counted a persona as seen on the strength of an email address.

## 0.5.6 (2026-09-20)

Project 07 built the document-wide sweep on its own detector, measured it, and sent the
mechanism back. It is a detection stage this library did not have, and its warning about
the guards turned out to name a hole here as well.

- **`boundary.redact.sweep`**: a person the detector found anywhere in a document licenses
  the other whole-word occurrences of that person's name parts everywhere else. The loss it
  recovers is lexical rather than semantic: a model reads "Marie Chaulk applied" and answers,
  and three pages later the prose says only "Marie", in a position carrying no signal. 07
  measured person recall on its hard name pool going 93.4 to 98.0 percent, and the two
  shapes that cost the most gained the most: a two-token surname by 6.8 points, a bare
  accented forename by 8.0. The four guards are 07's, each one a bug it hit first:
  case-sensitive, whole word, token runs rather than single words, and vocabulary words
  refused. The whole-word boundary here knows about accents, since those are the names the
  stage is worth the most for.

  `Policy` has masked the bare "Marie" since 0.5.0, so this changes no redacted output. What
  it adds is the **span**: a workflow drawing boxes on a page, and an error decomposition
  attributing a miss to the detector rather than to the decision, could not see that
  occurrence before. `sweep` returns the spans it was given plus the ones it found, overlaps
  resolved, with the added ones carrying `recogniser == "boundary:sweep"`.

- **A name part the vocabulary allows is no longer licensed**, which was a real
  over-redaction here and is the guard 07 warned about. "Decision Letter" typed as a PERSON
  at 0.6, which is the sort of thing a model does to a heading, turned every "Decision" in
  an access-to-information record into a placeholder: the one word such a record is about,
  blacked out on every page, by a policy nobody would suspect. The detected span itself is
  still masked, because the detector said so and this library fails closed. What the policy
  declines to do is carry one detection across a document it was never asked about. The cost
  is stated rather than hidden: a forename that is also a common word, "Will Grant", is
  masked whole wherever it appears and a bare "Will" three pages later is released, which is
  what the second pass does with that word in any case.

- **`boundary.redact.names`**: `is_name_shaped` and `name_parts`, which the policy and the
  sweep both need. Two copies of that judgement is a leak waiting for a disagreement.

- The comparison in docs/redact.md is labelled again: 07's published figure is now 98.6% with
  its sweep, against 96.3% here measured without one. Both are shipped engines rather than
  models, and this library has not been re-measured since gaining the stage.

## 0.5.5 (2026-09-20)

Project 07 reported two things about 0.5.4 and a number. One was a real gap, one was a
false alarm here that was a real bug there, and the number is the most interesting of the
three.

- **`Policy(spans, vault=...)`**, so a policy rebuilt between redacting and rehydrating
  resolves what the first one found. A policy holds two kinds of value and only one is
  derivable: the ones from `spans` are derived, so a rebuild over the same spans mints the
  same placeholders and the guard added in 0.5.3 still fires; the ones the **second pass**
  finds are discovered while redacting and cannot be recovered from anything, so a rebuilt
  policy used to leave them in the text with only `unresolved` saying so. A restored entry
  keeps its placeholder, the counters move past every index restored so a new value cannot
  be handed one that already means something else, and an entry is stored under the one
  spelling `rehydrate` reads however it was written. This is also the library half of Part
  B's Redis vault. 07's report said a rebuilt policy does not refuse; it does, for the
  derived placeholders, and the docs now say which half is which.

- **The substring false positive 07 hit is not available here**, checked rather than
  assumed: the surname "Le Drew" made 07's own guard refuse any text containing "withdrew".
  Both patterns here are bounded by a non-alphanumeric on each side, for whole values and
  for the parts of a name alike, so "withdrew", "andrews" and "sundrew" are untouched while
  "Drew" and "Drew's" go. There is a test for it now.

- **07's second corpus recorded in docs/redact.md.** A second name pool, otherwise
  identical, drops person recall from 96.5% to **94.2%** and overall from 96.3% to
  **95.4%** on 0.5.4. The drop is small, real, and says the second pass was not the whole
  story: the first-pass detector loses ground on apostrophes, `Mac` prefixes and accents
  too, which no fix to the fallback recovers. A recall figure is about a name pool as much
  as an engine, so the pool now sits beside the number.

- **Also recorded: 0.5.4 measures identically to 0.5.1 on the original corpus, to the
  decimal, on every entity type.** Four releases of leak fixes moved nothing there, which
  is what a corpus with no accent, no apostrophe and no `Mac` in it should show. 07 found
  the same three blind spots in its own second pass, which is the case for running two
  implementations against each other rather than one against a number.

- The caseless-script limitation is narrowed in the docs: 07 confirms the Labrador
  Inuttitut in its records is Latin script, so the pass reads it like any other name.

## 0.5.4 (2026-09-20)

Two things probing the command line turned up, both about reading the ledger rather than
writing it.

- **A data class written by a later version could not be queried.** `ledger report` printed
  a row holding an unrecognised class, correctly and as stored, and then `--data-class` on
  that same value exited 2 as a typo. The vocabulary is closed for writing, because a class
  the Part B policy cannot place is a row nobody can act on; it should never have been
  closed for reading, because an auditor has to be able to ask about the rows in front of
  them, and an old reader that cannot query its own file is a different failure from the one
  the vocabulary protects against. A filter is now accepted when it is in the vocabulary,
  is `undeclared`, or occurs in the ledger being read. A word matching nothing anywhere is
  still refused, and the message now names what the file does hold.
- **A long project name pushed every column out of line.** The project column was 24
  characters and the plan's names run to 31, so the ordinary report for project 07 was
  unreadable exactly where somebody would be reading it. It is sized to the longest name
  present, and a test asserts the header and the row still line up.
- Measured while probing, not recorded as a result because there is no harness behind it:
  `outbound` on a 2,500-token page with about 150 known values takes roughly 10 ms, against
  Part B's budget of under 100 ms with redaction on. Detection without Presidio is about
  3 ms a page. Presidio dominates when it is enabled.

## 0.5.3 (2026-09-20)

One more found by probing, and it is the only failure in this module so far that makes the
output **wrong** rather than unsafe: a document containing the literal text `<PERSON_1>`
came back from `rehydrate` carrying a real person's name. The released record would name
somebody it never mentioned, which in an access-to-information workflow is fabrication. A
document can carry that text for real: an office's own procedure manual is about redaction,
and a record may already have been redacted by another hand.

- **A placeholder the policy did not mint is masked whole**, as one opaque token, and
  rehydrates to itself. Masking only the word inside it, which is what the second pass did
  once it stopped skipping foreign placeholders, left brackets and a number wrapped around a
  placeholder of ours.
- **A placeholder the policy did mint is refused** by `outbound`, through the new
  `minted_placeholders_in`, because it cannot be told from the policy's own substitution and
  there is no correct answer on the way back. `Leak.kind` gains `placeholder`.
- **`outbound` is therefore for source text.** Handing it its own output now raises instead
  of quietly redacting twice. `redact` keeps no such guard and is still idempotent, and the
  test that used to assert that property through `outbound` asserts it through `redact`.
- CI's new redact job passed on its first run, so the two live Presidio assertions now run
  somewhere other than one laptop.

## 0.5.2 (2026-09-20)

Four more leaks in the second pass, found by probing it with the names project 07's corpus
does not contain rather than by a report. Each released a real value in clear with no
refusal, and none of them could have shown up in the 96.3% recorded yesterday, because that
corpus is synthetic and its names are ASCII. The pass whose entire job is to catch what the
detector missed was the thing that was wrong, which is the second time that has been true
in two days.

- **A word is now a run of letters in any script, judged afterwards, instead of the pattern
  `[A-Z][a-z]+`.** That pattern cannot see a letter outside ASCII, so `Emile Berube` was
  masked and `Émile Bérubé` was not, and `GAGNÉ` was not. In a province with French, Innu
  and Mi'kmaq names that is not an edge case. It also splits a word at an internal capital,
  so `MacDonald` became `Mac` plus `Donald`, each then discarded for being glued to a
  letter: the commonest surname shape in Newfoundland was invisible. `MacDonald`,
  `McCarthy`, `LeBlanc`, `DeSouza`, `O'Brien`, `Jean-Pierre`, `Côté` and `Петров` are all
  masked now, and `OK`, `NL` and lowercase words still are not.

- **`O'Brien` was masked as `O'` plus a placeholder**, releasing the first letter of a
  surname under something that reads as a redaction. It is one word now.

- **A value written in pieces is judged whole.** `A1B 2C3` was half masked and half
  released, because `2C3` is identifier-shaped on its own and `A1B` is not. Pieces joined by
  single spaces or dots are one candidate, so a postcode, a health number and a dotted phone
  number each go or stay together. A piece carrying no digit ends a run and a piece the
  vocabulary allows breaks it, so `12 of 40`, `pages 3, 4 and 5` and `Q1 2024` stay
  readable. This replaces the narrower grouped-digits rule added in 0.5.1.

- **An acronym inside a code is no longer masked twice.** `HCS` in `HCS-2024-0881` is
  name-shaped, and masking it as well as the code it sits inside produced two placeholders
  over one value, which rehydrated to that value twice. Names are taken last and never from
  inside an identifier already claimed.

- **A stated limit instead of a silent one**: the pass judges a word by its capital, so a
  script without case (Chinese, Arabic, Inuktitut syllabics) carries nothing it can read.
  Masking every word in those scripts would be fail-closed and would also make a Labrador
  document unreadable, so the pass leaves them, docs/redact.md says so, and a test asserts
  both halves. Cased scripts beyond ASCII need no detector.

- **The property test now generates accented and internal-capital names**, and CI gained a
  job that installs the `redact` extra with a pinned spaCy model wheel and runs the redact
  tests, so the two live assertions about Presidio's behaviour run somewhere other than one
  laptop. That job asserts the imports are present before it runs, because a failed install
  would otherwise skip those tests and report green.

## 0.5.1 documentation follow-up

Documentation and one test; no behaviour change.

- **07's re-measurement of 0.5.1 recorded in docs/redact.md**, beside the 0.5.0 numbers
  rather than over them. Overall detection recall 90.0% to 96.3%; health_number 20.0% to
  100%, organisation 19.5% to 96.2%, person 93.1% to 96.5% (the engine change helped person
  as well, which neither of us predicted). 07 confirmed each of the three fixes directly and
  has dropped the 0.9 score it had raised to work around the overlap rule.
- **The comparison column is now named for the model it used**, because that is most of what
  it measured: 07's detector was on spaCy's small model at 92.6% and is now on the large one
  at 97.4%, against 96.3% here. The honest current gap is 07's street-address and job-title
  recognisers plus the `DATE` type this vocabulary excludes by design.
- **Two limitations written down** rather than changed: organisations are found but only
  40.0% covered end to end, because spaCy splits a long name into a location and an
  organisation (nothing leaves in clear, since the pieces are each substituted and the
  remainder is name-shaped, but one name arrives as two placeholders; the gazetteer is still
  Part B's), and the grouped-digits rule masks a full ISO date as well as a fiscal-year
  range, which 07 checked against its own workflow and reported benign.
- **A golden asserting that a label word inside a longer word is not a label**, on 07's
  suggestion after it fixed exactly that bug in its own recogniser: `referred 2024`,
  `referenced 4471`, `staff 12345` and `claimant 90210` all return nothing.

## 0.5.1 (2026-09-19)

Project 07 ran 0.5.0 against its persona corpus the same day, 5,355 labelled values over 210
pages, and reported three findings with one-line reproductions. One was a live leak. All
three are fixed here, and 07's recall table is recorded in docs/redact.md as the first
measurement of this engine, with its caveats.

- **A space-separated Medical Care Plan number left the boundary unredacted, with no
  refusal.** `123456789012` was caught by the recogniser and `123-456-789-012` by the second
  pass, but `123 456 789 012`, which is how the number is written on a form, was caught by
  neither: the recogniser wanted twelve contiguous digits and the second pass's identifier
  shape did not span spaces. On 07's corpus 57 of 210 pages came back from `outbound()` with
  a real health number in them, recall 20.0% (10.3 to 30.8). Two fixes, because the second
  pass is the part that is supposed to make recall irrelevant: `boundary:nl-mcp` now takes
  the four groups of three with a space or hyphen between them, and the second pass masks
  any run of digit groups separated by spaces, hyphens or dots that carries eight or more
  digits, as `ID_LIKE`. A SIN that fails the checksum and is written with spaces is caught
  the same way.

- **Organisations were never requested from Presidio.** A bare `AnalyzerEngine()` does not
  declare `ORGANIZATION`, so asking for it returned nothing: recall 19.5% (13.2 to 25.9)
  against 86.7% for 07's own detector, and the model was not the cause. The default engine
  is now built through `NlpEngineProvider` with an explicit model and an explicit label
  mapping (`ORG` to `ORGANIZATION`, `GPE`, `LOC` and `FAC` to `LOCATION`), which is what
  07 found gave the most correct output: "Newfoundland" as a location and the real
  departments as organisations. The configuration is `DEFAULT_NLP_CONFIGURATION` and a
  caller can pass its own.

- **A span that strictly contains another now wins regardless of score.** Presidio's
  `LOCATION` "Bannerman Street" at 0.85 beat 07's `ADDRESS` "14 Bannerman Street" at 0.75,
  so the box went over the street and the house number was released: a partially covered
  value is a leak wearing a redaction. Containment is the fail-closed case and is resolved
  first; score, then length, then priority still decide partial overlaps.

- **`EntityType.ADDRESS`**, which 07's address recogniser needed and the closed vocabulary
  did not have.

- Two things 07 reported and this release does not change, on purpose: an unlabelled file
  number such as `HCS-2024-0881` in a heading is not typed as `FILE_NUMBER` (it is still
  masked by the second pass as `ID_LIKE`, so it does not leave), and dates are not an
  entity type (a date of death is invisible to detection, which is 07's evidence for the
  twenty-year exclusion and belongs in 07's results rather than in a recogniser here).

## 0.5.0 (2026-09-19)

`boundary.redact`, pulled forward from Part B on Peter's decision the same day 0.4.0 was
cut: project 07 is the first consumer, and the plan's dates are no longer the schedule.
Built to 07's specification in PLAN.md section B2.3, which came from a corpus-wide test
rather than from the plan. Documented in docs/redact.md.

- **Detection.** `Analyzer.analyze(pages)` returns `Span`s with a page number and half-open
  offsets into that page, the exact text, an entity type from a closed vocabulary, a score
  and the id of the recogniser that fired. Two guarantees whatever produced a span: none
  crosses a line break (Presidio's `Wallace Penashue\nDate` defect), and none overlap. Eight
  built-in regular-expression recognisers with goldens: email with any number of domain
  labels (Presidio's `gov.nl.ca` defect), Canadian postal code, SIN with the Luhn checksum,
  the NL Medical Care Plan number, North American phone, labelled date of birth, labelled
  file and case numbers, labelled employee identifiers. Presidio for people, places and
  organisations behind the same protocol, as an optional `redact` extra, naming the Presidio
  recogniser on every span. Run live on 2026-09-19 it reproduced both of 07's defects.

- **The personal-class policy.** `Policy(spans)` mints stable typed placeholders for every
  entity in the document; `outbound(text)` substitutes and refuses with `RedactionRefused`
  unless its own output is clean; `rehydrate(text)` puts the values back, tolerant of case,
  spacing and dropped brackets. The three findings from 07's test each have a test that
  fails without the fix: the policy is document-wide, not built from the spans being sent;
  a detected full name licenses its parts (`<PERSON_1.1>`); and a second pass, always on,
  masks anything name-shaped or identifier-shaped that no detector claimed unless it is on
  a vocabulary of decision-relevant terms, because a boundary built on detections inherits
  every miss. A sixty-seed round-trip property test holds redact-then-rehydrate to the
  identity over documents with detected names, undetected names and places, and identifiers.

- **Nothing here logs, prints or raises a personal value.** The refusal's message carries
  counts; the details sit on an attribute.

- **Not measured yet, and said so**: precision and recall per entity type with intervals,
  and the rehydration mutation rate, are Part B's table. Nothing in this release claims a
  recall.

- The gateway does not apply the policy inside a call in 0.5. A project builds the request
  with `policy.outbound`, sends it with `data_class="personal"`, and rehydrates the answer.

## 0.4.0 (2026-09-19)

Project 07 (access-to-information redaction) asked for four things on 2026-09-19, smallest
first. The first two are here, both additive, and the version is bumped because the frozen
interface gained a keyword and the ledger gained a column. The third, `boundary.redact`,
stays in Part B (May 2027): 07 does not need it to proceed, and 07's brief is now written
into PLAN.md section B2.3 so that Part B is built to it. The fourth was a question, answered
in docs/interface.md section 11, item 6.

- **A cap for 07.** `access-to-information-redaction` in `config/caps.yaml`, at the amounts
  07 proposed. Without an entry the project fell to the default, whose per-run cap is below
  the cost of one full corpus run, so the first real run would have been refused partway
  through. The portfolio line rises to hold it, as the test requires. Peter to confirm
  before 07's first vendor call.

- **`data_class` on every call method: `chat`, `achat`, `chat_stream`, `achat_stream`,
  `raw`, `batch_submit`.** What kind of data the request carries, declared by the caller
  from the closed vocabulary `DataClass`: `public`, `internal`, `personal`, `sensitive`,
  which is PLAN.md B2.2's, so a row written now reads under the Part B policy without
  translation. Written to the row and the span; enforced by nothing yet. A word outside the
  vocabulary raises `ValueError` before the model is resolved and no row is written. None
  writes null, which the reports keep apart from every declared class as `undeclared`: no
  claim is not the weakest claim, the same rule `residency` follows. 07 had been smuggling
  the class into `purpose` as a string convention; a convention cannot be queried, aggregated
  or, later, enforced.

- **Ledger schema v7, additive: `data_class`.** One nullable column, never backfilled. Also
  on the span as `boundary.data_class`, four words from a closed vocabulary that cannot carry
  content.

- **`data_class` and `call_uid` on `ChatResponse`.** The uid is the answer to 07's question
  about attaching document and page identifiers to a call: keep them in the caller's own
  records and join on `call_uid`, which survives `ledger merge` where `ledger_id` does not.

- **`--data-class` on `boundary ledger report` and `boundary ledger residency`**, and a
  `class` column in the report's key. `ledger residency --data-class personal` is the audit
  question in one command: which calls carried personal data, and where did they go.
  `--data-class undeclared` lists the calls that made no claim. A misspelt class exits 2
  rather than printing an empty table that reads as "nothing personal left".

- **No free-form metadata mapping on a call**, and none planned. Every value that reaches a
  row or a span has to be one the library can show cannot carry content; a caller-filled
  mapping is a channel it cannot check. A fact worth recording gets a typed column, which is
  what this release did.

## 0.3.0 (2026-09-19)

Three additions project 06 (fraction-of-the-bill, package `smallprint`) asked for on
2026-09-19, all additive. Tagged after one live streamed call against the laptop's Ollama
server on this exact code: `boundary smoke local --stream`, status 200, `llama3.2:3b`, 32
input and 2 output tokens at price zero, ledger row 8, **5,484 ms to the first token and
5,557 ms to the last byte**. The gap between the two is the finding the column exists to
show: this model was cold, and nearly all of the wall time was spent before anything
appeared. A non-streamed call would have reported one number and hidden that.

- **Streaming for `openai_compat` hosts: `chat_stream` and `achat_stream`.** Sends
  `stream: true` with `stream_options: {include_usage: true}`, reads the server-sent events as
  they arrive and returns a whole `ChatResponse`: the full text, the usage from the final
  event, and a new optional field **`ttft_ms`**, the wall time from sending the request to the
  first content delta. `latency_ms` runs to the last byte. One ledger row per call, not per
  event. Project 06 measures time to first token against self-hosted vLLM and llama.cpp servers
  in its load tests, and needs the number to land in the same ledger row as the cost.

  Standard mode only. Pass-through is refused with `PassthroughViolation` before the model is
  resolved: pass-through's guarantee is one request body compared byte for byte with one
  response body, and a stream is many events read as they come. The development cache is
  never consulted, because a cached answer has no first token to time. Every other provider
  kind raises `NotImplementedError` naming the kind; each arrives when a project needs it,
  with a mock upstream test of its own event shape.

  **What counts as the first token** is spelled out rather than left to the reader: the first
  event whose delta carries non-empty `content`. OpenAI's leading role-only event is not a
  token. A `reasoning_content` delta is not one either, so a reasoning model that thinks for
  ten seconds has a ten-second time to first token, which is what a person waiting for it
  waited. The usage is read from whichever event carries it: a final event with empty
  `choices` on OpenAI and vLLM, the last content event on llama.cpp. Both shapes are goldens.

  **A stream that carried no usage object is written uncosted**, even for a priced model.
  Zero tokens at a real rate is US$0.00, and that is not what the call cost; it is what the
  library could not see. This is the never-guess-a-price rule applied to a host that ignores
  `stream_options`, and `strict_cost` raises on it as it does for an unknown model.

  **A stream that fails after it began is not retried.** The host has produced tokens it may
  bill for and the library cannot count them, so a second attempt would put two hosts' worth
  of work on one row. The row records the transport error and no cost. A retryable status, or
  a failure before any byte arrived, retries exactly as `chat` does.

  Tested against a mock SSE upstream with a known 150 ms delay placed **after** the role-only
  event and before the first content event, so the test fails if the wrong event stops the
  clock. Sixty-four concurrent async streams each write their own timed row and finish in
  about one delay rather than sixty-four, which is the test that the ledger lock and the
  parser do not serialise them.

- **Ledger schema v6, additive: `ttft_ms`.** One nullable column, null for every call that was
  not streamed and for a streamed call that ended with no content. Nothing is backfilled. Also
  on the span as `boundary.ttft_ms`, which is a duration and cannot carry a prompt.

- **Connection pool limits made explicit**: 128 connections, 64 kept alive, on both clients.
  06 holds at least 64 streams open against one host, and a client whose pool was the
  bottleneck would report a time to first token that was the pool's and not the server's.
  httpx's default of 100 was already enough; a test now asserts the number rather than
  inheriting it.

- **Measured rates for self-hosted hosts.** A provider entry may be flagged `self_hosted: true`
  and the configuration may name `self_hosted_prices: <dir>`, a directory of dated files in
  the price-file format that the project running the host supplies. 06 derives a rate from a
  dated GPU-hour price and a measured throughput, as USD per million output tokens with input
  at 0, re-measured per model, quantisation and GPU, with `source` naming the measurement run.

  Two refusals, both at gateway construction, keep "one copy of every vendor price" true with
  the overlay beside it: the overlay may not price a provider that is not flagged, and the
  packaged vendor list may not price one that is. `self_hosted` and `price_zero` together are
  refused too: free and measured are different claims. A row costed from the overlay cites
  the overlay in `price_list` and `price_sha256`, so a self-hosted cost in a merged ledger is
  audited back to its measurement and never mistaken for a vendor rate. The caps' pre-call
  estimate uses the overlay as well. `boundary prices check` validates and prints it.

- **`boundary smoke <provider> --stream`**, which is what the live call above ran.

- **A spend cap for project 06** in `config/caps.yaml`: US$100 a month, US$30 a run, the
  amounts 06 proposed, to be confirmed by Peter before its first vendor call. The portfolio
  line moves from 350 to 400 because it has to hold every named project's month at once and a
  test says so; the vendor console caps are unchanged.

- **PLAN.md section 2.8 amended in the same commit**: streaming was out of scope for version 0
  because nothing needed it before May 2027. Something does now, and it arrived for the one
  provider kind the thing that needs it speaks.

## 0.2.2 (2026-09-18)

- **Ledger schema v5, additive: `price_sha256`.** A row now records the **rates** it was
  costed against, not only the date of the list it read them from. A sha256 of the parsed
  rates, canonically ordered, so two files holding the same rates fingerprint the same
  whatever their comments, key order or line endings say, and two files holding different
  rates never do. Also on `ChatResponse`, beside `price_list`.

  **It exists because of a real near-miss rather than a hypothetical one.** September's ledger
  was costed from two repositories. On 2026-09-18 this library and project 02 both held a
  `2026-09-12.yaml`: **byte-different**, 6,766 against 7,607, and after parsing **identical**,
  the 841 bytes being comments. Every row from both projects said only
  `price_list: 2026-09-12`. The rates agreed, and nothing in either ledger could have shown it
  if they had not. A date is not unique across repositories. A fingerprint is.

  The fingerprint is of the parsed rates and not the file's bytes on purpose: a comment is not
  a rate, and a column that moved when somebody reformatted a file would be noise rather than
  evidence. `source` and `date` are excluded for the same reason, and `date` is already in
  `price_list`, so the two columns answer different questions: which list was in force, and
  what was in it.

- **The near-miss is held open as a test**, `tests/test_price_identity.py`. It loads 02's
  copies from the sibling checkout and asserts the fingerprints match, skipping rather than
  failing when 02 is not checked out beside this repository. A second test asserts the two
  files really are still byte-different, so that if somebody makes them identical the parsed
  comparison gets retired deliberately instead of passing for a reason nobody intended.

- **The open price-file decision is answered** in `docs/invoice-check.md`, and it had already
  answered itself: price files ship inside the package at `boundary/prices/`, this
  repository's configuration says `prices: builtin`, and all five dated lists are there, so
  September's costing is reproducible from this checkout alone. What remains is one line in
  02, which still reads its own copies.

- **The Vertex quota inference was wrong, and the correction is in
  `docs/hyperscaler-setup.md` beside it.** This document said a Claude bucket with no
  `effectiveLimit` was probably not a quota you can raise but a quota you do not have, making
  self-service inapplicable and a sales request the real route. The Cloud Quotas API says the
  project **is** eligible to ask, on the exact quota that returned the 429. The route is the
  ordinary console increase. The wrong version is kept in place because the correction needs
  something to correct.

  **Two of the check's own fields were worthless, which is recorded rather than quietly
  dropped**: `isFixedLimit` is absent from all 367 quotaInfos, and `isEligible` is true on all
  367 including quotas this project has never called, so it describes the project rather than
  any bucket. It refutes "you may not ask" and does not establish "asking will work".

  **What replaces it is narrower and better.** The quota that returned the 429 holds nineteen
  rows: seventeen Anthropic models with no value at all, and two Google models at 600. One
  quota, one API call, the whole finding on one screen, from the governance API rather than
  the reporting one. Elsewhere on the same account Anthropic quotas do carry values: web
  search at 1200, the superseded Claude 3 Haiku at 15,000 tokens a minute in five regions,
  Google's internal Anthropic test models unlimited. So the account is not unprovisioned for
  Anthropic. What has nothing is every current Claude model and only those.

- **No Canadian region on Vertex, now confirmed from a second source.** The finding rested on
  one reading of one Model Garden panel. A sweep of all 367 quotas returns **zero** rows for
  any Anthropic model in any `northamerica-*` region: not a low limit, no row at all.
  `claude-haiku-4-5` has regional rows in `europe-west1` and `us-east5` only. The two sources
  disagree about `asia-east1`, which is recorded rather than smoothed over, because tidying a
  disagreement away makes the agreement worth less.

- **An expired Google access token does not say it has expired.** Three Google APIs gave three
  accounts of one stale token: Vertex said the credentials were invalid, Service Usage and
  Cloud Quotas both said `ACCESS_TOKEN_TYPE_UNSUPPORTED`, and only `oauth2/tokeninfo` said
  `invalid_token`. The middle one names the wrong thing and sends a reader off to build a
  service account. This is the one-line argument for `boundary/credentials.py`.

## 0.2.1 (2026-09-18)

The hyperscaler release, and it is as much a set of findings as a set of adapters. Tagged
after smoke run #6 from GitHub Actions exercised every provider on this exact code: seven
calls, five costed, two uncosted because the vendor publishes its own rates. Two of the three
hyperscaler platforms answered; the third is refused by quota rather than by code, and that
refusal is documented rather than hidden.

Both prior tags waited on live calls before the version left `.dev0`. This one did the same:
the calls ran first, on `f0abfbe`, and the tag followed.

- **`boundary ledger residency`**, which reads the v4 column back. Schema v4 has been
  recording a residency on every row since 2026-09-15 and nothing could ask the question the
  column exists to answer: `ledger report` groups by project, model and month, which is a
  spend question. This groups by provider, region and residency, widest reach first.

  `--require single-region|geo|global` turns the report into a gate and exits 2 when any
  group went wider than the limit. Three rules, all fail-closed:

  - An **undeclared** row fails every limit, including `--require global`. Null is not
    `global`. `global` is the weakest claim somebody made; null is no claim at all.
  - A residency class **this version does not recognise** fails every limit too, because an
    old reader cannot know whether a new class is narrower or wider than `global`. It still
    prints the value as stored.
  - A group of **pure cache hits** is never a violation, because nothing left the machine.
    The calls are still counted, so the totals agree with `ledger report`.

  The command prints what a clean report does not prove: residency is configuration, not
  observation, and no vendor reports where a request was actually processed. That belongs in
  the output and not only in the docs, because the moment somebody is most likely to
  over-read a clean report is while they are looking at one.

- **`local` now declares `region: localhost` and `residency: single-region`**, which the new
  command surfaced on its first run: it was the only provider entry that could honestly claim
  single-region and the only one not saying so. Every hosted entry declares `geo`, `global`,
  or nothing at all, so a run that has to pass `--require single-region` has exactly one route
  available today, and it is the one where nothing reaches a network. That is the state of the
  market rather than a gap in the configuration, and there is a test that fails on the day it
  changes.

- **A Canada Central provider entry** (`foundry-canada`), which is the only genuinely Canadian
  deployment available anywhere in this configuration. It needed no code: Foundry's
  OpenAI-compatible route is `https://{resource}.services.ai.azure.com/openai/v1/`, takes
  `Authorization: Bearer`, carries the deployment name in `model`, and uses implicit versioning
  so there is no `api-version` parameter, which is exactly what the `openai_compat` adapter
  already builds.

  It exists because of a finding rather than a plan. Foundry allocates zero quota for every
  Anthropic model and normal quota for everything else, so the Canadian resource that cannot
  serve Claude serves a GPT deployment perfectly well.

  The row it writes says `region: canadacentral` and `residency: global`: deployed in Canada,
  processed anywhere, and it says both. That pair is the worked example the residency argument
  needed, and it is the ceiling rather than a workaround.

- **`ProviderError`'s message now follows its retry count.** An adapter's `parse_error` cannot
  know how many attempts were made, so it builds the error with zero and the gateway assigns
  the real count afterwards. The message was formatted in `__init__`, so that assignment never
  reached the text: a Vertex 429 retried three times reported "after 0 retries" while the
  ledger correctly recorded `retries = 3`. The message is what a person reads first when a call
  fails, and that one read as evidence the retry policy had not run.

- **Google credential minting for Vertex** (`boundary/credentials.py`), behind a new optional
  `vertex` extra: `uv sync --extra vertex`, or `pip install 'boundary[vertex]'`. A
  cloud-platform access token from Application Default Credentials, refreshed five minutes
  before it expires rather than after a call fails on it. This was the last code gap in the
  three hyperscaler adapters: a pasted token lasts about an hour, which is fine for a smoke
  call and useless for a drift run.

  A new `credentials` field on a provider entry (`env`, the default, or `google_adc`) says
  where the credential comes from. Under `google_adc` a set `api_key_env` variable still
  wins, so one checked-in entry serves a laptop with `gcloud auth application-default login`
  and a CI runner with a token from an earlier step and no gcloud.

  The token is fetched through this library's own pinned httpx client rather than
  google-auth's `requests` or `urllib3` transports, so the token call and the vendor call
  verify TLS against the same store. On a network that inspects TLS, the alternative fails
  the mint while every vendor call succeeds, and reads like a credentials problem.

  The adapter is unchanged and still pure. `VertexAdapter` receives a token string and knows
  nothing about where it came from.

- **Amazon Bedrock adapter** (`aws_bedrock`), a subclass of the Anthropic adapter with a
  different path and a residency guard. AWS serves the Anthropic Messages API at
  `POST {host}/anthropic/v1/messages` with a Bedrock API key in `x-api-key`, so there is no
  SigV4 and no botocore: PLAN.md section 2.2's SDK exception now names only `google-auth`.
  Exercised live from `ca-central-1` on 2026-09-15, and the response and error goldens in
  `tests/test_bedrock.py` are that call's bytes rather than documentation samples.

  The guard is most of the adapter. On Bedrock the processing geography is encoded in the
  model identifier's prefix and nowhere else, so `build_request` refuses a profile sent to
  the single-region endpoint, a bare id sent to the endpoint that cannot serve one, a region
  that differs from the one named in the host, and a model identifier that contradicts the
  provider entry's declared `residency`.

  Bedrock calls are **uncosted**: AWS publishes its own Claude rates and they were not
  readable from the published page on 2026-09-15, and an unknown price never becomes an
  estimate. Batches are not implemented; Bedrock's is `CreateModelInvocationJob` over S3, a
  different API, and a batch aimed at a bedrock provider is refused by name.

- **Ledger schema v4, additive: `residency`.** One nullable column recording how far a call
  was allowed to travel from its `region`, copied from the provider entry's declaration.

  It is configuration rather than observation because no vendor reports where a request was
  processed: Foundry hides the hosting version, Vertex hides the processing location, and
  Bedrock strips the routing profile out of the model identifier it echoes back. A row can
  say what the operator chose and cannot say what the vendor did. Null and `"global"` stay
  distinct, and no existing row is backfilled, because a value there would be a claim about
  where data went that nobody made.

- **`residency` on a provider entry** (`single-region`, `geo`, `global`), optional and
  additive. `aws_bedrock` enforces it against the model identifier; other kinds record it
  without checking, because their platforms offer no equivalent signal to check against.

- **Microsoft Foundry and Google Vertex adapters** (`azure_foundry`, `gcp_vertex`), both
  subclassing the Anthropic adapter because both platforms serve the Messages API. Only the
  envelope differs and only the envelope is overridden, so a change to how a response or its
  usage is read cannot drift between the direct vendor and a platform. Endpoint shapes verified
  against the vendors' documentation on 2026-09-14. Foundry takes the Azure key in `api-key`
  and keeps `model` in the body, where it is the deployment name; Vertex moves `model` into the
  URL and `anthropic_version` into the body as `vertex-2023-10-16`. 19 goldens, no network.
- **Vertex refuses a host and a region that disagree.** The URL's host and its `locations/`
  segment both name a geography and the host is what actually routes, so a provider entry
  pinned to `northamerica-northeast1` serving a route pinned to `global` would send data to
  another country while the ledger row recorded the region the route asked for. That pair is
  now a `ConfigError` naming both sides, not a request. Failing closed on residency is Part B's
  rule; it is cheaper to build in now than to retrofit around live rows.
- **`ProviderConfig.project`**, additive and optional: the Google Cloud project id, which Vertex
  carries in the URL rather than in a header. Every existing configuration still loads.
- **Neither platform has a price entry**, so a call through either writes an uncosted row. That
  is deliberate. The rates are copied from the vendor's page on the day the account exists and
  dated then; a rate written weeks early carries a date that lies about when it was checked.
  Foundry bills in Claude Consumption Units at US$0.01 per CCU rated at standard USD rates, so
  the invoice check divides the Azure line item by 100 before comparing.
- **Claude on Foundry is unavailable to a Free Trial subscription, and the error does not say
  so.** Deploying `claude-haiku-4-5` Global Standard in `eastus2` on 2026-09-14 was refused
  with "Insufficient quota" on **both** offered model versions. Microsoft's quota table gives
  the documented default for that subscription type: 80 RPM, on either version. **Measured
  through the ARM API rather than inferred from the error**: all 19 Claude quota entries in
  `eastus2` read zero, while 159 other AIServices quotas in the same region are non-zero, so
  the subscription is provisioned normally and only the Anthropic models are at zero. Same in
  `eastus`, `centralus` and `swedencentral`, on both hosting versions; `canadacentral` has no
  Claude quota entry at all, which confirms the region finding from the resource provider
  rather than the documentation. The documented default is a ceiling you may request, not an
  allowance you receive. `docs/hyperscaler-setup.md` step 3a has the one-line `az` command
  that reproduces it.

  **Two wrong explanations were published here first and are corrected rather than deleted**,
  because one of them reached the portfolio site. The first said the Azure-hosted version was
  rationed while the Anthropic-hosted one deployed freely; the table marks both allocatable
  with the same default. The second said the subscription must be a Free Trial, which gets
  zero for everything; it is pay-as-you-go. Both were inferred from the error message instead
  of read from the quota page the portal will simply show, which is the failure mode this
  repository keeps finding in other people's cost claims, appearing here twice in one day.

  What survives: quota on this platform is per subscription and shared across regions, so a
  per-deployment capacity message points away from its own cause and invites four changes
  (region, model, version, retry) that cannot move a subscription-level allocation.
- **Microsoft's own wording for the distinction**, quoted in the setup document because it is
  the vendor's rather than ours: "Data might be processed globally, outside the resource's
  Azure geography, but data storage remains in the AI resource's Azure geography." Processing
  is global, storage is regional. A requirement written about storage can be met; one written
  about processing cannot, on this deployment type, and the two are commonly written as if
  they were one.
- **Foundry offers no Claude model in a Canadian region, which is a finding rather than a
  blocker.** A Foundry resource created in Canada Central on 2026-09-14 had no Anthropic model
  to deploy, and Microsoft's own region table agrees: every `claude-*` row reads `-` against
  `canadacentral` and `canadaeast`, in both hosting versions. The Data Zone deployment type,
  the one narrower control on offer, exists only for the United States. So Foundry gives a
  Canadian buyer two choices for Claude, global routing or the US, and neither is Canada.
  `docs/hyperscaler-setup.md` carries the region lists and the source; PLAN.md B2.2 is amended
  because Part B's residency design had assumed the Canadian set was non-empty. It can be
  empty, and the worked example should show that refusal rather than design around it, which
  demonstrates failing closed better than a policy that always finds a route. The earlier
  wording in this repository said a Canadian deployment "is achievable"; for Claude on Foundry
  that was wrong and it is corrected.
- **Batches for OpenAI, Together and Gemini**, which until now only Anthropic had. Project
  02's own-run panel batched 9,292 of its 9,298 Anthropic calls and none of the other 18,000,
  because there was nothing to batch them with: US$11.24 of that run went at full price where
  a batch rate would have been about US$5.62.
- **The OpenAI shape submits in two round trips**, uploading the requests as a JSONL file and
  then creating a batch that names it. `BatchAdapter` gains `uploads_input_file`, and a new
  `UploadingBatchAdapter` carries the upload methods. The ledger rows are written before the
  *upload*, not before the create, because the upload is the request the prompts leave in.
  Neither round trip is billed, so a failure in either completes every row as a failure at no
  cost rather than leaving it in flight at an estimate.
- **Gemini batches submit inline** and return their results inside the status response, so
  there is no results file. To keep one collection path for every vendor, the batch name is
  carried as the results URL and the operation is fetched a second time. A Gemini batch is
  single-model by construction, because the model is in the URL, and a mixed batch is refused
  rather than silently sent to one of them.
- **A response is never matched to the wrong request.** Anthropic and OpenAI return the
  `custom_id` on every result, so order cannot matter. Gemini's documented shape does not
  promise the key comes back, so the adapter reads `metadata.key` when present and falls back
  to position when it is not. A silently shifted mapping would put one call's usage on another
  call's row, which is the worst thing a cost ledger can do.
- **`batches` on a provider entry**, additive and optional. `openai_compat` defaults to OFF
  because most compatible hosts (Ollama, vLLM) answer `/v1/chat/completions` and have no
  `/v1/batches`; a batch aimed at the local server used to be a confusing parse failure and is
  now a `ConfigError` naming the provider. `anthropic` and `google` default on.
- **A vendor-supplied file id cannot become a path.** The OpenAI results request carries the
  API key and builds its URL from an id the vendor returned, so an id that is not a plain id
  is refused before the request is built. Anthropic's equivalent guard checks a returned URL
  against the configured host; this is the same defence for a shape that has no URL.
- **`chat_body`/`parse_completion` and `generate_body`/`parse_generate` extracted** from the
  OpenAI-compatible and Google adapters, so the batch path builds byte-identical bodies to the
  single-call path and reads usage through the same parser. Tested both ways. No behaviour
  change to either single-call path.
- **Together's batch discount is per model**, which no other vendor here does:
  `meta-llama/Llama-3.3-70B-Instruct-Turbo` runs at half price and `openai/gpt-oss-120b` runs
  at the standard rate, so the price file carries `batch_multiplier: 1.0` for the latter.
  Writing 0.5 there would have understated its invoice by half on every batched call.
- **Price files ship with the library**, at `boundary/prices/`, and a configuration asks for
  them with `prices: builtin`. A directory path still works, for trying a rate before it is
  released. Until now this repository held three dated files and project 02 held a fourth of
  its own, so September's costing could not be reproduced from either repository alone; the
  invoice check found it. A project now pins a version and the version determines the rates.
  02's `2026-09-12.yaml` moved here byte for byte, so nothing it has already costed changes.
  `docs/prices.md` has the reasoning and the steps for moving 02 across.
- **`boundary/prices/2026-09-14.yaml`** supersedes `2026-09-12` and adds the `foundry` provider
  at the standard per-model USD rates, which is what Foundry meters before converting to Claude
  Consumption Units at US$0.01 each. No `batch_multiplier`, because Foundry does not offer the
  Message Batches API and an absent multiplier correctly leaves a batched call uncosted. No
  `vertex` block: Google publishes its own Claude rates and they were not readable from the
  published page on 2026-09-14, so a Vertex call is uncosted until someone reads them.
- **`docs/hyperscaler-setup.md`**, the step-by-step for creating the Foundry resource and the
  Vertex project, and the two things that bite: a Canadian regional endpoint may not serve the
  newest models at all, and the Vertex bearer token expires hourly with no refresh layer built
  yet.
- **`boundary experiment token-estimates`**, Rule C candidate 2, measured over 15,996 real calls
  from project 03's first official drift run. Three estimators against what the vendors actually
  returned. The finding is not that estimates are inaccurate but that they are accurate in
  aggregate and wrong per call: chars/4 gets the month's open-weights input count right to +0.1
  percent while getting the typical call wrong by -31.2 percent. A spend cap is checked per
  call, so an estimator that cancels out over a month is useless for the thing the number is
  for. Written up in `docs/rejected.md`; the costing path is unchanged.
- **`boundary.sqlite-wal` and `-shm` are no longer committed.** `.gitignore` covered
  `*.sqlite` and not its sidecars, so the 2026-09-12 commit carried a write-ahead log for a
  database that is itself ignored: a checkout got a WAL with no database beside it. Untracked,
  and the ignore rule now covers `-wal`, `-shm` and `-journal`. Found while writing the invoice
  check, which turned on exactly this distinction.
- **`docs/invoice-check.md`**, the first ledger-against-invoice check, run early because 03's
  first official run moved from Sep 27 to Sep 13. 70,834 calls across three projects merged into
  one ledger, US$61.3551 to 2026-09-14, reconciling with 03's own independent accounting to
  within US$0.000001 over 35,728 of those calls. The four vendor console figures wait for
  October. Two things the check found about itself are recorded there: gathering ledgers with a
  plain `cp` drops rows still in the write-ahead log, which `ledger merge` itself handles
  correctly and an operator tidying files beforehand does not; and September was priced from two
  repositories, so the costing cannot be reproduced from this one alone.

## 0.2.0 (2026-09-11)

- **Anthropic Message Batches.** `Gateway.batch_submit(requests, purpose=, run_id=)` returns
  a `BatchHandle`; `batch_results(handle, wait_s=, poll_s=)` completes the rows;
  `batch_status(handle)` asks after one without changing anything; `batch_handle(batch_id)`
  rebuilds a handle from the ledger so a batch can be collected by a process that did not
  submit it. Raw HTTP under the pinned `anthropic-version`, no beta header, because batches
  are generally available; a vendor that later wants one takes it from `headers` on the
  provider entry. Standard mode only, and the development cache is never consulted, because
  a cache hit inside a batch would make a row say a request was billed when it was not. The
  `custom_id` sent per request is the row's `call_uid`, so a result maps back to exactly one
  row whatever order the results file is in, and the params of a batched request are byte
  for byte the body a single call would have sent. Brought forward from November: project 02
  built its public-data half on 2026-09-11, seven weeks early, and its own-run panel was the
  thing waiting.
- **Ledger schema v3, additive: `batch_id`.** One nullable column, null for every ordinary
  call. A batch is submitted in one process and collected in another, so the rows written at
  submit have to be findable again by something the vendor also knows. The migration now
  runs one version at a time, so a uid is invented only for a file that predates the column:
  a null `call_uid` in a v2 or later file was put there by hand, and inventing one would let
  the same call merge twice.
- **Batch accounting.** One row per request, written before the submit leaves the process,
  in flight and carrying the estimate at the batch rate, because the vendor bills for every
  request the moment it accepts the batch. The caps are checked once for the whole batch: a
  vendor does not accept half of one. A refused submit completes every row as a failure with
  no cost; an accepted submit whose body cannot be read leaves the rows in flight at their
  estimate, because recording billed work as failed would understate the month. At result
  time each row is completed from the returned usage at the price entry's
  `batch_multiplier`, and an entry with no batch rate leaves the row uncosted rather than
  costed at the full rate. A request the results file never mentions is completed as
  `batch_missing`.
- **Merging never writes to a source.** `ledger merge` used to upgrade a v1 source in place,
  and an older library then refuses to write to the upgraded file by design, so merging an
  environment's ledger would have stopped that environment appending to it. Project 03 pins
  0.1.0 and commits one ledger per arm per month, and the monthly invoice check merges
  exactly those files. The rows are now read from a temporary copy and the copy is what gets
  upgraded; a test asserts the source's bytes are unchanged and that it is still readable and
  writable by the version that wrote it. A v1 source still merges to the same uids, because
  the backfill derives them from the rows rather than inventing them.
- **A ledger that fails to open no longer leaks the file handle**, which on Windows turned a
  clear error about one file into a confusing one about another.
- **The batch results URL is checked against the configured host** before the request that
  fetches it is sent, because that request carries the API key.
- **A spend cap for project 02** in `config/caps.yaml`, so its first own-run call is admitted
  or refused by its own line rather than by the US$10 default. Provisional amounts, to be
  confirmed before that call.
- **Ledger schema v2, additive.** Two columns: `call_uid`, a uid minted in the process that
  makes the call, and `env`, which environment made it. A v1 file is upgraded in place the
  first time this version opens it, with `call_uid` backfilled from each row's own contents
  so that two copies of one v1 file still merge to one row per call. No value a call
  recorded is changed, and no column moved.
- **`boundary ledger merge --into <dest> <sources...>`.** Combines one ledger per
  environment into a central file, matching on `call_uid` and never on `id`. Idempotent:
  merging the same source again inserts nothing, which is what makes "run it again" the
  answer to a merge that failed. A row held as `in_flight` is completed when the source has
  since completed it, so a central file settles on actual costs; a completed row is never
  reverted by an older copy. One source is one transaction. `--dry-run` reports without
  writing.
- **`env` on the gateway and in the configuration.** `Gateway(..., env=...)`, else
  `BOUNDARY_ENV`, else `ledger.env` in `boundary.yaml` (default `local`). Written to every
  ledger row and carried on the span as `boundary.env`. `ledger report` groups by it.
- **Rule C: `docs/rejected.md`.** A central ledger written over the network, measured
  against local-first plus merge over 100 runs with an outage in each, by
  `boundary experiment remote-ledger`. The strict remote design finished none of the runs;
  the best-effort one finished them all and left 8.8% of the calls it had paid for with no
  record, and made 981 calls with no cap check, because the cap cannot be checked when the
  host holding the totals is unreachable. Local-first lost nothing. The table in the doc
  and the figures in the README are filled from `bench/remote-ledger.json` and a test
  fails if they drift.
- The mock upstream and the request corpus behind `boundary bench` moved to
  `boundary/_mock.py` so the experiment measures the same code path against the same
  corpus. The three deterministic bench numbers are unchanged by the move.
- **Live calls, 2026-09-11, which are what the tag waited on.** All four providers answered
  under 0.2 from GitHub Actions. A real two-request Anthropic batch was submitted and polled,
  the vendor reporting its own counts; it had not ended within fifteen minutes, so it was
  collected by a second run in collect mode, from the first run's ledger artefact and with
  nothing carried between them but the batch id. Both rows completed at 16 input and 4 output
  tokens for US$0.000018 each, which is Haiku 4.5's list price times the 0.5 batch
  multiplier, computed from the returned usage and not estimated. Zero errors and zero
  uncosted rows in the report. A local Ollama server answered at price zero, costed rather
  than left unknown. Ten live calls in all this day, well under a cent.

- **A local OpenAI-compatible host, exercised live.** Ollama 0.34.0 with `llama3.2:3b` on
  the laptop, answering through the `local` provider entry at price zero: ledger row 5,
  status 200, 32 input and 2 output tokens, `costed = 1` and cost 0.00, uncosted count
  still zero. It is the one live call that can be made from a network that inspects TLS,
  because nothing leaves the machine. `boundary smoke local` now has that model as its
  default.
- **`boundary smoke <provider> --batch`** submits two short requests as a real vendor batch
  and collects them, so the batch path can be exercised live the way a single call already
  could. `--wait` and `--poll` control how long it will sit there.
- **`boundary batch status <id>` and `boundary batch collect <id>`.** A batch the vendor has
  not finished is the ordinary case, not a failure, so there has to be a way to come back to
  one. `collect` rebuilds the handle from the ledger and completes the rows; `--ledger`
  points at the ledger that submitted it, which for a hosted runner means one restored from
  that run's artefact. `status` exits non-zero until the batch has ended, so a script can
  wait on it. Added after the first live run: the vendor had not finished a two-request batch
  within fifteen minutes, and abandoning it would have meant paying for work with no record
  and no result.
- The smoke workflow gained a collect mode for the same reason, and lost the
  `continue-on-error` on its batch step. A green tick that meant "the batch was submitted"
  when nothing had been collected is worse than a red one.
- **A `smoke` workflow in this repository**, manual only, running one call per vendor and
  the batch path on GitHub's runners, because the laptop's usual network inspects TLS and a
  vendor call from there would pass a personal key and a prompt through an intermediary that
  should see neither. It
  needs the four vendor keys as repository secrets before it will do anything; until now
  those lived only on the release-gate repository.
- A CLI test reached a real local server and wrote to this repository's own ledger once
  `smoke local` gained a default model. It no longer does, and the test that covers "a
  provider with no default model is refused" now uses one that really has none.

Still deferred past the 0.2.0 tag, to 0.2.1 and 0.2.2: the Foundry, Vertex and Bedrock
adapters, which need their accounts and billing alerts first and which nothing is waiting
on. OTLP export arrives with Part B. (All three adapters in fact landed in 0.2.1 together,
written ahead of their accounts; what did not land is a costed row for two of them, and the
reason is a quota wall rather than a schedule.)

Before the 0.2.0 tag: one live batch and one live local call, both recorded in the ledger.
Version stays at `0.2.0.dev0` until then, as `v0.1.0` did until its smoke calls ran.

## 0.1.0 (2026-09-10)

First version of the `boundary` library, Part A of the Compliant AI Gateway. Tagged after
one live call per provider succeeded and was costed (run from GitHub Actions on
2026-09-10: Anthropic, OpenAI, Google, Together; ten calls in all, US$0.0009).

- Adapters over raw HTTP with pinned API version headers, no vendor SDKs: Anthropic
  Messages, OpenAI-compatible chat completions (OpenAI, Together, local servers) and
  Gemini generateContent. Golden request and response tests including error shapes.
- Routing by configuration: aliases redirected by one line in the routes file; explicit
  `provider/model-id` never redirected.
- Two modes. Standard retries 429, 5xx and timeouts with backoff and `Retry-After`, and
  can fill defaults. Pass-through never retries, never caches, never rewrites, requires
  an explicit model and `max_tokens`, and records request and response bytes plus headers
  to a caller-owned raw store; byte equality and single-upstream-call tests.
- Ledger, schema v1, in SQLite. Two-phase rows: inserted before the request leaves with
  the pessimistic estimate, completed with the actual. Cost only from returned usage and a
  dated price file; unknown prices write uncosted rows; `strict_cost` raises instead.
- Spend caps per project month, per run and per portfolio, refused before any request.
- One OpenTelemetry span per call with an attribute allow list enforced in code and no
  content; console exporter. Trace and span ids written to the ledger row.
- Exact-match development cache, standard mode only.
- Live calls from GitHub Actions on 2026-09-10 (the laptop's network inspects TLS): Anthropic,
  OpenAI and Together answered; Google refused `gemini-2.5-flash-lite` as closed to new
  users, so the smoke default is `gemini-3.5-flash-lite`. OpenAI returned a dated id for an
  undated request, which the price list did not know: the ledger now prices by the returned
  id and falls back to the requested id, never further. Price file `2026-09-10.yaml` adds
  the current Gemini and GPT lines. The smoke call sends `reasoning_effort: minimal` to
  OpenAI so a reasoning model produces text within the smoke budget (the gpt-5.4 family
  wants `none` instead; the value belongs to the caller, not the library).
- Gemini `extra` fields under `generationConfig` merge into the built one instead of
  replacing it, so a caller can fix `thinkingConfig` without losing `maxOutputTokens`.
  Found when `gemini-flash-latest` (resolving to `gemini-3.8-flash`) spent a 64-token
  budget on thinking and returned no text.
- TLS verification against the operating system trust store.
- Price files 2026-09-07 (Anthropic list prices from the plan) and 2026-09-09 (Anthropic,
  OpenAI, Google and Together, copied from their price pages).
- Command line: `boundary smoke`, `routes show`, `prices check`, `ledger report`, `bench`.
- Measured against an in-process mock (`boundary bench`): overhead p50 and p95 with
  bootstrap intervals, ledger completeness under injected faults including a kill
  mid-call, cap enforcement, pass-through fidelity. Results in the README and
  `bench/results.json`.

Deferred to 0.2 (October 2026): Foundry, Bedrock and Vertex adapters; Anthropic Message
Batches; `ledger merge` (done, see above); OTLP exporter arrives with Part B.
