"""The benchmark runs end to end at small sizes and its invariants hold."""

from __future__ import annotations

from pathlib import Path

from boundary import bench

from .conftest import CONFIG_DIR


def test_bench_small(tmp_path: Path, keys: None) -> None:
    results = bench.run(
        CONFIG_DIR / "boundary.yaml",
        tmp_path,
        calls=30,
        per_fault=3,
        cap_attempts=8,
        fidelity_requests=25,
    )
    assert results.overhead.calls == 30
    assert results.overhead.p50_ci_ms[0] <= results.overhead.p50_ms <= results.overhead.p50_ci_ms[1]
    assert results.completeness.attempted == 3 * len(bench.FAULTS) * 2
    assert results.completeness.ratio == 1.0
    assert results.completeness.in_flight_rows == 3 * 2
    assert results.caps.reached_upstream_past_cap == 0
    assert results.caps.attempted_past_cap > 0
    assert results.fidelity.share == 1.0
    row = results.readme_row()
    assert row.startswith("|") and row.endswith("|") and "100%" in row


def test_write_readme_replaces_between_markers(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        f"before\n{bench.README_START}\n| old |\n{bench.README_END}\nafter\n", encoding="utf-8"
    )
    bench.write_readme(readme, "| new |")
    text = readme.read_text(encoding="utf-8")
    assert "| old |" not in text and "| new |" in text
    assert text.startswith("before\n") and text.endswith("after\n")
