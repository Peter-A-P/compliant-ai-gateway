"""A price list's date is not unique. Its rates are, and now a ledger row says which it used.

This exists because of a real near-miss, not a hypothetical one. On 2026-09-18 this library
held `boundary/prices/2026-09-12.yaml` and project 02 held `mselect/config/prices/2026-09-12.yaml`,
byte-different (6,766 against 7,607) and semantically identical. Every ledger row from both
projects said only `price_list: 2026-09-12`. The rates agreed. Nothing in the ledger could
have shown it if they had not, and September's costing spans both.

So the fingerprint hashes the parsed rates, not the file. Comments, key order and line endings
are not rates, and a column that changed when a comment changed would be noise rather than
evidence. The test that matters most here is the last one, which holds the two real files up
against each other.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml

from boundary.config import PriceList, load_price_list, price_files
from boundary.ledger.store import SCHEMA_VERSION

PRICES = Path(__file__).resolve().parent.parent / "boundary" / "prices"


def _list(**rates: dict[str, dict[str, float]]) -> PriceList:
    return PriceList(
        version=1,
        date=dt.date(2026, 9, 12),
        currency="USD",
        source="test",
        per_million_tokens=rates,
    )


# -- what the fingerprint ignores -----------------------------------------------------------


def test_the_same_rates_in_a_different_order_fingerprint_the_same() -> None:
    """Key order is a property of a file, not of a rate."""
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}, "b": {"input": 2.0, "output": 6.0}})
    b = _list(anthropic={"b": {"input": 2.0, "output": 6.0}, "a": {"input": 1.0, "output": 5.0}})
    assert a.rates_sha256 == b.rates_sha256


def test_a_different_source_line_does_not_change_the_fingerprint() -> None:
    """`source` documents where the rates were read from. It is not a rate, and a row costed
    from these rates was costed identically whatever the provenance note says."""
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    b = a.model_copy(update={"source": "somewhere else entirely"})
    assert a.rates_sha256 == b.rates_sha256


def test_the_date_does_not_change_the_fingerprint() -> None:
    """Deliberate. The date is already in `price_list`, and the two columns answer different
    questions: which list was in force, and what was in it."""
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    b = a.model_copy(update={"date": dt.date(2026, 10, 1)})
    assert a.rates_sha256 == b.rates_sha256


# -- what it catches ------------------------------------------------------------------------


def test_a_changed_rate_changes_the_fingerprint() -> None:
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    b = _list(anthropic={"a": {"input": 1.0, "output": 5.01}})
    assert a.rates_sha256 != b.rates_sha256


def test_an_added_model_changes_the_fingerprint() -> None:
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    b = _list(anthropic={"a": {"input": 1.0, "output": 5.0}, "z": {"input": 9.0, "output": 9.0}})
    assert a.rates_sha256 != b.rates_sha256


def test_a_rate_that_moves_provider_changes_the_fingerprint() -> None:
    """The same model id under a different provider is a different route and a different
    price lookup, so it has to be a different fingerprint."""
    a = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    b = _list(openai={"a": {"input": 1.0, "output": 5.0}})
    assert a.rates_sha256 != b.rates_sha256


def test_an_absent_optional_rate_is_not_the_same_as_a_zero_one() -> None:
    """A missing `cache_read` means calls using prompt caching are written uncosted. A zero
    means they are free. Collapsing the two would make an uncosted row look costed, which is
    the exact failure the never-guess-a-price rule exists to prevent."""
    absent = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    zero = _list(anthropic={"a": {"input": 1.0, "output": 5.0, "cache_read": 0.0}})
    assert absent.rates_sha256 != zero.rates_sha256


def test_the_fingerprint_is_a_sha256_and_is_stable_across_calls() -> None:
    pl = _list(anthropic={"a": {"input": 1.0, "output": 5.0}})
    assert len(pl.rates_sha256) == 64
    assert all(c in "0123456789abcdef" for c in pl.rates_sha256)
    assert pl.rates_sha256 == pl.rates_sha256


# -- the shipped files ----------------------------------------------------------------------


def test_every_shipped_price_file_fingerprints_and_they_all_differ() -> None:
    """Each dated file is a repricing, so no two should carry the same rates. Two that did
    would mean a file was added that changed nothing, which is worth noticing."""
    seen: dict[str, str] = {}
    for f in price_files(PRICES):
        pl = load_price_list(f)
        assert len(pl.rates_sha256) == 64
        assert pl.rates_sha256 not in seen, (
            f"{f.name} has the same rates as {seen.get(pl.rates_sha256)}"
        )
        seen[pl.rates_sha256] = f.name
    assert len(seen) >= 4


def test_schema_v5_or_later_ships_the_column() -> None:
    assert SCHEMA_VERSION >= 5


# -- the case this was built for -------------------------------------------------------------

# Project 02 keeps its own copies of two price files. It is a sibling checkout, not a
# dependency, so this is skipped rather than failed when it is not beside this one: a test
# that fails on a machine holding only this repository would be reporting the wrong thing.
SIBLING = (
    Path(__file__).resolve().parents[2]
    / "02-model-selection-tenth-cost"
    / "mselect"
    / "config"
    / "prices"
)


@pytest.mark.parametrize("name", ["2026-09-10.yaml", "2026-09-12.yaml"])
def test_02s_copy_of_a_shared_price_file_has_the_same_rates(name: str) -> None:
    """The near-miss, held open so it cannot close quietly.

    September's ledger was costed from both repositories. If these ever disagree, every row
    citing that date becomes ambiguous and the reconciliation in docs/invoice-check.md is
    worth less than it claims. Today they agree, and `2026-09-12.yaml` agrees only after
    parsing: the two files differ by 841 bytes of comments.
    """
    theirs = SIBLING / name
    if not theirs.is_file():
        pytest.skip(f"project 02 is not checked out beside this repository ({SIBLING})")
    mine = PRICES / name
    assert mine.is_file(), f"{name} should ship with the library"

    a, b = load_price_list(mine), load_price_list(theirs)
    assert a.rates_sha256 == b.rates_sha256, (
        f"{name} names different rates in the two repositories. Every ledger row citing "
        f"price_list={a.name} is now ambiguous about which it used."
    )


def test_the_two_copies_of_2026_09_12_really_are_byte_different() -> None:
    """Guards the premise of the test above rather than the conclusion.

    If somebody makes the two files byte-identical, the parsed comparison above stops proving
    anything interesting and should be replaced by a plain file comparison. This fails on that
    day and says so, instead of leaving a test that passes for a reason nobody intended.
    """
    theirs = SIBLING / "2026-09-12.yaml"
    if not theirs.is_file():
        pytest.skip("project 02 is not checked out beside this repository")
    mine = PRICES / "2026-09-12.yaml"
    if mine.read_bytes() == theirs.read_bytes():
        pytest.skip("the two files are now byte-identical; the parsed comparison is redundant")
    # Different bytes, and the rates still have to match. That is the whole point.
    assert (
        yaml.safe_load(mine.read_text(encoding="utf-8"))["per_million_tokens"]
        == yaml.safe_load(theirs.read_text(encoding="utf-8"))["per_million_tokens"]
    )
