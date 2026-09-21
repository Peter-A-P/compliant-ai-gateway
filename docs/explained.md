# boundary, explained in plain language

This page says what the library does and why, for a reader who does not want to start
from the code. The precise version is [PLAN.md](../PLAN.md); the frozen programming
interface is [interface.md](interface.md).

## Why it exists

This portfolio is fifteen projects built over a year to show, in public and with numbers a
stranger can check, how production machine learning and AI systems are built. Most of them
call AI models from vendors such as Anthropic, OpenAI and Google.

If each project called the vendors directly, three things would go wrong. When a vendor
retires or reprices a model, ten codebases would need changing. Nobody could say what the
whole portfolio spent, or on which project. And the project that measures whether vendors
quietly change their "frozen" models from month to month could never prove that our own
code did not distort the measurement.

So the first thing built is a small shared library called `boundary`. Every model call in
the portfolio goes through it. Think of it as the one door in the building: everything
that leaves goes through it, and everything that leaves gets logged.

## What it does

**One way to call any vendor.** A project writes one kind of request, and the library
translates it into whatever each vendor expects. Version 0.1 covered four: Anthropic,
OpenAI, Google, and any host that speaks the OpenAI-compatible protocol, which is how
open-weights models and local servers are reached. Version 0.2.1 added the three large
cloud platforms that resell those models, Microsoft Foundry, Amazon Bedrock and Google
Vertex, which brings it to seven ways in. The library talks to them over
raw HTTP with pinned API version headers rather than through the vendors' own software
kits, because those kits change their behaviour between releases, and a measurement cannot
tolerate that.

**Aliases and explicit names.** A project can ask for `fast` and get whatever the routes
file says `fast` means today. Change one line in that file and every project moves to the
new model. Or a project can name a model exactly, and the library promises never to
redirect it. Measurements use exact names, because a redirect would be a change to the
data.

**Two modes.** Standard mode is for everyday use: it retries on transient errors and fills
in defaults. Pass-through mode is for measurements: no retries, no cache, no rewriting,
exact model only, and a test proves the bytes sent to the vendor are identical to the
bytes the caller built. Pass-through also keeps a raw copy of every request and response,
with the key blanked out, so a measurement can be audited later.

**The ledger.** Every call writes one row to a local database, including failed calls.
The row is written before the request leaves the machine, carrying a deliberately high
cost estimate, and completed afterwards with the real cost. That order matters: if the
process dies mid-call, the row is still there. The real cost is computed only from the
token counts the vendor returns and a dated price file copied from the vendor's own price
page. If the price is not in the file, the row says "uncosted". The library never guesses
a price. Column by column: [ledger.md](ledger.md).

**One ledger per machine, combined afterwards.** The database is a file on whichever
machine made the call: a laptop, a GitHub Actions runner, a small server. `boundary ledger
merge` copies them into one file when a total across all of them is needed, and it is safe
to run twice, because every row carries a uid minted where the call was made. The obvious
alternative, one central database every call writes to over the network, was tried and
measured first: when the network went down it either stopped the run or lost the record of
calls that had already been paid for, and the spend cap stopped working at the same moment,
because a cap can only be checked against a total it can reach. The numbers are in
[rejected.md](rejected.md).

**Spend caps.** Before each call the library adds up this month's spend for the project,
adds the pessimistic estimate for this call, and refuses if the total would pass the cap.
A refused call makes no request. There are caps per project per month, per run, and for
the whole portfolio. These are the second line of defence; the hard caps set in each
vendor's console are the first.

**Telemetry without content.** Each call also emits one trace span with counts, costs,
timings and identifiers, and never the prompt or the answer. The list of allowed fields is
enforced in code, and a test plants a fictional patient's name in a prompt and checks that
it never appears in the output.

**Tests that would fail for a reason.** Golden requests and responses for every vendor,
including error shapes. Byte equality in pass-through. A process killed mid-call still
leaves its ledger row. A cap refusal makes zero upstream calls. A scan of every committed
file for anything shaped like an API key.

## What has been added since

The plan put everything below in 2027. Each one arrived early because a project needed it,
which is the rule this library is maintained by: a feature arrives when something is
waiting for it, and not before.

The three cloud platforms and the half-price batch endpoint came in 0.2. Streaming, with
the time to the first token recorded, came in 0.3 for the project that load-tests model
servers. A **data class** the caller declares on every call came in 0.4, so the record can
answer which calls carried personal data and where they went; the library records that
declaration and never guesses one.

**Reversible redaction** came in 0.5, two years before the plan expected it, because the
access-to-information project is building on it now. Personal values in a document are
replaced by typed placeholders (`<PERSON_1>`, `<HEALTH_NUMBER_2>`) before the text is sent,
and the real values are put back in the answer. The library refuses to send rather than
warn: if anything it recognises is still in the text after redaction, the call does not
happen. That engine has been through nine releases in two days, almost all of them fixing
something the other project found by using it, and the reasoning behind each is in
[redact.md](redact.md).

What is still ahead: the network proxy itself, routing by data class rather than merely
recording it, the tamper-evident audit trail, the cache, per-team budgets and the published
latency budget. Nothing built now is thrown away then.

## What it deliberately does not do

It does not serve as a network proxy, does not type tool calls, and does not decide what
kind of data a request carries: the caller declares that, because a library that guessed
would be making a compliance decision nobody reviewed. It reads content only where a caller
hands it text to redact, and it never puts content in the record: spans and ledger rows
carry counts, costs, timings and identifiers, never prompts or answers.
