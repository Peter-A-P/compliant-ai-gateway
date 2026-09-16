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
