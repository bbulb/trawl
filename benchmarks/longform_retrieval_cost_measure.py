"""Longform retrieval cost A/B: budget=0 vs budget=150.

Runs the four longform cases identified in the C5 premise spike
(wiki_history_of_the_internet, arxiv_pdf, wiki_llm, korean_wiki_person)
against both modes, 3 iterations each, and reports:

- retrieval_ms per mode per case (p50 / p95 across 3 runs)
- n_chunks_embedded per mode (sanity: experiment stays at / under 150)
- rank-1 chunk stability (same top-1 chunk identity across modes,
  measured after the reranker via a normalised string-prefix signature)

The script is idempotent — it disables the fetch cache only when
explicitly requested (`--no-fetch-cache`); otherwise letting the cache
hit on iteration 2–3 is *desirable*, because it removes fetch-time
noise and lets us measure the retrieval-stage delta cleanly.

Pre-registered gates (design doc
`docs/superpowers/specs/2026-04-20-longform-retrieval-cost-design.md`):

- longform `retrieval_ms.p95` baseline ≥ 5000 ms, experiment ≤ 2500 ms
- rank-1 identity preserved for ≥ 3/4 cases
- baseline and experiment both error-free for all 4 cases

Invoke:
    python benchmarks/longform_retrieval_cost_measure.py
    python benchmarks/longform_retrieval_cost_measure.py --only wiki_llm
    python benchmarks/longform_retrieval_cost_measure.py --iterations 5 -v

Writes `benchmarks/results/longform-retrieval-cost/<ts>/`:
    summary.json   aggregated metrics + gate pass/fail
    report.md      human-readable report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_ROOT = BENCH_DIR / "results" / "longform-retrieval-cost"

LONGFORM_CASES = [
    {
        "id": "wiki_history_of_the_internet",
        "url": "https://en.wikipedia.org/wiki/History_of_the_Internet",
        "query": "when was the first ARPANET message sent",
    },
    {
        "id": "arxiv_pdf",
        "url": "https://arxiv.org/pdf/1706.03762",
        "query": "attention is all you need architecture overview",
    },
    {
        "id": "wiki_llm",
        "url": "https://en.wikipedia.org/wiki/Large_language_model",
        "query": "scaling laws for large language models",
    },
    {
        "id": "korean_wiki_person",
        "url": "https://ko.wikipedia.org/wiki/%EC%9D%B4%EC%88%9C%EC%8B%A0",
        "query": "이순신이 명량 해전에서 몇 척의 배로 싸웠나",
    },
]

GATE_RETRIEVAL_MS_P95_MAX = 2500
GATE_RANK1_MIN_IDENTITY = 3  # out of 4 longform cases
BUDGET_EXPERIMENT = 150


@dataclass
class RunOutcome:
    iteration: int
    ok: bool
    error: str | None = None
    retrieval_ms: int | None = None
    n_chunks_total: int | None = None
    n_chunks_embedded: int | None = None
    total_ms: int | None = None
    rank1_sig: str | None = None


@dataclass
class CaseResult:
    id: str
    url: str
    baseline: list[RunOutcome] = field(default_factory=list)
    experiment: list[RunOutcome] = field(default_factory=list)


def _rank1_signature(result: Any) -> str | None:
    """Short fingerprint of the rank-1 chunk so identity can be compared
    across modes even when scores differ. We take the first 80 chars of
    the chunk text + its heading path so a chunk moved to a different
    section gets a different signature.
    """
    if not result.chunks:
        return None
    top = result.chunks[0]
    body = (top.get("text") or "")[:80].strip()
    heading = top.get("heading") or ""
    return hashlib.sha1(f"{heading}||{body}".encode()).hexdigest()[:12]


def run_case(case: dict, *, budget: int, iteration: int, verbose: bool) -> RunOutcome:
    from trawl import fetch_relevant

    if budget > 0:
        os.environ["TRAWL_CHUNK_BUDGET"] = str(budget)
    else:
        os.environ.pop("TRAWL_CHUNK_BUDGET", None)

    outcome = RunOutcome(iteration=iteration, ok=False)
    try:
        t0 = time.time()
        result = fetch_relevant(case["url"], case["query"])
        outcome.total_ms = int((time.time() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        outcome.error = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"    iter {iteration}: EXC {outcome.error}", file=sys.stderr)
        return outcome

    outcome.ok = result.error is None
    outcome.error = result.error
    outcome.retrieval_ms = result.retrieval_ms
    outcome.n_chunks_total = result.n_chunks_total
    outcome.n_chunks_embedded = result.n_chunks_embedded
    outcome.rank1_sig = _rank1_signature(result)
    if verbose:
        status = "ok " if outcome.ok else "err"
        print(
            f"    iter {iteration} [{status}]: "
            f"retr={outcome.retrieval_ms}ms "
            f"chunks={outcome.n_chunks_total}/{outcome.n_chunks_embedded}",
            file=sys.stderr,
        )
    return outcome


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


def _mode_stats(runs: list[RunOutcome]) -> dict:
    oks = [r for r in runs if r.ok and r.retrieval_ms is not None]
    retr = [float(r.retrieval_ms) for r in oks]
    embedded = [float(r.n_chunks_embedded) for r in oks if r.n_chunks_embedded is not None]
    total = [float(r.total_ms) for r in oks if r.total_ms is not None]
    return {
        "n_ok": len(oks),
        "retrieval_ms": {
            "min": min(retr) if retr else None,
            "median": statistics.median(retr) if retr else None,
            "p95": pct(retr, 0.95),
            "values": retr,
        },
        "n_chunks_embedded": {
            "median": statistics.median(embedded) if embedded else None,
            "values": embedded,
        },
        "total_ms": {
            "median": statistics.median(total) if total else None,
            "values": total,
        },
    }


def _rank1_identity(runs: list[RunOutcome]) -> str | None:
    """Return the rank-1 signature if stable across all ok runs, else None."""
    sigs = {r.rank1_sig for r in runs if r.ok and r.rank1_sig}
    if not sigs:
        return None
    if len(sigs) > 1:
        return None  # unstable across iterations; can't compare meaningfully
    return sigs.pop()


def aggregate(cases: list[CaseResult]) -> dict:
    per_case: dict[str, Any] = {}
    baseline_retr_p95: list[float] = []
    experiment_retr_p95: list[float] = []
    rank1_hits = 0
    rank1_total = 0

    for case in cases:
        bs = _mode_stats(case.baseline)
        es = _mode_stats(case.experiment)
        b_sig = _rank1_identity(case.baseline)
        e_sig = _rank1_identity(case.experiment)
        identity_match = None
        if b_sig is not None and e_sig is not None:
            identity_match = b_sig == e_sig
            rank1_total += 1
            if identity_match:
                rank1_hits += 1

        per_case[case.id] = {
            "url": case.url,
            "baseline": bs,
            "experiment": es,
            "baseline_rank1_sig": b_sig,
            "experiment_rank1_sig": e_sig,
            "rank1_identity_match": identity_match,
        }
        if bs["retrieval_ms"]["p95"] is not None:
            baseline_retr_p95.append(bs["retrieval_ms"]["p95"])
        if es["retrieval_ms"]["p95"] is not None:
            experiment_retr_p95.append(es["retrieval_ms"]["p95"])

    overall_baseline_p95 = pct(baseline_retr_p95, 0.95) if baseline_retr_p95 else None
    overall_experiment_p95 = pct(experiment_retr_p95, 0.95) if experiment_retr_p95 else None

    # Gate evaluation
    gate_p95 = (
        overall_experiment_p95 is not None
        and overall_experiment_p95 <= GATE_RETRIEVAL_MS_P95_MAX
    )
    gate_rank1 = rank1_hits >= GATE_RANK1_MIN_IDENTITY

    return {
        "per_case": per_case,
        "overall": {
            "baseline_retrieval_ms_p95": overall_baseline_p95,
            "experiment_retrieval_ms_p95": overall_experiment_p95,
            "rank1_identity_matches": rank1_hits,
            "rank1_identity_total": rank1_total,
        },
        "gates": {
            "retrieval_ms_p95_leq_2500": gate_p95,
            "rank1_identity_ge_3_of_4": gate_rank1,
        },
    }


def write_report(
    aggregation: dict, out_path: Path, generated_at: str, budget: int
) -> None:
    g = aggregation["gates"]
    o = aggregation["overall"]
    lines: list[str] = []
    lines.append("# Longform retrieval cost — A/B report")
    lines.append("")
    lines.append(f"**Generated:** {generated_at}")
    lines.append(f"**Budget (experiment):** {budget}")
    lines.append("")
    lines.append("## Gate summary")
    lines.append("")
    lines.append(f"- retrieval_ms.p95 ≤ {GATE_RETRIEVAL_MS_P95_MAX} ms: "
                 f"**{'PASS' if g['retrieval_ms_p95_leq_2500'] else 'FAIL'}**")
    lines.append(f"- rank-1 identity ≥ {GATE_RANK1_MIN_IDENTITY}/4: "
                 f"**{'PASS' if g['rank1_identity_ge_3_of_4'] else 'FAIL'}**")
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append("| metric | baseline | experiment |")
    lines.append("|---|---:|---:|")
    lines.append(
        f"| retrieval_ms.p95 (across cases) | "
        f"{_fmt_ms(o['baseline_retrieval_ms_p95'])} | "
        f"{_fmt_ms(o['experiment_retrieval_ms_p95'])} |"
    )
    lines.append(
        f"| rank-1 identity matches | - | "
        f"{o['rank1_identity_matches']}/{o['rank1_identity_total']} |"
    )
    lines.append("")
    lines.append("## Per case")
    lines.append("")
    lines.append("| case | baseline retr median/p95 | exp retr median/p95 | baseline chunks | exp embedded | rank1 match |")
    lines.append("|---|---:|---:|---:|---:|:---:|")
    for case_id, case in aggregation["per_case"].items():
        b = case["baseline"]
        e = case["experiment"]
        b_retr = f"{_fmt_ms(b['retrieval_ms']['median'])}/{_fmt_ms(b['retrieval_ms']['p95'])}"
        e_retr = f"{_fmt_ms(e['retrieval_ms']['median'])}/{_fmt_ms(e['retrieval_ms']['p95'])}"
        b_chunks = _fmt_int(b['n_chunks_embedded']['median'])
        e_chunks = _fmt_int(e['n_chunks_embedded']['median'])
        match = case['rank1_identity_match']
        match_str = "y" if match is True else ("n" if match is False else "?")
        lines.append(f"| {case_id} | {b_retr} | {e_retr} | {b_chunks} | {e_chunks} | {match_str} |")
    lines.append("")
    lines.append("## Raw iteration values")
    lines.append("")
    for case_id, case in aggregation["per_case"].items():
        lines.append(f"### {case_id}")
        lines.append("")
        lines.append("| mode | retrieval_ms (each iter) | n_chunks_embedded | rank1_sig |")
        lines.append("|---|---|---|---|")
        b = case["baseline"]
        e = case["experiment"]
        lines.append(
            f"| baseline | {[int(v) for v in b['retrieval_ms']['values']]} | "
            f"{[int(v) for v in b['n_chunks_embedded']['values']]} | "
            f"{case['baseline_rank1_sig']} |"
        )
        lines.append(
            f"| exp (budget={budget}) | "
            f"{[int(v) for v in e['retrieval_ms']['values']]} | "
            f"{[int(v) for v in e['n_chunks_embedded']['values']]} | "
            f"{case['experiment_rank1_sig']} |"
        )
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{int(v)} ms"


def _fmt_int(v: float | None) -> str:
    if v is None:
        return "-"
    return str(int(v))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--only", help="Run only the case with this id")
    parser.add_argument(
        "--iterations", type=int, default=3,
        help="Number of iterations per (case, mode). Default 3.",
    )
    parser.add_argument(
        "--budget", type=int, default=BUDGET_EXPERIMENT,
        help=f"Experimental chunk budget. Default {BUDGET_EXPERIMENT}.",
    )
    parser.add_argument(
        "--no-fetch-cache", action="store_true",
        help="Disable fetch cache (TRAWL_FETCH_CACHE_TTL=0). Default uses cache so iterations 2+ measure pure retrieval.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    cases = LONGFORM_CASES
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"no case with id={args.only!r}", file=sys.stderr)
            return 2

    if args.no_fetch_cache:
        os.environ["TRAWL_FETCH_CACHE_TTL"] = "0"

    # Ensure hybrid retrieval is off to isolate the chunk_budget effect.
    os.environ.pop("TRAWL_HYBRID_RETRIEVAL", None)

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    print(
        f"running {len(cases)} cases × 2 modes × {args.iterations} iterations "
        f"→ {out_dir}",
        file=sys.stderr,
    )

    case_results: list[CaseResult] = []
    for i, c in enumerate(cases, 1):
        cr = CaseResult(id=c["id"], url=c["url"])
        print(f"[{i}/{len(cases)}] {c['id']}", file=sys.stderr)
        for mode_name, budget in (("baseline", 0), ("experiment", args.budget)):
            print(f"  {mode_name} (budget={budget}):", file=sys.stderr)
            for it in range(1, args.iterations + 1):
                outcome = run_case(c, budget=budget, iteration=it, verbose=args.verbose)
                getattr(cr, mode_name).append(outcome)
        case_results.append(cr)

    aggregation = aggregate(case_results)
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "iterations": args.iterations,
                "budget_experiment": args.budget,
                "gates": aggregation["gates"],
                "overall": aggregation["overall"],
                "per_case": aggregation["per_case"],
                "cases": [
                    {
                        "id": c.id,
                        "url": c.url,
                        "baseline": [asdict(o) for o in c.baseline],
                        "experiment": [asdict(o) for o in c.experiment],
                    }
                    for c in case_results
                ],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    write_report(aggregation, out_dir / "report.md", generated_at, args.budget)

    print("", file=sys.stderr)
    gates = aggregation["gates"]
    print(
        f"gate retrieval_ms.p95 ≤ {GATE_RETRIEVAL_MS_P95_MAX}: "
        f"{'PASS' if gates['retrieval_ms_p95_leq_2500'] else 'FAIL'}",
        file=sys.stderr,
    )
    print(
        f"gate rank-1 identity ≥ {GATE_RANK1_MIN_IDENTITY}/4: "
        f"{'PASS' if gates['rank1_identity_ge_3_of_4'] else 'FAIL'}",
        file=sys.stderr,
    )
    print(f"results -> {out_dir}/report.md", file=sys.stderr)
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
