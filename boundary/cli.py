"""Command line: boundary smoke <provider> | routes show | prices check | ledger report.

`ledger merge` arrives in v0.2. Every command takes --config (default: config/boundary.yaml
next to the current directory or the installed package's config) and --project.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

from boundary import __version__
from boundary.config import latest_price_list, load_config, load_price_list, price_files
from boundary.errors import BoundaryError
from boundary.gateway import Gateway
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
    with Gateway.from_config(args.config, project=args.project) as gw:
        resp = gw.chat(
            ChatRequest(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word OK."}],
                # No temperature: reasoning models accept only their default, and a smoke
                # call is about the plumbing, not the sampling.
                max_tokens=64,
                extra=SMOKE_EXTRA.get(args.provider, {}),
            ),
            purpose="smoke",
            run_id=args.run_id,
            mode=Mode.STANDARD,
        )
    cost = f"US${resp.cost_usd:.6f}" if resp.cost_usd is not None else "uncosted"
    print(
        f"{resp.provider}: status {resp.status}, model {resp.model_returned}, "
        f"text {resp.text!r}, tokens {resp.usage.input_tokens}/{resp.usage.output_tokens}, "
        f"{cost}, {resp.latency_ms:.0f} ms, retries {resp.retries}, ledger row {resp.ledger_id}"
    )
    return 0 if resp.ok else 1


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
    for provider, models in sorted(latest.per_million_tokens.items()):
        for model, e in sorted(models.items()):
            cache = (
                f", cache read {e.cache_read} write {e.cache_write}"
                if e.cache_read is not None or e.cache_write is not None
                else ""
            )
            batch = f", batch x{e.batch_multiplier}" if e.batch_multiplier is not None else ""
            print(f"  {provider}/{model}: in {e.input} out {e.output}{cache}{batch}")
    missing = [
        f"{alias} -> {r.provider}/{r.model}"
        for alias, r in cfg.routes.items()
        if not cfg.providers[r.provider].price_zero and latest.lookup(r.provider, r.model) is None
    ]
    if missing:
        print("routes without a price (calls will be uncosted):", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
    for f in files:
        load_price_list(f)  # validates every file, not only the latest
    return 1 if (stale or missing) else 0


def cmd_ledger_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    path = Path(args.ledger) if args.ledger else cfg.ledger.path
    if not path.is_file():
        print(f"no ledger at {path}", file=sys.stderr)
        return 1
    store = LedgerStore(path)
    try:
        rows = store.rows()
        month = args.month
        if month:
            rows = [r for r in rows if str(r["ts_utc"]).startswith(month)]
        by: dict[tuple[str, str, str], dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "uncosted": 0, "cost": 0.0, "in": 0, "out": 0}
        )
        for r in rows:
            key = (str(r["ts_utc"])[:7], str(r["project"]), str(r["model_requested"]))
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
        print(
            f"{'month':<8} {'project':<24} {'model':<44} {'calls':>6} {'err':>4} {'unc':>4} {'in':>9} {'out':>8} {'USD':>10}"
        )
        total = 0.0
        for (m, p, model), b in sorted(by.items()):
            total += b["cost"]
            print(
                f"{m:<8} {p:<24} {model:<44} {int(b['calls']):>6} {int(b['errors']):>4} "
                f"{int(b['uncosted']):>4} {int(b['in']):>9} {int(b['out']):>8} {b['cost']:>10.4f}"
            )
        print(
            f"{'total':<8} {'':<24} {'':<44} {len(rows):>6} {'':>4} {store.uncosted_count():>4} {'':>9} {'':>8} {total:>10.4f}"
        )
        in_flight = sum(1 for r in rows if r["error_type"] == "in_flight")
        if in_flight:
            print(
                f"warning: {in_flight} row(s) still in_flight (process killed mid-call)",
                file=sys.stderr,
            )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="boundary", description=f"boundary {__version__}")
    parser.add_argument("--config", type=Path, default=_default_config())
    parser.add_argument("--project", default="compliant-ai-gateway")
    sub = parser.add_subparsers(dest="cmd", required=True)

    smoke = sub.add_parser("smoke", help="one short standard-mode call to a provider")
    smoke.add_argument("provider")
    smoke.add_argument("--model", help="provider/model-id; default per provider where known")
    smoke.add_argument("--run-id", dest="run_id", default=None)
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

    ledger = sub.add_parser("ledger", help="ledger commands").add_subparsers(
        dest="sub", required=True
    )
    report = ledger.add_parser("report", help="calls, tokens and cost per project, model and month")
    report.add_argument("--month", help="YYYY-MM filter")
    report.add_argument("--ledger", help="path to a ledger file (default from config)")
    report.set_defaults(func=cmd_ledger_report)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BoundaryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
