"""Command line: boundary smoke <provider> | routes show | prices check |
ledger report | ledger residency | ledger merge | batch status | batch collect | bench |
redact eval | audit seal | audit verify | audit anchor | audit tamper-test | policy eval |
serve | teams key | experiment remote-ledger.

Every command takes --config (default: config/boundary.yaml next to the current directory
or the installed package's config) and --project.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from boundary import __version__
from boundary.config import (
    BoundaryConfig,
    PriceList,
    Residency,
    check_price_lists,
    latest_price_list,
    load_config,
    load_price_list,
    price_files,
)
from boundary.errors import BatchNotReady, BoundaryError
from boundary.gateway import Gateway
from boundary.ledger import data_class as data_class_filter
from boundary.ledger import residency as residency_report
from boundary.ledger.store import LedgerStore
from boundary.types import ChatRequest, Mode

SMOKE_MODELS: dict[str, str] = {
    # One cheap model per provider entry in the checked-in configuration. A different
    # entry name or model can be given with --model.
    "anthropic": "anthropic/claude-haiku-4-5-20251001",
    "openai": "openai/gpt-5-nano",
    # gemini-2.5-flash-lite is closed to new API users (404 on 2026-09-10).
    "google": "google/gemini-3.5-flash-lite",
    "openweights": "openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    # The `us.` prefix is a geographic inference profile, not decoration: it decides where
    # the request may be processed, and the provider entry declares `residency: geo` to
    # match it. Changing this string changes the residency. See providers/bedrock.py.
    "bedrock": "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    # The `@` is the Model Garden "Version name" and belongs in the URL unencoded. The
    # undated `claude-haiku-4-5` also resolves; the dated form is used here because it says
    # which weights answered, and the ledger records what was requested.
    "vertex": "vertex/claude-haiku-4-5@20251001",
    # The deployment name in the Canada Central Foundry resource. gpt-4o-mini turned out not
    # to be offered in canadacentral (only canadaeast), which is another region-availability
    # finding; gpt-5.6-luna is, and is cheaper, so it is what is deployed there.
    "foundry-canada": "foundry-canada/gpt-5.6-luna",
    # A local server at price zero. Nothing leaves the machine, so this is the one smoke
    # call that can run from a network that inspects TLS. `ollama pull llama3.2:3b` first.
    "local": "local/llama3.2:3b",
}
# Vendor fields the smoke call needs to produce visible text within its small max_tokens.
# OpenAI's gpt-5 family reasons first and spends the whole budget on it otherwise.
SMOKE_EXTRA: dict[str, dict[str, object]] = {
    "openai": {"reasoning_effort": "minimal"},
}


def _default_config() -> Path:
    here = Path.cwd() / "config" / "boundary.yaml"
    if here.is_file():
        return here
    return Path(__file__).resolve().parent.parent / "config" / "boundary.yaml"


def cmd_smoke(args: argparse.Namespace) -> int:
    model = args.model or SMOKE_MODELS.get(args.provider)
    if model is None:
        print(
            f"no default smoke model for provider {args.provider!r}; pass --model provider/model-id",
            file=sys.stderr,
        )
        return 2
    if args.batch:
        return _smoke_batch(args, model)
    request = ChatRequest(
        model=model,
        messages=[{"role": "user", "content": "Reply with the single word OK."}],
        # No temperature: reasoning models accept only their default, and a smoke
        # call is about the plumbing, not the sampling.
        max_tokens=64,
        extra=SMOKE_EXTRA.get(args.provider, {}),
    )
    with Gateway.from_config(args.config, project=args.project) as gw:
        if args.stream:
            resp = gw.chat_stream(request, purpose="smoke-stream", run_id=args.run_id)
        else:
            resp = gw.chat(request, purpose="smoke", run_id=args.run_id, mode=Mode.STANDARD)
    cost = f"US${resp.cost_usd:.6f}" if resp.cost_usd is not None else "uncosted"
    ttft = f", first token {resp.ttft_ms:.0f} ms" if resp.ttft_ms is not None else ""
    print(
        f"{resp.provider}: status {resp.status}, model {resp.model_returned}, "
        f"text {resp.text!r}, tokens {resp.usage.input_tokens}/{resp.usage.output_tokens}, "
        f"{cost}, {resp.latency_ms:.0f} ms{ttft}, retries {resp.retries}, "
        f"ledger row {resp.ledger_id}"
    )
    return 0 if resp.ok else 1


def _smoke_batch(args: argparse.Namespace, model: str) -> int:
    """One tiny real batch: submit, wait, collect. Exercises the whole path the way a single
    smoke call exercises the single-request path, for a couple of hundred tokens."""
    with Gateway.from_config(args.config, project=args.project) as gw:
        requests = [
            ChatRequest(
                model=model,
                messages=[{"role": "user", "content": f"Reply with the single word OK ({n})."}],
                max_tokens=16,
                extra=SMOKE_EXTRA.get(args.provider, {}),
            )
            for n in (1, 2)
        ]
        handle = gw.batch_submit(requests, purpose="smoke-batch", run_id=args.run_id)
        print(
            f"submitted batch {handle.batch_id}: {len(handle)} request(s), "
            f"ledger rows {list(handle.ledger_ids)}"
        )
        try:
            responses = gw.batch_results(handle, wait_s=args.wait, poll_s=args.poll)
        except BatchNotReady as e:
            print(f"{e}", file=sys.stderr)
            print(
                f"the rows stay in flight; collect it later with the batch id {handle.batch_id}",
                file=sys.stderr,
            )
            return 1
        for r in responses:
            cost = f"US${r.cost_usd:.6f}" if r.cost_usd is not None else "uncosted"
            print(
                f"  {r.status} model {r.model_returned} text {r.text!r} "
                f"tokens {r.usage.input_tokens}/{r.usage.output_tokens} {cost} row {r.ledger_id}"
            )
    return 0 if all(r.ok for r in responses) else 1


def _batch_gateway(args: argparse.Namespace) -> Gateway:
    """A gateway pointed at the ledger that holds the batch.

    A batch is collected from the ledger that submitted it, which for a run on a hosted
    runner means a ledger restored from that run's artefact rather than the configured one.
    """
    return Gateway.from_config(
        args.config,
        project=args.project,
        ledger_path=Path(args.ledger) if args.ledger else None,
    )


def cmd_batch_status(args: argparse.Namespace) -> int:
    with _batch_gateway(args) as gw:
        handle = gw.batch_handle(args.batch_id)
        progress = gw.batch_status(handle)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(progress.counts.items()))
    print(
        f"{progress.batch_id}: {progress.processing_status}"
        f"{' (ended)' if progress.ended else ''}, {len(handle)} request(s)"
        + (f", {counts}" if counts else "")
    )
    return 0 if progress.ended else 1


def cmd_batch_collect(args: argparse.Namespace) -> int:
    """Complete the ledger rows of a batch submitted earlier, possibly by another process.

    This is the path the library exists to make safe: the batch id is the only thing the
    caller kept, and everything else is rebuilt from the ledger.
    """
    with _batch_gateway(args) as gw:
        handle = gw.batch_handle(args.batch_id)
        print(f"{handle.batch_id}: {len(handle)} request(s), ledger rows {list(handle.ledger_ids)}")
        try:
            responses = gw.batch_results(handle, wait_s=args.wait, poll_s=args.poll)
        except BatchNotReady as e:
            print(f"{e}", file=sys.stderr)
            return 1
        total = 0.0
        for r in responses:
            cost = f"US${r.cost_usd:.6f}" if r.cost_usd is not None else "uncosted"
            total += r.cost_usd or 0.0
            print(
                f"  {r.status} model {r.model_returned} text {r.text!r} "
                f"tokens {r.usage.input_tokens}/{r.usage.output_tokens} {cost} row {r.ledger_id}"
            )
        print(f"collected {len(responses)} request(s) at the batch rate, US${total:.6f} in all")
    return 0 if all(r.ok for r in responses) else 1


def cmd_routes_show(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    width = max(len(a) for a in cfg.routes) if cfg.routes else 5
    for alias, route in sorted(cfg.routes.items()):
        extra = []
        if route.api_version:
            extra.append(f"api {route.api_version}")
        if route.region:
            extra.append(f"region {route.region}")
        print(f"{alias:<{width}}  -> {route.provider}/{route.model}  {' '.join(extra)}".rstrip())
    print()
    for name, pc in sorted(cfg.providers.items()):
        key = pc.api_key_env or "no key"
        print(f"provider {name:<12} {pc.kind.value:<14} {pc.base_url}  ({key})")
    return 0


def _print_rates(pl: PriceList) -> None:
    for provider, models in sorted(pl.per_million_tokens.items()):
        for model, e in sorted(models.items()):
            cache = (
                f", cache read {e.cache_read} write {e.cache_write}"
                if e.cache_read is not None or e.cache_write is not None
                else ""
            )
            batch = f", batch x{e.batch_multiplier}" if e.batch_multiplier is not None else ""
            print(f"  {provider}/{model}: in {e.input} out {e.output}{cache}{batch}")


def _self_hosted_check(cfg: BoundaryConfig) -> PriceList | None:
    """Validate and print the self-hosted overlay when the configuration names one."""
    if cfg.self_hosted_prices is None:
        return None
    files = price_files(cfg.self_hosted_prices)
    if not files:
        print(f"no self-hosted price files in {cfg.self_hosted_prices}", file=sys.stderr)
        return None
    for f in files:
        load_price_list(f)
    overlay = latest_price_list(cfg.self_hosted_prices)
    print(
        f"self-hosted price list: {overlay.name} ({len(files)} file(s)); source: {overlay.source}"
    )
    _print_rates(overlay)
    return overlay


def cmd_prices_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    files = price_files(cfg.prices)
    if not files:
        print(f"no price files in {cfg.prices}", file=sys.stderr)
        return 1
    latest = latest_price_list(cfg.prices)
    today = dt.date.today()
    stale = (latest.date.year, latest.date.month) != (today.year, today.month)
    print(f"latest price list: {latest.name} ({len(files)} file(s)); source: {latest.source}")
    if stale:
        print(
            f"warning: the latest price file is from {latest.date:%B %Y}; repricing is a new dated file",
            file=sys.stderr,
        )
    _print_rates(latest)
    overlay = _self_hosted_check(cfg)
    # The same refusals the gateway applies at construction, so a misplaced rate is found
    # here, by the command whose job is to look, and not by the first call of a run.
    check_price_lists(cfg, latest, overlay)

    def priced(provider: str, model: str) -> bool:
        pc = cfg.providers[provider]
        if pc.price_zero:
            return True
        source = overlay if pc.self_hosted else latest
        return source is not None and source.lookup(provider, model) is not None

    missing = [
        f"{alias} -> {r.provider}/{r.model}"
        for alias, r in cfg.routes.items()
        if not priced(r.provider, r.model)
    ]
    if missing:
        print("routes without a price (calls will be uncosted):", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
    for f in files:
        load_price_list(f)  # validates every file, not only the latest
    return 1 if (stale or missing) else 0


def _open_ledger(args: argparse.Namespace) -> LedgerStore | None:
    """The ledger a read-only command should read, or None after saying why not."""
    cfg = load_config(args.config)
    path = Path(args.ledger) if args.ledger else cfg.ledger.path
    if not path.is_file():
        print(f"no ledger at {path}", file=sys.stderr)
        return None
    return LedgerStore(path)


def _data_class_arg(args: argparse.Namespace, rows: Sequence[Mapping[str, Any]]) -> str | None:
    """The validated `--data-class` filter, or None. A word that matches nothing anywhere is
    refused rather than filtered on, because an empty report reads as "no such calls"; a
    class this version does not know but the ledger holds is allowed, because an auditor has
    to be able to ask about the rows in front of them."""
    wanted = getattr(args, "data_class", None)
    if not wanted:
        return None
    return data_class_filter.check_filter(wanted, data_class_filter.present_in(rows))


def cmd_ledger_report(args: argparse.Namespace) -> int:
    store = _open_ledger(args)
    if store is None:
        return 1
    try:
        all_rows = store.rows()
        wanted = _data_class_arg(args, all_rows)
    except ValueError as bad_filter:
        store.close()
        print(f"error: {bad_filter}", file=sys.stderr)
        return 2
    try:
        rows = all_rows
        month = args.month
        if month:
            rows = [r for r in rows if str(r["ts_utc"]).startswith(month)]
        rows = [r for r in rows if data_class_filter.matches(r, wanted)]
        # The declared class is part of the key rather than a filter only, so that the
        # ordinary report already says which calls carried personal data and which made no
        # claim at all, without anybody having to think to ask.
        by: dict[tuple[str, str, str, str, str], dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "uncosted": 0, "cost": 0.0, "in": 0, "out": 0}
        )
        for r in rows:
            key = (
                str(r["ts_utc"])[:7],
                str(r["env"] or "-"),
                str(r["project"]),
                data_class_filter.label(r.get("data_class")),
                str(r["model_requested"]),
            )
            b = by[key]
            b["calls"] += 1
            b["in"] += int(r["input_tokens"] or 0)
            b["out"] += int(r["output_tokens"] or 0)
            if r["error_type"]:
                b["errors"] += 1
            elif not r["costed"]:
                b["uncosted"] += 1
            if r["cost_usd"] is not None and r["costed"]:
                b["cost"] += float(r["cost_usd"])
        # Wide enough for the longest project name present, so one long name does not push
        # every column out of line and make the table unreadable. The plan's project names
        # run to 31 characters.
        pw = max(24, *(len(key[2]) for key in by)) if by else 24
        print(
            f"{'month':<8} {'env':<8} {'project':<{pw}} {'class':<10} {'model':<44} {'calls':>6} {'err':>4} {'unc':>4} {'in':>9} {'out':>8} {'USD':>10}"
        )
        total = 0.0
        for (m, e, p, dc, model), b in sorted(by.items()):
            total += b["cost"]
            print(
                f"{m:<8} {e:<8} {p:<{pw}} {dc:<10} {model:<44} {int(b['calls']):>6} {int(b['errors']):>4} "
                f"{int(b['uncosted']):>4} {int(b['in']):>9} {int(b['out']):>8} {b['cost']:>10.4f}"
            )
        print(
            f"{'total':<8} {'':<8} {'':<{pw}} {'':<10} {'':<44} {len(rows):>6} {'':>4} {store.uncosted_count():>4} {'':>9} {'':>8} {total:>10.4f}"
        )
        if wanted is not None:
            print(f"filtered to data class {wanted}: {len(rows)} row(s)")
        in_flight = sum(1 for r in rows if r["error_type"] == "in_flight")
        if in_flight:
            print(
                f"warning: {in_flight} row(s) still in_flight (process killed mid-call)",
                file=sys.stderr,
            )
    finally:
        store.close()
    return 0


def cmd_ledger_residency(args: argparse.Namespace) -> int:
    """Where the data went, and whether it stayed inside a declared limit.

    `ledger report` answers the spend question. This answers the compliance one, off the same
    rows. `--require` turns it from a report into a gate: a run can fail its own CI for
    sending data further than it said it would, which is the only version of this feature
    worth having.
    """
    store = _open_ledger(args)
    if store is None:
        return 1
    try:
        rows = store.rows()
        wanted = _data_class_arg(args, rows)
        groups = residency_report.summarise(
            rows, month=args.month, project=args.project_filter, data_class=wanted
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        store.close()

    if wanted is not None:
        print(f"calls whose caller declared data class {wanted}:")
    if not groups:
        print("no rows match")
        return 0

    print(
        f"{'reach':<14} {'provider':<16} {'region':<20} {'calls':>6} {'cached':>7} {'err':>4} "
        f"{'in':>9} {'out':>8}  models"
    )
    for g in groups:
        print(
            f"{g.residency:<14} {g.provider:<16} {g.region:<20} {g.calls:>6} {g.cached:>7} "
            f"{g.errors:>4} {g.input_tokens:>9} {g.output_tokens:>8}  {', '.join(g.models)}"
        )

    # Said in the output and not only in the docs, because the person who can misread a clean
    # report is the one running the command.
    print()
    print(
        "residency is what the provider entry declared, not what the vendor reported:\n"
        "no vendor reports where a request was processed. region is where it was sent."
    )

    undeclared = [g for g in groups if g.residency == residency_report.UNDECLARED]
    if undeclared and args.require is None:
        calls = sum(g.calls for g in undeclared)
        print(
            f"note: {calls} call(s) across {len(undeclared)} provider/region pair(s) declared "
            "no residency at all."
        )

    if args.require is None:
        return 0

    limit = Residency(args.require)
    bad = residency_report.violations(groups, limit)
    if not bad:
        print(f"ok: every call was within {limit.value}")
        return 0
    print(file=sys.stderr)
    print(f"FAIL: {len(bad)} group(s) not within {limit.value}", file=sys.stderr)
    for g in bad:
        why = (
            "no residency declared"
            if g.residency == residency_report.UNDECLARED
            else (
                f"{g.residency!r} is not a residency this version knows"
                if not residency_report.is_known(g.residency)
                else f"{g.residency} is wider than {limit.value}"
            )
        )
        print(f"  {g.provider}/{g.region}: {g.sent} call(s) sent, {why}", file=sys.stderr)
    return 2


def cmd_ledger_merge(args: argparse.Namespace) -> int:
    """Combine per-environment ledgers into one file. Idempotent: a second run of the same
    sources inserts nothing, which is the property the local-first design rests on."""
    cfg = load_config(args.config)
    dest = Path(args.into) if args.into else cfg.ledger.path
    store = LedgerStore(dest)
    try:
        before = store.count()
        total_inserted = total_completed = total_skipped = 0
        for src in args.sources:
            stats = store.merge_from(Path(src), dry_run=args.dry_run)
            print(stats)
            total_inserted += stats.inserted
            total_completed += stats.completed
            total_skipped += stats.skipped
        after = store.count()
        verb = "would hold" if args.dry_run else "holds"
        print(
            f"{dest}: {before} row(s) before, {verb} {before + total_inserted} after "
            f"({total_inserted} inserted, {total_completed} completed, {total_skipped} already held)"
        )
        if not args.dry_run and after != before + total_inserted:
            print(
                f"error: {dest} holds {after} rows, expected {before + total_inserted}",
                file=sys.stderr,
            )
            return 1
    finally:
        store.close()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    import tempfile

    from boundary import bench

    with tempfile.TemporaryDirectory(prefix="boundary-bench-") as tmp:
        results = bench.run(
            args.config,
            Path(tmp),
            calls=args.calls,
            per_fault=args.per_fault,
            fidelity_requests=args.fidelity,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(bench.to_json(results) + "\n", encoding="utf-8")
    print(results.readme_row())
    if args.write_readme:
        readme = args.config.resolve().parent.parent / "README.md"
        bench.write_readme(readme, results.readme_row())
        print(f"README row written to {readme}")
    return 0


def cmd_redact_eval(args: argparse.Namespace) -> int:
    """Measure boundary.redact against a corpus generated from a seed.

    No key, no account, no network and no model: the corpus is code and the engine is
    local, which is what makes this the one measurement in this repository that a stranger
    can reproduce with nothing but a checkout.
    """
    from boundary.redact import evaluate

    extra: list[object] = []
    detector = "built-in recognisers only"
    if args.presidio:
        from boundary.redact.presidio import PresidioRecogniser

        extra.append(PresidioRecogniser())
        detector = "built-in recognisers and Presidio"
    if args.tab:
        return _redact_eval_tab(args)
    if args.rehydration:
        rehy = evaluate.rehydration(pages=args.pages, seed=args.seed)
        print(rehy.table())
        return 0
    if args.identifiers:
        ident = evaluate.identifiers(
            per_family=args.per_family,
            seed=args.seed,
            extra=extra,  # type: ignore[arg-type]
            detector=detector,
        )
        print(ident.table())
        return 0
    results = evaluate.run(
        pages=args.pages,
        seed=args.seed,
        extra=extra,  # type: ignore[arg-type]
        detector=detector,
    )
    print(results.table())
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(evaluate.to_json(results) + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    if args.write_readme:
        readme = args.config.resolve().parent.parent / "README.md"
        evaluate.write_readme(readme, results.readme_row())
        print(f"README row written to {readme}")
    return 0


def _redact_eval_tab(args: argparse.Namespace) -> int:
    """The Text Anonymization Benchmark: real court judgments, public, MIT licence.

    Downloads the test split once into .cache/tab at a pinned commit and refuses a file
    whose SHA-256 differs. Built-in recognisers always; Presidio as a second configuration
    when --presidio is given, because the README reports the two side by side.
    """
    import json

    from boundary.redact import tab

    docs = tab.load(tab.fetch())
    allow: list[str] = []
    if args.tab_allow == "train":
        allow = tab.derive_allow(tab.fetch("train"))
    elif args.tab_allow:
        allow = tab.read_allow(Path(args.tab_allow))
    configs: list[tuple[str, list[object]]] = [("built-in recognisers only", [])]
    if args.presidio:
        from boundary.redact.presidio import PresidioRecogniser

        configs.append(("built-in recognisers and Presidio", [PresidioRecogniser()]))
    results = []
    for detector, extra in configs:
        lists: list[tuple[list[str], str]] = [([], "")]
        if allow:
            lists.append((allow, f", allow list of {len(allow):,}"))
        for terms, label in lists:
            r = tab.run(docs, extra=extra, detector=detector + label, allow=terms)  # type: ignore[arg-type]
            print(r.table())
            print()
            results.append(r)
    if args.out is not None:
        out = args.out.with_name("redact-tab.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(tab.to_json(r)) for r in results]
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"written to {out}")
    if args.write_readme:
        if not args.presidio:
            print("the README reports both configurations; add --presidio", file=sys.stderr)
            return 2
        readme = args.config.resolve().parent.parent / "README.md"
        tab.write_readme(readme, [r.readme_cells() for r in results])
        print(f"README rows written to {readme}")
    return 0


MUTATION_MODELS = (
    "anthropic/claude-haiku-4-5-20251001",
    "openweights/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "google/gemini-3.5-flash-lite",
)


def cmd_redact_mutation(args: argparse.Namespace) -> int:
    """The rehydration mutation rate under real models (0.14). Collecting calls vendors and
    costs money, capped by --max-usd; --score re-reads a stored run with no call at all."""
    import asyncio

    from boundary.redact import mutation

    if args.score is not None:
        run = mutation.read(args.score)
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        gw = Gateway.from_config(args.config, project=args.project)
        try:
            run = asyncio.run(
                mutation.collect(
                    gw,
                    models,
                    pages=args.pages,
                    run_id=args.run_id,
                    max_usd=args.max_usd,
                )
            )
        finally:
            gw.close()
        mutation.write(args.out, run)
        spent = sum(c.cost_usd or 0.0 for c in run.calls)
        print(f"{len(run.calls)} calls, US${spent:.4f}, written to {args.out}", file=sys.stderr)
    scored = mutation.score(run)
    print(scored.table())
    if args.write_readme:
        readme = args.config.resolve().parent.parent / "README.md"
        mutation.write_readme(readme, scored.readme_rows())
        print(f"README rows written to {readme}")
    return 0


def cmd_policy_eval(args: argparse.Namespace) -> int:
    """The adversarial suite for the data policy, over this configuration's own providers
    and aliases. In process, against a mock upstream: no network, no keys, no money."""
    from boundary import enforce_eval

    cfg = load_config(args.config)
    policy = Path(args.policy) if args.policy else args.config.resolve().parent / "policy.yaml"
    if args.proxy:
        results = enforce_eval.run_proxy(cfg, policy, models=SMOKE_MODELS)
    else:
        results = enforce_eval.run(cfg, policy, models=SMOKE_MODELS)
    print(results.table())
    if args.write_readme:
        readme = args.config.resolve().parent.parent / "README.md"
        enforce_eval.write_readme(readme, results.readme_row(), proxy=args.proxy)
        print(f"README row written to {readme}")
    return 0 if results.violations.hits == 0 and results.false_refusals.hits == 0 else 1


PROXY_LEDGER = "boundary.proxy.sqlite"


def cmd_serve(args: argparse.Namespace) -> int:
    """The OpenAI-compatible proxy (0.13). Needs the `server` extra. Binds to loopback
    unless told otherwise: a proxy holding vendor keys is not put on a network by default."""
    try:
        import uvicorn

        from boundary.server import create_app, load_teams
    except ImportError:
        print(
            "error: the proxy needs the server extra: uv sync --extra server "
            "(or pip install 'boundary[server]')",
            file=sys.stderr,
        )
        return 2
    from boundary.enforce import load_policy
    from boundary.env import find_dotenv, load_dotenv

    env_file = find_dotenv(Path.cwd(), args.config.resolve().parent.parent, args.config.parent)
    if env_file is not None:
        load_dotenv(env_file)
    cfg = load_config(args.config)
    teams_path = Path(args.teams) if args.teams else args.config.resolve().parent / "teams.yaml"
    policy_path = Path(args.policy) if args.policy else cfg.policy
    if policy_path is None:
        policy_path = args.config.resolve().parent / "policy.yaml"
    # A ledger of its own by default, beside the library's: the gateway's monthly ceiling is
    # the spend of every row in the file, and the library's own calls are not the teams'.
    ledger = Path(args.ledger) if args.ledger else cfg.ledger.path.with_name(PROXY_LEDGER)
    app = create_app(
        cfg,
        load_teams(teams_path),
        ledger_path=ledger,
        policy=load_policy(policy_path),
    )
    print(
        f"boundary {__version__} proxy on http://{args.host}:{args.port}/v1 "
        f"(teams {teams_path}, policy {policy_path}, ledger {ledger})",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_teams_key(args: argparse.Namespace) -> int:
    """Mint a key for a team. The key is printed once and stored nowhere; the hash is what
    goes into teams.yaml."""
    from boundary.server.teams import hash_key, new_key

    key = new_key()
    print(f"key for team {args.team} (shown once, give it to the team and keep no copy):")
    print(f"  {key}")
    print("add this line under the team's key_sha256 in teams.yaml:")
    print(f"  - {hash_key(key)}")
    return 0


def _audit_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    """The ledger and the audit log beside it. The log defaults to `<ledger>.audit.sqlite`,
    so one ledger has one chain and a merge destination gets its own."""
    cfg = load_config(args.config)
    ledger = Path(args.ledger) if args.ledger else cfg.ledger.path
    audit = Path(args.audit) if args.audit else ledger.with_name(ledger.stem + ".audit.sqlite")
    return ledger, audit


def cmd_audit_seal(args: argparse.Namespace) -> int:
    from boundary.audit import AuditLog

    ledger_path, audit_path = _audit_paths(args)
    if not ledger_path.is_file():
        print(f"no ledger at {ledger_path}", file=sys.stderr)
        return 1
    store = LedgerStore(ledger_path)
    try:
        rows = store.rows()
    finally:
        store.close()
    with AuditLog(audit_path) as log:
        stats = log.seal(rows, settle_s=args.settle_hours * 3600)
    print(f"{audit_path}: {stats}")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from boundary.audit import AuditLog, read_anchors, verify

    ledger_path, audit_path = _audit_paths(args)
    if not audit_path.is_file():
        print(f"no audit log at {audit_path}", file=sys.stderr)
        return 1
    with AuditLog(audit_path) as log:
        records = log.records()
    anchors = read_anchors(Path(args.anchors)) if args.anchors else []
    rows = None
    if not args.no_ledger:
        if not ledger_path.is_file():
            print(f"no ledger at {ledger_path}; pass --no-ledger to check the chain alone")
            return 1
        store = LedgerStore(ledger_path)
        try:
            rows = store.rows()
        finally:
            store.close()
    result = verify(records, anchors, ledger_rows=rows)
    print(result.summary())
    return 0 if result.ok else 1


def cmd_audit_anchor(args: argparse.Namespace) -> int:
    from boundary.audit import AuditLog, append_anchor

    _, audit_path = _audit_paths(args)
    if not audit_path.is_file():
        print(f"no audit log at {audit_path}", file=sys.stderr)
        return 1
    with AuditLog(audit_path) as log:
        anchor = log.anchor()
    if anchor.seq == 0:
        print("the log is empty; there is nothing to anchor", file=sys.stderr)
        return 1
    append_anchor(Path(args.anchors), anchor)
    print(f"anchored seq {anchor.seq} head {anchor.head} in {args.anchors}")
    return 0


def cmd_audit_tamper_test(args: argparse.Namespace) -> int:
    """Corrupt a generated chain every way an operator could and count what is detected.

    In memory, from a seed: no ledger of anybody's and no network, like `redact eval`.
    """
    from boundary.audit import tamper

    results = tamper.run(
        records=args.records,
        anchor_interval=args.anchor_interval,
        trials=args.trials,
        seed=args.seed,
    )
    print(results.table())
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(tamper.to_json(results) + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    if args.write_readme:
        readme = args.config.resolve().parent.parent / "README.md"
        tamper.write_readme(readme, results.readme_row())
        print(f"README row written to {readme}")
    if args.write_doc:
        doc = args.config.resolve().parent.parent / "docs" / "audit.md"
        tamper.write_doc(doc, results.doc_block())
        print(f"table written to {doc}")
    return 0


def cmd_experiment_token_estimates(args: argparse.Namespace) -> int:
    """Rule C candidate 2: a local token estimate against the vendor's returned usage."""
    from boundary.experiment import token_estimates

    result = token_estimates.run(args.run_dir, bootstrap=args.bootstrap)
    if not result.tiktoken_available:
        print(
            "note: tiktoken is not installed, so the strongest-case row is missing "
            "(uv sync --group dev)",
            file=sys.stderr,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(token_estimates.to_json(result) + "\n", encoding="utf-8")
    rows = token_estimates.format_rows(result)
    print(f"{result.calls_scored} call(s) scored from {result.run_id or args.run_dir}")
    print(rows)
    print(f"results written to {args.out}")
    if args.write_doc:
        doc = args.config.resolve().parent.parent / "docs" / "rejected.md"
        token_estimates.write_doc(doc, rows)
        print(f"table written to {doc}")
    return 0


def cmd_experiment_remote_ledger(args: argparse.Namespace) -> int:
    """Rule C candidate 3: a central remote ledger against local-first plus merge."""
    import tempfile

    from boundary.experiment import remote_ledger

    with tempfile.TemporaryDirectory(prefix="boundary-experiment-") as tmp:
        result = remote_ledger.run(
            args.config,
            Path(tmp),
            repetitions=args.repetitions,
            calls=args.calls,
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(remote_ledger.to_json(result) + "\n", encoding="utf-8")
    print(result.doc_rows())
    print(f"results written to {args.out}")
    if args.write_doc:
        doc = args.config.resolve().parent.parent / "docs" / "rejected.md"
        remote_ledger.write_doc(doc, result.doc_rows())
        print(f"table written to {doc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boundary", description=f"boundary {__version__}")
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--project", default="compliant-ai-gateway")
    sub = parser.add_subparsers(dest="cmd", required=True)

    smoke = sub.add_parser("smoke", help="one short standard-mode call to a provider")
    smoke.add_argument("provider")
    smoke.add_argument("--model", help="provider/model-id; default per provider where known")
    smoke.add_argument("--run-id", dest="run_id", default=None)
    smoke.add_argument(
        "--batch",
        action="store_true",
        help="submit two requests as a vendor batch and collect them, instead of one call",
    )
    smoke.add_argument(
        "--stream",
        action="store_true",
        help="stream the call and report time to first token (openai_compat hosts only)",
    )
    smoke.add_argument(
        "--wait",
        type=float,
        default=900.0,
        help="seconds to wait for a batch to end before giving up (default 900)",
    )
    smoke.add_argument("--poll", type=float, default=20.0, help="seconds between batch polls")
    smoke.set_defaults(func=cmd_smoke)

    routes = sub.add_parser("routes", help="routes commands").add_subparsers(
        dest="sub", required=True
    )
    routes.add_parser("show", help="aliases and providers").set_defaults(func=cmd_routes_show)

    prices = sub.add_parser("prices", help="price file commands").add_subparsers(
        dest="sub", required=True
    )
    prices.add_parser("check", help="validate price files and warn when stale").set_defaults(
        func=cmd_prices_check
    )

    bench = sub.add_parser(
        "bench", help="measure overhead, completeness, caps and fidelity against a mock"
    )
    bench.add_argument("--calls", type=int, default=1000)
    bench.add_argument("--per-fault", dest="per_fault", type=int, default=50)
    bench.add_argument("--fidelity", type=int, default=500)
    bench.add_argument("--out", type=Path, default=Path("bench/results.json"))
    bench.add_argument("--write-readme", dest="write_readme", action="store_true")
    bench.set_defaults(func=cmd_bench)

    redact = sub.add_parser("redact", help="redaction commands").add_subparsers(
        dest="sub", required=True
    )
    rev = redact.add_parser(
        "eval", help="measure detection, leaks, over-redaction and latency on a generated corpus"
    )
    rev.add_argument("--pages", type=int, default=200)
    rev.add_argument(
        "--identifiers",
        action="store_true",
        help="the Canadian identifier set instead of the prose corpus: every claimed shape "
        "in every written form, the shapes no recogniser claims, and the near-misses",
    )
    rev.add_argument("--per-family", dest="per_family", type=int, default=50)
    rev.add_argument(
        "--rehydration",
        action="store_true",
        help="what survives the trip back: every mutation form a model applies to a "
        "placeholder, and the fabrication count that must stay at zero",
    )
    rev.add_argument(
        "--tab",
        action="store_true",
        help="the Text Anonymization Benchmark instead: real court judgments, test split, "
        "downloaded once at a pinned commit (with --presidio, both configurations)",
    )
    rev.add_argument(
        "--tab-allow",
        dest="tab_allow",
        default="",
        help="with --tab, also run with an allow list: 'train' derives one from TAB's train "
        "split (docs/redact.md), anything else is a file of one term per line",
    )
    rev.add_argument("--seed", type=int, default=20260920)
    rev.add_argument(
        "--presidio",
        action="store_true",
        help="add the Presidio recogniser (needs the redact extra and a spaCy model)",
    )
    rev.add_argument("--out", type=Path, default=Path("bench/redact.json"))
    rev.add_argument("--write-readme", dest="write_readme", action="store_true")
    rev.set_defaults(func=cmd_redact_eval)

    mut = redact.add_parser(
        "mutation",
        help="what models do to placeholders in flight, with and without a line asking them "
        "to preserve them; calls vendors (capped by --max-usd) unless --score is given",
    )
    mut.add_argument("--models", default=",".join(MUTATION_MODELS))
    mut.add_argument("--pages", type=int, default=20)
    mut.add_argument("--max-usd", dest="max_usd", type=float, default=0.35)
    mut.add_argument("--run-id", dest="run_id", default="mutation-rate")
    mut.add_argument("--out", type=Path, default=Path("bench/mutation.json"))
    mut.add_argument(
        "--score", type=Path, default=None, help="re-score a stored run; no call is made"
    )
    mut.add_argument("--write-readme", dest="write_readme", action="store_true")
    mut.set_defaults(func=cmd_redact_mutation)

    ledger = sub.add_parser("ledger", help="ledger commands").add_subparsers(
        dest="sub", required=True
    )
    report = ledger.add_parser("report", help="calls, tokens and cost per project, model and month")
    report.add_argument("--month", help="YYYY-MM filter")
    report.add_argument("--ledger", help="path to a ledger file (default from config)")
    report.add_argument(
        "--data-class",
        dest="data_class",
        help="only rows whose caller declared this class (public, internal, personal, "
        "sensitive), or 'undeclared' for the rows that declared none",
    )
    report.set_defaults(func=cmd_ledger_report)
    resid = ledger.add_parser("residency", help="calls per provider, region and declared residency")
    resid.add_argument("--month", help="YYYY-MM filter")
    resid.add_argument("--ledger", help="path to a ledger file (default from config)")
    resid.add_argument(
        "--data-class",
        dest="data_class",
        help="only rows whose caller declared this class, or 'undeclared': which calls "
        "carried personal data, and where did they go",
    )
    resid.add_argument(
        "--project",
        dest="project_filter",
        help="only rows from this ledger project (the top-level --project is the writer's "
        "label and does not filter)",
    )
    resid.add_argument(
        "--require",
        choices=[r.value for r in Residency],
        help="exit 2 if any call went wider than this. Undeclared rows fail every limit",
    )
    resid.set_defaults(func=cmd_ledger_residency)
    merge = ledger.add_parser(
        "merge", help="copy rows from other environments' ledgers into one file"
    )
    merge.add_argument("sources", nargs="+", help="ledger files to merge in")
    merge.add_argument("--into", help="destination ledger (default from config)")
    merge.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="report what would be inserted without writing to the destination",
    )
    merge.set_defaults(func=cmd_ledger_merge)

    pol = sub.add_parser(
        "policy", help="the data policy: which provider each class of data may reach"
    ).add_subparsers(dest="sub", required=True)
    pe = pol.add_parser(
        "eval",
        help="drive every provider, alias, class and entry point through the policy and count "
        "violations; exits 1 on any violation or false refusal",
    )
    pe.add_argument("--policy", help="the policy file (default: policy.yaml beside the config)")
    pe.add_argument(
        "--proxy",
        action="store_true",
        help="run the suite through the OpenAI-compatible proxy over HTTP (server extra)",
    )
    pe.add_argument("--write-readme", dest="write_readme", action="store_true")
    pe.set_defaults(func=cmd_policy_eval)

    serve = sub.add_parser("serve", help="run the OpenAI-compatible proxy (server extra)")
    serve.add_argument("--teams", help="the teams file (default: teams.yaml beside the config)")
    serve.add_argument(
        "--policy",
        help="the data policy (default: the config's policy, else policy.yaml beside it); "
        "the proxy will not start without one",
    )
    serve.add_argument(
        "--ledger",
        help=f"the ledger file (default: {PROXY_LEDGER} beside the config's ledger, so the "
        "proxy's teams and the library's own calls are counted apart)",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=cmd_serve)

    teams = sub.add_parser("teams", help="proxy team commands").add_subparsers(
        dest="sub", required=True
    )
    tk = teams.add_parser("key", help="mint a team key and print the hash for teams.yaml")
    tk.add_argument("--team", required=True)
    tk.set_defaults(func=cmd_teams_key)

    audit = sub.add_parser(
        "audit", help="the hash-chained audit log over a ledger (docs/audit.md)"
    ).add_subparsers(dest="sub", required=True)
    for name, help_text, func in (
        ("seal", "append a record for every ledger row new or changed since", cmd_audit_seal),
        (
            "verify",
            "recompute the chain and check it against anchors and the ledger",
            cmd_audit_verify,
        ),
        ("anchor", "append the current head to an anchor file", cmd_audit_anchor),
    ):
        sp = audit.add_parser(name, help=help_text)
        sp.add_argument("--ledger", help="the ledger the log seals (default from config)")
        sp.add_argument("--audit", help="the audit log (default <ledger>.audit.sqlite)")
        if name == "seal":
            sp.add_argument(
                "--settle-hours",
                dest="settle_hours",
                type=float,
                default=24.0,
                help="hold a row still in flight this long before sealing it as it stands",
            )
        if name == "verify":
            sp.add_argument("--anchors", help="an anchor file, one JSON line per anchor")
            sp.add_argument(
                "--no-ledger",
                dest="no_ledger",
                action="store_true",
                help="check the chain and anchors only, not the ledger against the chain",
            )
        if name == "anchor":
            sp.add_argument("--anchors", required=True, help="the anchor file to append to")
        sp.set_defaults(func=func)
    tt = audit.add_parser(
        "tamper-test", help="corrupt a generated chain every way and count detections"
    )
    tt.add_argument("--records", type=int, default=500)
    tt.add_argument("--anchor-interval", dest="anchor_interval", type=int, default=50)
    tt.add_argument("--trials", type=int, default=200)
    tt.add_argument("--seed", type=int, default=20260922)
    tt.add_argument("--out", type=Path, default=Path("bench/audit.json"))
    tt.add_argument("--write-readme", dest="write_readme", action="store_true")
    tt.add_argument(
        "--write-doc",
        dest="write_doc",
        action="store_true",
        help="fill the per-kind table in docs/audit.md between its markers",
    )
    tt.set_defaults(func=cmd_audit_tamper_test)

    batch = sub.add_parser(
        "batch", help="a vendor batch submitted earlier: ask after it, or collect it"
    ).add_subparsers(dest="sub", required=True)
    for name, help_text, func in (
        ("status", "where the vendor has got to; writes nothing", cmd_batch_status),
        ("collect", "complete the ledger rows of a finished batch", cmd_batch_collect),
    ):
        sp = batch.add_parser(name, help=help_text)
        sp.add_argument("batch_id")
        sp.add_argument(
            "--ledger",
            help="the ledger holding the batch's rows (default from config); a batch is "
            "collected from the ledger that submitted it",
        )
        sp.add_argument("--wait", type=float, default=0.0, help="seconds to wait for the batch")
        sp.add_argument("--poll", type=float, default=20.0, help="seconds between polls")
        sp.set_defaults(func=func)

    experiment = sub.add_parser(
        "experiment", help="the measurements behind docs/rejected.md (Rule C)"
    ).add_subparsers(dest="sub", required=True)
    remote = experiment.add_parser(
        "remote-ledger", help="a central remote ledger against local-first plus merge"
    )
    remote.add_argument("--repetitions", type=int, default=100)
    remote.add_argument("--calls", type=int, default=40)
    remote.add_argument("--out", type=Path, default=Path("bench/remote-ledger.json"))
    remote.add_argument(
        "--write-doc",
        dest="write_doc",
        action="store_true",
        help="fill the table in docs/rejected.md between its markers",
    )
    remote.set_defaults(func=cmd_experiment_remote_ledger)

    tokens = experiment.add_parser(
        "token-estimates",
        help="a local token estimate against the vendor's returned usage",
    )
    tokens.add_argument(
        "run_dir",
        type=Path,
        help="a drift run directory holding one sub-directory per arm, each with a "
        "ledger.sqlite and a raw store",
    )
    tokens.add_argument("--bootstrap", type=int, default=1000)
    tokens.add_argument("--out", type=Path, default=Path("bench/token-estimates.json"))
    tokens.add_argument(
        "--write-doc",
        dest="write_doc",
        action="store_true",
        help="fill the table in docs/rejected.md between its markers",
    )
    tokens.set_defaults(func=cmd_experiment_token_estimates)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BoundaryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
