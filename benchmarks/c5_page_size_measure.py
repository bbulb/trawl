"""C5 premise-verification spike: page size + chunk count distribution.

Runs `fetch_relevant()` over the parity matrix + benchmark matrix + a
small stress tail, with telemetry enabled to an isolated path, then
aggregates the resulting JSONL into a summary. The goal is to answer:

    Does the current pipeline hit the scale problem (pages producing
    thousands of chunks / 1M-token-scale markdown) that C5 (hierarchical
    section fetch) is supposed to solve? Or is the profile + records
    chunker already covering the envelope we actually fetch?

See docs/superpowers/specs/2026-04-20-c5-hierarchical-fetch-design.md
for the pre-registered decision thresholds.

Invoke:
    python benchmarks/c5_page_size_measure.py
    python benchmarks/c5_page_size_measure.py --only wiki_llm
    python benchmarks/c5_page_size_measure.py --skip parity,benchmark
    python benchmarks/c5_page_size_measure.py --verbose

Writes results to benchmarks/results/c5-premise/<ts>/:
    telemetry.jsonl      raw per-call telemetry events (gitignored)
    summary.json         aggregated percentiles + per-category breakdown
    report.md            human-readable decision-grade report
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

BENCH_DIR = Path(__file__).parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_ROOT = BENCH_DIR / "results" / "c5-premise"

PARITY_CASES = REPO_ROOT / "tests" / "test_cases.yaml"
BENCH_CASES = BENCH_DIR / "benchmark_cases.yaml"

# Stress-tail URLs: deliberately-long public pages used to probe the
# top end of the distribution. Not about correctness; only about size.
STRESS_CASES = [
    {
        "id": "wiki_list_countries",
        "category": "stress_wiki_megapage",
        "url": "https://en.wikipedia.org/wiki/List_of_countries_and_dependencies_by_population",
        "query": "countries ranked by population",
    },
    {
        "id": "python_full_stdlib_index",
        "category": "stress_docs",
        "url": "https://docs.python.org/3/library/index.html",
        "query": "standard library modules list",
    },
    {
        "id": "wiki_history_of_the_internet",
        "category": "stress_wiki_longform",
        "url": "https://en.wikipedia.org/wiki/History_of_the_Internet",
        "query": "when was the first ARPANET message sent",
    },
]


@dataclass
class RunOutcome:
    id: str
    category: str
    url: str
    ok: bool
    error: str | None = None
    page_chars: int | None = None
    n_chunks_total: int | None = None
    fetch_ms: int | None = None
    chunk_ms: int | None = None
    retrieval_ms: int | None = None
    rerank_ms: int | None = None
    total_ms: int | None = None
    fetcher_used: str | None = None
    path: str | None = None
    profile_used: bool | None = None
    host: str | None = None


@dataclass
class Summary:
    generated_at: str
    schema_version: int = 1
    runs: list[dict] = field(default_factory=list)
    percentiles: dict = field(default_factory=dict)
    per_category: dict = field(default_factory=dict)
    per_host: dict = field(default_factory=dict)
    failures: list[dict] = field(default_factory=list)
    decision: dict = field(default_factory=dict)


def load_parity_cases() -> list[dict]:
    with PARITY_CASES.open() as f:
        cases = yaml.safe_load(f)["cases"]
    for c in cases:
        c.setdefault("category", "parity")
    return cases


def load_bench_cases() -> list[dict]:
    with BENCH_CASES.open() as f:
        return yaml.safe_load(f)["cases"]


def pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values_sorted = sorted(values)
    k = (len(values_sorted) - 1) * p
    f_idx = int(k)
    c_idx = min(f_idx + 1, len(values_sorted) - 1)
    if f_idx == c_idx:
        return float(values_sorted[f_idx])
    frac = k - f_idx
    return float(values_sorted[f_idx] * (1 - frac) + values_sorted[c_idx] * frac)


def summarise(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": min(values),
        "p50": pct(values, 0.50),
        "p95": pct(values, 0.95),
        "p99": pct(values, 0.99),
        "max": max(values),
        "mean": statistics.fmean(values),
    }


def run_one(case: dict, verbose: bool) -> RunOutcome:
    from trawl import fetch_relevant

    url = case["url"]
    query = case.get("query") or ""
    outcome = RunOutcome(
        id=case.get("id", "?"),
        category=case.get("category", "unknown"),
        url=url,
        ok=False,
        host=urlsplit(url).netloc,
    )
    t0 = time.time()
    try:
        result = fetch_relevant(url, query)
    except Exception as e:  # noqa: BLE001
        outcome.error = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"  [err] {case['id']}: {outcome.error}", file=sys.stderr)
        return outcome
    elapsed_ms = int((time.time() - t0) * 1000)
    outcome.ok = result.error is None
    outcome.error = result.error
    outcome.page_chars = result.page_chars
    outcome.n_chunks_total = result.n_chunks_total
    outcome.fetch_ms = result.fetch_ms
    outcome.chunk_ms = result.chunk_ms
    outcome.retrieval_ms = result.retrieval_ms
    outcome.rerank_ms = result.rerank_ms
    outcome.total_ms = result.total_ms or elapsed_ms
    outcome.fetcher_used = result.fetcher_used
    outcome.path = result.path
    outcome.profile_used = result.profile_used
    if verbose:
        status = "ok " if outcome.ok else "err"
        print(
            f"  [{status}] {case['id']:<30s} "
            f"page={outcome.page_chars or 0:>7d} "
            f"chunks={outcome.n_chunks_total or 0:>4d} "
            f"fetch={outcome.fetch_ms or 0:>5d}ms "
            f"retr={outcome.retrieval_ms or 0:>4d}ms "
            f"path={outcome.path}"
        )
    return outcome


def decide(per_summary: dict) -> dict:
    """Apply pre-registered thresholds from the design doc.

    Thresholds mirror the table in the design doc so the conclusion
    cannot be fudged: changing the threshold requires changing the doc.
    """
    p95_page = per_summary.get("page_chars", {}).get("p95") or 0
    p95_chunks = per_summary.get("n_chunks_total", {}).get("p95") or 0
    p95_retr = per_summary.get("retrieval_ms", {}).get("p95") or 0

    crossed = []
    if p95_page >= 200_000:
        crossed.append(f"page_chars.p95={int(p95_page)} >= 200000")
    if p95_chunks >= 500:
        crossed.append(f"n_chunks_total.p95={int(p95_chunks)} >= 500")
    if p95_retr >= 1000:
        crossed.append(f"retrieval_ms.p95={int(p95_retr)} >= 1000")

    if not crossed:
        verdict = "defer"
    elif len(crossed) == 1:
        verdict = "adopt_narrow"
    else:
        verdict = "adopt_broad"

    return {
        "verdict": verdict,
        "thresholds_crossed": crossed,
        "p95": {
            "page_chars": p95_page,
            "n_chunks_total": p95_chunks,
            "retrieval_ms": p95_retr,
        },
    }


def write_report(summary: Summary, out_path: Path) -> None:
    d = summary.decision
    p95 = d.get("p95", {})

    lines: list[str] = []
    lines.append("# C5 Premise Measurement — Report")
    lines.append("")
    lines.append(f"**Generated:** {summary.generated_at}")
    lines.append(f"**Runs:** {len(summary.runs)} (failures: {len(summary.failures)})")
    lines.append("")
    lines.append("## Decision")
    lines.append("")
    lines.append(f"**Verdict:** `{d.get('verdict', '?')}`")
    lines.append("")
    if d.get("thresholds_crossed"):
        lines.append("Thresholds crossed:")
        for t in d["thresholds_crossed"]:
            lines.append(f"- {t}")
    else:
        lines.append("No pre-registered threshold crossed.")
    lines.append("")
    lines.append("p95 snapshot:")
    lines.append(f"- page_chars.p95 = {int(p95.get('page_chars') or 0):,}")
    lines.append(f"- n_chunks_total.p95 = {int(p95.get('n_chunks_total') or 0)}")
    lines.append(f"- retrieval_ms.p95 = {int(p95.get('retrieval_ms') or 0)}")
    lines.append("")

    lines.append("## Overall percentiles")
    lines.append("")
    lines.append("| metric | n | min | p50 | p95 | p99 | max | mean |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for metric in [
        "page_chars",
        "n_chunks_total",
        "fetch_ms",
        "chunk_ms",
        "retrieval_ms",
        "rerank_ms",
        "total_ms",
    ]:
        s = summary.percentiles.get(metric) or {}
        if not s or s.get("n", 0) == 0:
            lines.append(f"| {metric} | 0 | - | - | - | - | - | - |")
            continue
        lines.append(
            f"| {metric} | {s['n']} | {int(s['min'])} | "
            f"{int(s['p50'])} | {int(s['p95'])} | {int(s['p99'])} | "
            f"{int(s['max'])} | {int(s['mean'])} |"
        )
    lines.append("")

    lines.append("## Per category")
    lines.append("")
    lines.append("| category | n | page_chars p95 | n_chunks_total p95 | retrieval_ms p95 |")
    lines.append("|---|---:|---:|---:|---:|")
    for cat, metrics in sorted(summary.per_category.items()):
        s_page = metrics.get("page_chars", {})
        s_chunks = metrics.get("n_chunks_total", {})
        s_retr = metrics.get("retrieval_ms", {})
        lines.append(
            f"| {cat} | {s_page.get('n', 0)} | "
            f"{int(s_page.get('p95') or 0)} | "
            f"{int(s_chunks.get('p95') or 0)} | "
            f"{int(s_retr.get('p95') or 0)} |"
        )
    lines.append("")

    lines.append("## Per host")
    lines.append("")
    lines.append("| host | n | page_chars max | n_chunks_total max |")
    lines.append("|---|---:|---:|---:|")
    for host, metrics in sorted(summary.per_host.items()):
        s_page = metrics.get("page_chars", {})
        s_chunks = metrics.get("n_chunks_total", {})
        lines.append(
            f"| {host} | {s_page.get('n', 0)} | "
            f"{int(s_page.get('max') or 0)} | "
            f"{int(s_chunks.get('max') or 0)} |"
        )
    lines.append("")

    if summary.failures:
        lines.append("## Failures")
        lines.append("")
        for f in summary.failures:
            lines.append(f"- `{f['id']}` ({f.get('host') or '?'}): {f.get('error')}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def aggregate(outcomes: list[RunOutcome]) -> Summary:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary = Summary(generated_at=ts)

    successes = [o for o in outcomes if o.ok and o.page_chars is not None]

    summary.runs = [o.__dict__ for o in outcomes]
    summary.failures = [
        {"id": o.id, "host": o.host, "error": o.error}
        for o in outcomes
        if not o.ok
    ]

    fields_to_summarise = [
        "page_chars",
        "n_chunks_total",
        "fetch_ms",
        "chunk_ms",
        "retrieval_ms",
        "rerank_ms",
        "total_ms",
    ]
    for f in fields_to_summarise:
        values = [getattr(o, f) for o in successes if getattr(o, f) is not None]
        summary.percentiles[f] = summarise([float(v) for v in values])

    per_cat: dict[str, list[RunOutcome]] = defaultdict(list)
    for o in successes:
        per_cat[o.category].append(o)
    for cat, group in per_cat.items():
        summary.per_category[cat] = {
            f: summarise(
                [float(getattr(o, f)) for o in group if getattr(o, f) is not None]
            )
            for f in fields_to_summarise
        }

    per_host: dict[str, list[RunOutcome]] = defaultdict(list)
    for o in successes:
        if o.host:
            per_host[o.host].append(o)
    for host, group in per_host.items():
        summary.per_host[host] = {
            f: summarise(
                [float(getattr(o, f)) for o in group if getattr(o, f) is not None]
            )
            for f in fields_to_summarise
        }

    summary.decision = decide(summary.percentiles)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="Run only the case with this id")
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated suites to skip: parity, benchmark, stress",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    ts_dir = RESULTS_ROOT / time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    ts_dir.mkdir(parents=True, exist_ok=True)
    telemetry_path = ts_dir / "telemetry.jsonl"

    os.environ["TRAWL_TELEMETRY"] = "1"
    os.environ["TRAWL_TELEMETRY_PATH"] = str(telemetry_path)

    cases: list[dict] = []
    if "parity" not in skip:
        cases.extend(load_parity_cases())
    if "benchmark" not in skip:
        cases.extend(load_bench_cases())
    if "stress" not in skip:
        cases.extend(STRESS_CASES)

    if args.only:
        cases = [c for c in cases if c.get("id") == args.only]
        if not cases:
            print(f"no case with id={args.only!r}", file=sys.stderr)
            return 2

    print(f"running {len(cases)} cases -> {ts_dir}", file=sys.stderr)
    if args.verbose:
        print("  telemetry:", telemetry_path, file=sys.stderr)

    outcomes: list[RunOutcome] = []
    for i, c in enumerate(cases, 1):
        if args.verbose:
            print(f"[{i}/{len(cases)}] {c['id']} ({c.get('category', '?')})", file=sys.stderr)
        outcome = run_one(c, verbose=args.verbose)
        outcomes.append(outcome)

    summary = aggregate(outcomes)

    (ts_dir / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": summary.generated_at,
                "schema_version": summary.schema_version,
                "runs": summary.runs,
                "percentiles": summary.percentiles,
                "per_category": summary.per_category,
                "per_host": summary.per_host,
                "failures": summary.failures,
                "decision": summary.decision,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_report(summary, ts_dir / "report.md")

    d = summary.decision
    print("", file=sys.stderr)
    print(f"verdict: {d['verdict']}", file=sys.stderr)
    for t in d["thresholds_crossed"]:
        print(f"  crossed: {t}", file=sys.stderr)
    print(f"results -> {ts_dir}/report.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
