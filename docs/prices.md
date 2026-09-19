# Where prices live, and why they ship with the library

A price file is a dated list of USD rates per million tokens, per provider and model. The
ledger costs a call from the returned usage and one of these files, and records which file
priced it. An unknown price writes an uncosted row; the library never fills a rate in.

## One copy, inside the package

The files live at `boundary/prices/YYYY-MM-DD.yaml` and ship in the wheel. A configuration
asks for them with the sentinel:

```yaml
prices: builtin
```

A directory path still works and still resolves against the configuration file, which is how
you try a rate before it is released. It is not how you keep a second copy.

**Why inside the package.** Until 2026-09-14 this repository held `2026-09-07`, `2026-09-09`
and `2026-09-10` while project 02 held its own `2026-09-12` under `mselect/config/prices/`.
Both were correct and neither was complete, so September's costing could not be reproduced
from either repository alone. The first ledger-against-invoice check found it
([invoice-check.md](invoice-check.md)), and it was a gap in the reproducibility claim rather
than in the arithmetic: the numbers were right, and checking them needed two checkouts that
nothing said you needed.

Shipping the files with the library closes it. A project pins a version, and the version
determines the rates, so `boundary==0.2.1` and a ledger are enough to recompute every cost in
it. That is the property this project exists to have.

**What it costs.** Prices change more often than the library does, so a repricing is now a
release. That is the trade, and it is a fair one at this cadence: the invoice check runs
monthly anyway, and a patch release beside it is cheap. It also means a project cannot silently
drift onto rates nobody reviewed, which is the failure the old layout allowed.

## The rules, unchanged

- **Repricing is a new dated file, never an edit to an older one.** A row already costed
  against a file must compute the same forever.
- **Adding a missing rate to an existing file is allowed**, because nothing already costed
  changes. Say so in the file's comments when you do; `2026-09-12.yaml` has an example.
- **The newest file wins.** `latest_price_list` sorts by name, which sorts by date.
- **Every rate names its source** in the comments, with the date it was read. A rate without
  a source is a guess wearing a number's clothes.
- **The date inside the file must match its name**, and loading refuses it otherwise.

## Moving a project onto the packaged files

For project 02, whose `mselect/config/prices/` is the copy this consolidated:

1. Bump the `boundary` pin to a version that carries `2026-09-12.yaml` or later.
2. Set `prices: builtin` in `mselect/config/boundary.yaml`.
3. Delete `mselect/config/prices/`.
4. Run `boundary prices check` and confirm every route still has a rate.

Nothing 02 has already costed changes: `2026-09-12.yaml` moved here byte for byte, so a row
citing that price list resolves to the same numbers it always did.

## Measured rates for self-hosted hosts (0.3)

A host this portfolio runs itself, a vLLM or llama.cpp server on a rented GPU, has no
vendor and no price page. Project 06 derives a rate for one instead: a dated GPU-hour price
divided by a measured throughput at a stated utilisation, expressed in this schema as USD
per million **output** tokens with input at 0, and re-measured for every model,
quantisation format and GPU. That is a finding of 06's, not a list of anyone's, so it does
not ship in this package. It lives beside 06's configuration:

```yaml
# boundary.yaml
providers:
  vllm-l4:
    kind: openai_compat
    base_url: http://10.0.0.7:8000/v1
    self_hosted: true          # rates come from the overlay below and nowhere else
prices: builtin                # the vendor lists, one copy, unchanged
self_hosted_prices: self-hosted-prices   # a directory of dated files, relative to this file
```

```yaml
# self-hosted-prices/2026-10-03.yaml, the same format as a vendor price file
version: 1
date: 2026-10-03
currency: USD
source: "06 load test 2026-10-03, L4 spot $0.39/h checked 2026-10-03, 412 output tok/s at c=32, utilisation 0.5"
per_million_tokens:
  vllm-l4:
    # 0.39 / (412 tok/s x 3600 s x 0.5) x 1e6
    Qwen2.5-7B-Instruct-AWQ:
      input: 0.0
      output: 0.5259
```

Three rules keep "one copy of every vendor price" true with the overlay beside it, and the
gateway refuses at construction, not on the first call, when one is broken:

- **The overlay prices self-hosted providers only.** A provider in it that is not flagged
  `self_hosted: true`, or that the configuration does not know, is a `ConfigError`. A vendor
  rate in a project's own directory is exactly the drift `prices: builtin` exists to prevent.
- **The vendor list may not price a self-hosted provider.** A measured rate in the package
  would be a second copy of a number that changes whenever the GPU does.
- **`self_hosted` and `price_zero` are incompatible.** Free and measured are different
  claims, and an entry cannot make both.

A row costed from the overlay cites the overlay: `price_list` is the overlay file's date and
`price_sha256` its fingerprint, so a self-hosted cost in a merged ledger is audited back to
the measurement run named in that file's `source`, and never mistaken for a vendor rate. A
self-hosted model the overlay does not name is written uncosted, at no neighbour's rate and
not at zero; a self-hosted provider with no overlay configured is uncosted with no list
cited. The pre-call estimate the caps refuse on uses the overlay too, so with input at 0 it
is `max_tokens` at the measured output rate. `boundary prices check` validates and prints
the overlay when the configuration names one, and applies the same refusals.

## What is deliberately absent

`vertex`. Claude on Google Cloud is partner-operated and Google publishes its own rates, which
Anthropic's pricing page defers to rather than restates. Those rates were not readable from the
published page on 2026-09-14, so there are none here and a Vertex call is uncosted. They get
copied in on the day the Google Cloud project exists, into a file dated that day, with the
regional premium accounted for separately. See [hyperscaler-setup.md](hyperscaler-setup.md).

`bedrock`, for the same reason and with two extra traps. AWS publishes its own Claude rates and
Anthropic's pricing page defers to them; they were not readable from the published page on
2026-09-15, so there are none here and a Bedrock call is uncosted. When they are copied in,
from https://aws.amazon.com/bedrock/pricing/ or from the account's own Cost Explorer:

- **Geo and regional endpoints cost 10 percent more than global** for Sonnet 4.5, Haiku 4.5,
  Opus 4.5 and later. `us.anthropic.*` is not the `global.anthropic.*` rate, so the two need
  separate entries even though they name the same model.
- **Bedrock charges appear in Cost Explorer under the model provider, not under Bedrock**,
  because third-party models bill through AWS Marketplace. The monthly invoice check will
  otherwise look in the wrong place and conclude the calls were free.
