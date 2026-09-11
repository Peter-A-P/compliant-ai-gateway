"""Rule C candidate 3 (PLAN.md section 9): a central remote ledger from day one, instead
of local-first files combined by `ledger merge`.

The claim to test, stated before the run: if every ledger write is a network round trip to
a central host, then an outage during a run either stops the run or loses rows, and
local-first loses nothing because the merge is idempotent. The plan expected that outcome;
this measures it rather than asserting it.

Method. Three designs make the same calls against the same in-process mock upstream, with
the same seeded corpus, and the same simulated outage in each repetition. Nothing touches
the network: the "remote" ledger is the same SQLite store with a fault injected in front
of it, which is generous to the remote design (it has no latency, no partial writes and no
authentication to fail).

    remote-strict       every ledger operation is a call to the central host. A failure
                        reaches the caller, so the run stops. This is the honest version
                        of a remote ledger: it will not proceed without a record.
    remote-best-effort  the same, except a failed ledger operation is logged and
                        swallowed so the run can continue. This is what a team actually
                        writes on the second day, after the first outage stops a run.
    local-first         what boundary does: writes go to a file on the machine making the
                        calls, and `ledger merge` combines the files afterwards. The
                        outage cannot touch it, and the merge runs twice to show that
                        merging again is a no-op.

An outage is a window over *ledger operations*, not over calls, because where it lands
inside a call is the whole question. A window that opens before the pre-call cap check
costs the run a call it never made; one that opens after the request went out costs the
record a cost it can never state, and the vendor still bills for it.

The reported number is the share of calls that reached the vendor whose actual cost never
reached the central record: rows missing outright, plus rows stuck at the pre-call
estimate. It is a per-repetition proportion, so it carries a bootstrap confidence interval
over repetitions.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import statistics
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from boundary import __version__
from boundary._mock import MockUpstream, mock_gateway, sample_request
from boundary.config import BoundaryConfig, load_config
from boundary.errors import BoundaryError
from boundary.ledger.store import IN_FLIGHT, LedgerRow, LedgerStore

ARMS = ("remote-strict", "remote-best-effort", "local-first")
DOC_START = "<!-- experiment:remote-ledger:start -->"
DOC_END = "<!-- experiment:remote-ledger:end -->"


class RemoteLedgerUnavailable(BoundaryError):
    """The central ledger host cannot be reached. What a client library would raise."""


class _RemoteLedger(LedgerStore):
    """A ledger on another host: every operation is a network round trip, and the network
    is down for a window of operations in the middle of the run.

    Backed by a local SQLite file, so the only difference from `LedgerStore` is the
    injected outage. Every way this design loses a row is therefore a property of writing
    the record somewhere the run cannot reach, not of a slow or lossy implementation.
    """

    def __init__(self, path: Path, *, down_from: int, down_until: int, strict: bool) -> None:
        super().__init__(path)
        self.down_from = down_from
        self.down_until = down_until
        self.strict = strict
        self.operations = 0
        self.failed_operations = 0
        self.swallowed_writes = 0
        self.uncapped_checks = 0

    def _network(self) -> bool:
        """Count one operation and say whether it reached the host."""
        i = self.operations
        self.operations += 1
        up = not (self.down_from <= i < self.down_until)
        if not up:
            self.failed_operations += 1
        return up

    def _fail(self) -> None:
        raise RemoteLedgerUnavailable(
            f"central ledger unreachable at operation {self.operations - 1}"
        )

    def begin(self, row: LedgerRow) -> int:
        if self._network():
            return super().begin(row)
        if self.strict:
            self._fail()
        # Best effort: no row, and the call goes ahead anyway.
        self.swallowed_writes += 1
        row.id = None
        return 0

    def complete(self, row: LedgerRow) -> None:
        if self._network():
            if row.id is None:
                # begin() was swallowed, so there is nothing to complete: the outcome of a
                # call that was made has nowhere to go.
                self.swallowed_writes += 1
                return
            super().complete(row)
            return
        if self.strict:
            self._fail()
        self.swallowed_writes += 1

    def spend_usd(
        self,
        *,
        project: str | None,
        year_month: str | None = None,
        run_id: str | None = None,
    ) -> float:
        if self._network():
            return super().spend_usd(project=project, year_month=year_month, run_id=run_id)
        if self.strict:
            self._fail()
        # Best effort: the cap cannot be checked, and the call is made regardless.
        self.uncapped_checks += 1
        return 0.0


@dataclass
class ArmResult:
    """One design over every repetition."""

    arm: str
    repetitions: int
    calls_planned: int
    calls_attempted: int
    calls_reached_vendor: int
    runs_finished: int
    rows_in_central: int
    rows_complete: int
    rows_in_flight: int
    calls_with_no_row: int
    uncapped_calls: int
    merge_second_pass_inserted: int | None = None
    unrecorded_rate_per_run: list[float] = field(default_factory=list)

    @property
    def unrecorded(self) -> int:
        """Calls the vendor will bill whose actual cost the central record cannot state."""
        return self.calls_with_no_row + self.rows_in_flight

    @property
    def unrecorded_share(self) -> float:
        return self.unrecorded / self.calls_reached_vendor if self.calls_reached_vendor else 0.0

    @property
    def finished_share(self) -> float:
        return self.runs_finished / self.repetitions if self.repetitions else 0.0


def _bootstrap_ci(
    values: list[float], stat: Callable[[list[float]], float], *, n: int, rng: random.Random
) -> tuple[float, float]:
    k = len(values)
    if k == 0:
        return (0.0, 0.0)
    samples = sorted(stat([values[rng.randrange(k)] for _ in range(k)]) for _ in range(n))
    return samples[int(0.025 * n)], samples[int(0.975 * n) - 1]


def _run_one(
    arm: str,
    config: BoundaryConfig,
    work: Path,
    *,
    calls: int,
    down_from: int,
    down_until: int,
    seed: int,
    result: ArmResult,
) -> None:
    rng = random.Random(seed)
    upstream = MockUpstream()
    central = work / "central.sqlite"
    remote: _RemoteLedger | None = None
    with mock_gateway(config, work, upstream, project="experiment", env=arm) as gw:
        if arm != "local-first":
            gw.ledger.close()
            remote = _RemoteLedger(
                central,
                down_from=down_from,
                down_until=down_until,
                strict=arm == "remote-strict",
            )
            gw.ledger = remote
        try:
            for i in range(calls):
                result.calls_attempted += 1
                gw.chat(sample_request(i, rng), purpose="experiment", run_id="rc3")
        except RemoteLedgerUnavailable:
            # The strict remote design stops the run rather than proceed unrecorded.
            pass
        else:
            result.runs_finished += 1
        # Whatever happened, this many requests reached the vendor and will be billed.
        made_before_stop = upstream.calls
        result.calls_reached_vendor += made_before_stop
        if remote is not None:
            result.uncapped_calls += remote.uncapped_checks

    if arm == "local-first":
        # The outage never touched the run. Combining the machine's file with the central
        # one is a separate step that can be repeated until it succeeds.
        store = LedgerStore(central)
        try:
            store.merge_from(work / "experiment.sqlite")
            again = store.merge_from(work / "experiment.sqlite")
            result.merge_second_pass_inserted = (result.merge_second_pass_inserted or 0) + (
                again.inserted + again.completed
            )
            rows = store.rows()
        finally:
            store.close()
    else:
        store = LedgerStore(central)
        try:
            rows = store.rows()
        finally:
            store.close()

    in_flight = sum(1 for r in rows if r["error_type"] == IN_FLIGHT)
    result.rows_in_central += len(rows)
    result.rows_in_flight += in_flight
    result.rows_complete += len(rows) - in_flight
    no_row = max(0, made_before_stop - len(rows))
    result.calls_with_no_row += no_row
    if made_before_stop:
        result.unrecorded_rate_per_run.append((no_row + in_flight) / made_before_stop)
    else:
        result.unrecorded_rate_per_run.append(0.0)


@dataclass
class ExperimentResult:
    boundary_version: str
    ran_utc: str
    repetitions: int
    calls_per_run: int
    seed: int
    outage_operations: tuple[int, int]
    arms: dict[str, Any]
    unrecorded_ci: dict[str, tuple[float, float]]

    def doc_rows(self) -> str:
        """The table body for docs/rejected.md. Never written by hand."""
        lines = []
        for arm in ARMS:
            a = self.arms[arm]
            lo, hi = self.unrecorded_ci[arm]
            merged = a["merge_second_pass_inserted"]
            second = "n/a" if merged is None else f"{merged}"
            lines.append(
                f"| `{arm}` "
                f"| {a['runs_finished']}/{a['repetitions']} "
                f"| {a['calls_reached_vendor']:,} "
                f"| {a['calls_with_no_row']:,} "
                f"| {a['rows_in_flight']:,} "
                f"| {a['unrecorded_share'] * 100:.1f}% ({lo * 100:.1f} to {hi * 100:.1f}) "
                f"| {a['uncapped_calls']:,} "
                f"| {second} |"
            )
        return "\n".join(lines)


def run(
    config_path: Path,
    work: Path,
    *,
    repetitions: int = 100,
    calls: int = 40,
    seed: int = 20260910,
    outage_min: int = 4,
    outage_max: int = 30,
) -> ExperimentResult:
    """Run the three arms. Each repetition gives all three the same corpus and the same
    outage window, so the arms differ only in where the record is written."""
    import os

    os.environ.setdefault("ANTHROPIC_API_KEY", "experiment-key")
    config = load_config(config_path)
    work.mkdir(parents=True, exist_ok=True)
    results = {
        arm: ArmResult(
            arm=arm,
            repetitions=repetitions,
            calls_planned=calls,
            calls_attempted=0,
            calls_reached_vendor=0,
            runs_finished=0,
            rows_in_central=0,
            rows_complete=0,
            rows_in_flight=0,
            calls_with_no_row=0,
            uncapped_calls=0,
        )
        for arm in ARMS
    }
    plan = random.Random(seed)
    # One call is a cap check (three spend queries), a begin and a complete: five
    # operations, so an outage placed uniformly over the run lands inside a call as often
    # as between two.
    ops_per_call = 5
    windows: list[tuple[int, int, int]] = []
    for rep in range(repetitions):
        length = plan.randint(outage_min, outage_max)
        start = plan.randrange(ops_per_call, calls * ops_per_call)
        windows.append((start, start + length, seed + rep))
    for arm in ARMS:
        for rep, (start, until, rep_seed) in enumerate(windows):
            _run_one(
                arm,
                config,
                work / arm / str(rep),
                calls=calls,
                down_from=start,
                down_until=until,
                seed=rep_seed,
                result=results[arm],
            )
    boot = random.Random(seed + 7)
    arms_json: dict[str, Any] = {}
    cis: dict[str, tuple[float, float]] = {}
    for arm, r in results.items():
        d = asdict(r)
        d["unrecorded"] = r.unrecorded
        d["unrecorded_share"] = r.unrecorded_share
        d["finished_share"] = r.finished_share
        d["unrecorded_rate_mean"] = statistics.fmean(r.unrecorded_rate_per_run)
        arms_json[arm] = d
        cis[arm] = _bootstrap_ci(r.unrecorded_rate_per_run, statistics.fmean, n=1000, rng=boot)
    return ExperimentResult(
        boundary_version=__version__,
        ran_utc=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        repetitions=repetitions,
        calls_per_run=calls,
        seed=seed,
        outage_operations=(outage_min, outage_max),
        arms=arms_json,
        unrecorded_ci=cis,
    )


def to_json(result: ExperimentResult) -> str:
    return json.dumps(asdict(result), indent=2)


def write_doc(doc: Path, rows: str) -> None:
    text = doc.read_text(encoding="utf-8")
    start = text.index(DOC_START)
    end = text.index(DOC_END)
    text = text[: start + len(DOC_START)] + "\n" + rows + "\n" + text[end:]
    doc.write_text(text, encoding="utf-8")
