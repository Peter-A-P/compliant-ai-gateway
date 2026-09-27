# The semantic cache

`boundary.semcache` (0.25) is PLAN.md B2.5's cache, built and measured before the proxy uses
it. It is not wired into `boundary serve` yet; this page is the evidence for how it will be.

    uv sync --extra cache
    boundary cache paraphrase     # once, US$0.024, already stored in bench/cache/
    boundary cache eval           # offline

## What it does

A request is split into its **scope**, everything but the final user message (model, system
prompt, earlier turns, generation settings), and its **question**, the final user message.
Only the question is embedded, with bge-small (`BAAI/bge-small-en-v1.5`) run locally through
fastembed, so no text leaves the machine to be embedded. A lookup compares the question with
the stored questions **in the same scope** only, and returns the nearest one's answer when the
cosine is at or above the threshold. The store is in memory; pgvector on the VPS replaces it
behind the same interface.

## How it was measured

PLAN.md B2.5 measures on replayed portfolio traffic with 200 hits hand-labelled. That traffic
does not exist yet (the portfolio's calls are evaluation runs, pass-through, never cached),
so this is the measurement that can be made now, labelled by construction on project 03's 100
gold questions:

- The cache holds **half** the questions (every other one). A hit for a question it does not
  hold is false by construction, and so is a hit to the wrong stored question.
- **Paraphrases**: two per question, written once by Haiku 4.5. Of the 40 read before use (every fifth
  question), four changed the meaning (q-021's second, "the length of time before interest
  starts", for "is there an interest-free period"; q-036's second; both of q-046's, "pay off"
  for "change"), and a hit on one of those counts as correct, so the false-hit rate here is a
  floor.
- **Another customer**: `personalise` with two new seeds; the cache holds customer A's
  version, customer B asks. Raw, and redacted by the proxy's rules.
- **Two prompt shapes**: `chat`, the bare question as the message; `retrieval`, 03's answer
  prompt, the regulator page and then the question, as a retrieval pipeline sends it.

**The threshold was chosen on the odd half and reported on the even half**, by a rule written
first: the lowest cosine on a 0.01 grid from 0.70 whose false-hit rate on the odd half's
paraphrases is at most 2%. The odd half alone was run and committed (chat 0.82; retrieval,
none) before the even half was run.

## The result, held-out half

| Prompt shape | Query | Threshold | Hits on questions held | False hits, of all hits |
|---|---|---|---|---|
| chat | paraphrase | 0.82 | 49 of 50, 98.0% (89.5 to 99.6) | 0 of 49, 0.0% (0.0 to 7.3) |
| chat | another customer, raw | 0.82 | 23 of 25, 92.0% (75.0 to 97.8) | 3 of 26, 11.5% (4.0 to 29.0) |
| chat | another customer, redacted | 0.82 | 25 of 25, 100.0% (86.7 to 100.0) | 24 of 49, 49.0% (35.6 to 62.5) |
| retrieval | paraphrase | 0.99, none qualified | 50 of 50 | 20 of 70, 28.6% (19.3 to 40.1) |
| retrieval | another customer, raw | 0.99 | 22 of 25 | 6 of 28, 21.4% (10.2 to 39.5) |
| retrieval | another customer, redacted | 0.99 | 25 of 25 | 10 of 35, 28.6% (16.3 to 45.1) |

What it says:

- **A bare question caches well.** A threshold set on one half carried to the other: 98% of
  paraphrased repeats found, no false hit in 49.
- **A question inside a retrieved page cannot be cached by embedding the message.** No
  threshold on the grid qualified, and at 0.99 more than a quarter of hits were a different
  question about the same page: the page is most of the text, so every question about it
  looks alike. A proxy cannot tell which part of a message is the question. So the cache will
  only ever embed a message the caller marks as a bare question, and a retrieval prompt goes
  to the model every time.
- **Redaction raises the hit rate, as project 07 predicted, and the false-hit rate with it.**
  With a customer's details replaced by placeholders, every customer's preamble reads alike:
  every held question was found, and half of all hits were the wrong question. Raw, the same
  preambles already cost 11.5% false hits at a threshold that gave paraphrases none, so a
  threshold set on clean questions does not transfer to messages with a lot of wrapper around
  them. The data policy's `cache: false` for `personal` keeps redacted payloads out, and this
  is the measurement behind it.
- **Dollars saved** depend on how often real traffic repeats, which only the replay can say.

What this cannot see: 03's questions are one domain and 100 items, the halves 50 each, and
the paraphrases were written by a model (a floor on false hits, above). The hand-labelled 200
hits on replayed traffic in B2.5 are still to come and will be reported beside these.
