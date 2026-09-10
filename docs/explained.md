# boundary, explained in plain language

This page says what the library does and why, for a reader who does not want to start
from the code. The precise version is [PLAN.md](../PLAN.md); the frozen programming
interface is [interface.md](interface.md).

## Why it exists

This portfolio is ten projects built over a year to show, in public and with numbers a
stranger can check, how production machine learning and AI systems are built. Eight of
the ten call AI models from vendors such as Anthropic, OpenAI and Google.

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
translates it into whatever each vendor expects. Four vendors are covered in version 0.1:
Anthropic, OpenAI, Google, and any host that speaks the OpenAI-compatible protocol, which
is how open-weights models and local servers are reached. The library talks to them over
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

## What happens next

Version 0.1.0 is the library the portfolio's other projects pin. Small additions follow in
October as projects need them: adapters for the three large cloud platforms, the
half-price batch endpoint, and merging ledgers from different machines. In 2027 the same
library becomes the core of the full gateway, which adds reversible redaction of personal
data, routing by data classification, a tamper-evident audit trail and a published latency
budget, the things regulated organisations ask about before they let a model near their
data. Nothing built now is thrown away then.

## What it deliberately does not do

It does not stream, does not type tool calls, does not serve as a network proxy, and does
not inspect or classify content. Version 0 moves bytes and counts money. Each of those
arrives in a later version when a project needs it, and not before.
