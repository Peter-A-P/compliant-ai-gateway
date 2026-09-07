# Working notes for Claude Code

This repository is the Compliant AI Gateway: the `boundary` library that every project in
the portfolio calls models through (Part A, September 2026) and, from May 2027, the full
gateway with redaction, residency routing, audit log, cache, budgets and the portfolio
observability dashboard (Part B). Both are planned in [PLAN.md](PLAN.md).

## Read first

- [README.md](README.md): what this is and the current result tables.
- [PLAN.md](PLAN.md): the design for both parts. Do not deviate from it silently; if
  something in it turns out wrong, change the plan in the same commit as the code and say
  why in the commit message.
- `docs/interface.md` once it exists: the frozen public interface and the version each
  part of it changed in.

## Engineering standard

- Python 3.13. Typed throughout; `mypy --strict` and `ruff` clean in CI.
- Tests that fail meaningfully. Every adapter has golden requests and responses, including
  error shapes. Pass-through has a byte-equality test.
- `pyproject.toml` with pinned major versions and a comment saying why for each pin.
- Docs ship in the same commit as the change.
- Never commit credentials, raw vendor keys, or anything from `.env`.

## Rules specific to this repository

- **Pass-through mode is inviolable.** No retries, no cache, no rewriting, explicit
  provider and model identifier only. Any change to the pass-through path needs a test
  that proves byte equality still holds.
- **Never guess a price.** Cost comes from returned usage and a dated price file. An
  unknown price writes an uncosted row; it never writes an estimate.
- **Every call writes a ledger row before returning**, including failures.
- **No content in telemetry.** Spans and ledger rows carry hashes, counts and identifiers,
  never prompts or completions.
- **The ledger schema is additive.** New columns only; never rename or remove.
- **The public interface is versioned.** Anything the 02 or 03 repositories import
  changes only with a version bump and a note in their plans.
- **No vendor SDKs** for request bodies. Raw HTTP with pinned API version headers; the
  only exceptions are the botocore signer and google-auth for credentials.
- **Every reported score carries a confidence interval.** A bare number is a bug.
- **Plain punctuation** in everything written here: no em-dashes or other typographic
  dashes, straight quotes only.

## What goes in the README

The README opens with the one-liner, the results tables, and the honest limitation, before
any installation instructions. The benchmark and evaluation commands fill the tables; do not
hand-edit them. Record one approach that was tried and rejected, with the evidence, once
the work has produced it.
