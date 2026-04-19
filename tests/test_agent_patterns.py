"""Agent usage pattern harness.

Runs every pattern in `tests/agent_patterns/*.yaml` against a live
trawl deployment and reports pass/fail per pattern. Designed to be
run alongside `tests/test_pipeline.py` (the 15-case extraction
parity matrix); the two tests have different goals:

    test_pipeline.py            extraction-quality regression for one URL
    test_agent_patterns.py      end-to-end workflow regression for one
                                or more URLs strung together as an
                                agent would use them

Invoke:
    python tests/test_agent_patterns.py                         # all
    python tests/test_agent_patterns.py --shard coding          # one shard
    python tests/test_agent_patterns.py --only ID --verbose
    python tests/test_agent_patterns.py --category compositional
    python tests/test_agent_patterns.py --dry-run               # schema only
    python tests/test_agent_patterns.py --baseline              # write budgets
    python tests/test_agent_patterns.py --regression            # +20% gate
    python tests/test_agent_patterns.py --repeats 3             # p95 latency

Exit code 0 iff every selected pattern passes.

See `docs/superpowers/specs/2026-04-19-agent-patterns-design.md` for
design rationale.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
RESULTS_DIR = TESTS_DIR / "results"
BASELINE_PATH = TESTS_DIR / "agent_patterns" / "baseline.json"

# `tests/` isn't a package (no __init__.py — pytest doesn't need one),
# so add this script's directory to sys.path before importing the
# `agent_patterns` subpackage. Keeps the script invocable directly via
# `python tests/test_agent_patterns.py` without pip install.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# `from trawl import fetch_relevant` would force every harness invocation
# to import the heavy pipeline (httpx, lxml, trafilatura, ...). Keep the
# import inside _run_operation so `--dry-run` stays import-free.
from agent_patterns import (  # noqa: E402  (sys.path tweak above)
    Pattern,
    PatternStep,
    PatternValidationError,
    load_all_patterns,
    load_shard,
)
from agent_patterns.schema import evaluate_comparison  # noqa: E402
REGRESSION_TOLERANCE = 1.20  # +20% over baseline counts as regression


# Result types --------------------------------------------------------


@dataclass
class StepOutcome:
    op: str
    url: str | None
    query: str | None
    elapsed_ms: int
    measurements: dict[str, Any]  # PipelineResult-as-dict (or {} for dry-run)
    assertion_failures: list[str] = field(default_factory=list)
    budget_failures: list[str] = field(default_factory=list)


@dataclass
class PatternOutcome:
    id: str
    shard: str
    category: str
    repeats: int
    total_ms_p95: int
    steps: list[StepOutcome] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(
            not s.assertion_failures and not s.budget_failures
            for s in self.steps
        )


# Filtering -----------------------------------------------------------


def _filter_patterns(
    patterns: list[Pattern],
    *,
    shard: str | None,
    only: str | None,
    category: str | None,
    primary_agent: str | None,
    limit: int | None,
) -> list[Pattern]:
    out = patterns
    if shard:
        out = [p for p in out if p.shard == shard]
    if only:
        out = [p for p in out if p.id == only]
    if category:
        out = [p for p in out if p.category == category]
    if primary_agent:
        out = [p for p in out if primary_agent in p.primary_agent]
    if limit:
        out = out[:limit]
    return out


# Operation runner ----------------------------------------------------


def _run_operation(
    op: str,
    url: str,
    query: str | None,
    *,
    dry_run: bool,
) -> tuple[dict[str, Any], int]:
    """Execute one operation. Returns (result_dict, elapsed_ms).

    In dry-run mode returns ({}, 0) without importing the pipeline.
    """
    if dry_run:
        return {}, 0

    if op == "fetch_page":
        from trawl import fetch_relevant, to_dict

        t0 = time.monotonic()
        result = fetch_relevant(url, query or "")
        elapsed = int((time.monotonic() - t0) * 1000)
        return to_dict(result), elapsed

    if op == "profile_page":
        # Lazy import — profile generation requires the VLM endpoint and
        # we don't want to drag the heavy imports for dry-run.
        from trawl.profiles import generate_profile

        t0 = time.monotonic()
        result = generate_profile(url)
        elapsed = int((time.monotonic() - t0) * 1000)
        # generate_profile returns either a summary_dict (success) or an
        # {ok: False, ...} failure dict. Normalise so the harness can
        # uniformly assert on profile_used / error_is_none for the next
        # step's fetch_page.
        return result, elapsed

    raise ValueError(f"unknown op {op!r}")


def _resolve_step(step: PatternStep, prior: list[StepOutcome]) -> tuple[str, str | None]:
    """Resolve url/query for a step, honoring `ref` back-references."""
    if step.ref is not None:
        ref_step = prior[step.ref]
        return ref_step.url, ref_step.query
    return step.url or "", step.query


# Assertion evaluator -------------------------------------------------


def _evaluate_assertions(
    assertions: dict[str, Any],
    measurements: dict[str, Any],
) -> list[str]:
    """Return a list of human-readable failure messages (empty = all pass)."""
    fails: list[str] = []
    chunks = measurements.get("chunks") or []
    blob = "\n\n".join(
        ((c.get("heading") or "") + "\n" + (c.get("text") or ""))
        for c in chunks
    )

    for key, expected in assertions.items():
        if key == "chunks_contain_all":
            missing = [s for s in expected if s not in blob]
            if missing:
                fails.append(f"chunks_contain_all: missing {missing!r}")
        elif key == "chunks_contain_any":
            if not any(s in blob for s in expected):
                fails.append(f"chunks_contain_any: none of {expected!r} present")
        elif key == "chunks_contain_pattern":
            if not re.search(expected, blob):
                fails.append(f"chunks_contain_pattern: regex {expected!r} did not match")
        elif key == "n_chunks_returned":
            actual = len(chunks)
            if not evaluate_comparison(expected, actual):
                fails.append(f"n_chunks_returned: expected {expected!r}, got {actual}")
        elif key == "profile_used":
            actual = bool(measurements.get("profile_used"))
            if actual is not bool(expected):
                fails.append(f"profile_used: expected {expected}, got {actual}")
        elif key == "path":
            actual = measurements.get("path")
            if actual != expected:
                fails.append(f"path: expected {expected!r}, got {actual!r}")
        elif key == "fetcher_used":
            actual = measurements.get("fetcher")
            if actual != expected:
                fails.append(f"fetcher_used: expected {expected!r}, got {actual!r}")
        elif key == "error_is_none":
            actual_err = measurements.get("error")
            actual = actual_err is None
            if actual is not bool(expected):
                fails.append(f"error_is_none: expected {expected}, got error={actual_err!r}")
        elif key == "error_contains":
            actual_err = measurements.get("error") or ""
            if expected not in actual_err:
                fails.append(f"error_contains: {expected!r} not in {actual_err!r}")
        elif key == "suggest_profile":
            actual = bool(measurements.get("suggest_profile"))
            if actual is not bool(expected):
                fails.append(f"suggest_profile: expected {expected}, got {actual}")
        elif key == "content_type":
            actual = measurements.get("content_type")
            if actual != expected:
                fails.append(f"content_type: expected {expected!r}, got {actual!r}")
        elif key == "truncated":
            actual = bool(measurements.get("truncated"))
            if actual is not bool(expected):
                fails.append(f"truncated: expected {expected}, got {actual}")
        elif key == "excerpts_min_count":
            actual = len(measurements.get("excerpts") or [])
            spec = expected if isinstance(expected, str) else f">= {int(expected)}"
            if not evaluate_comparison(spec, actual):
                fails.append(f"excerpts_min_count: expected {expected!r}, got {actual}")
        elif key == "outbound_links_contain_any":
            links = measurements.get("outbound_links") or []
            haystack = "\n".join(
                (lk.get("url") or "") + "\n" + (lk.get("anchor_text") or "")
                for lk in links
            )
            if not any(s in haystack for s in expected):
                fails.append(
                    f"outbound_links_contain_any: none of {expected!r} present "
                    f"in {len(links)} links"
                )
        elif key == "page_entities_contain_any":
            entities = measurements.get("page_entities") or []
            haystack = "\n".join(entities)
            if not any(s in haystack for s in expected):
                fails.append(
                    f"page_entities_contain_any: none of {expected!r} present "
                    f"in {len(entities)} entities"
                )
        elif key == "chain_hints_has_key":
            hints = measurements.get("chain_hints") or {}
            if expected not in hints:
                fails.append(
                    f"chain_hints_has_key: {expected!r} not in keys {sorted(hints)!r}"
                )
    return fails


def _evaluate_budgets(
    budgets: dict[str, Any],
    measurements: dict[str, Any],
    p95_total_ms: int,
) -> list[str]:
    fails: list[str] = []
    if "total_ms_p95" in budgets:
        cap = int(budgets["total_ms_p95"])
        if p95_total_ms > cap:
            fails.append(f"total_ms_p95: {p95_total_ms} > {cap}")
    if "output_chars_max" in budgets:
        cap = int(budgets["output_chars_max"])
        actual = int(measurements.get("output_chars") or 0)
        if actual > cap:
            fails.append(f"output_chars_max: {actual} > {cap}")
    if "n_chunks_max" in budgets:
        cap = int(budgets["n_chunks_max"])
        actual = len(measurements.get("chunks") or [])
        if actual > cap:
            fails.append(f"n_chunks_max: {actual} > {cap}")
    return fails


# Pattern runner ------------------------------------------------------


def _run_pattern(
    pattern: Pattern,
    *,
    dry_run: bool,
    repeats: int,
    verbose: bool,
) -> PatternOutcome:
    outcome = PatternOutcome(
        id=pattern.id,
        shard=pattern.shard,
        category=pattern.category,
        repeats=repeats if not dry_run else 0,
        total_ms_p95=0,
    )

    try:
        if pattern.is_multi_op:
            steps = pattern.steps
        else:
            # Single-op patterns are normalised into a 1-element step list
            # so the same evaluator handles both shapes.
            steps = [
                PatternStep(
                    op="fetch_page",
                    url=pattern.url,
                    query=pattern.query,
                    assertions=pattern.assertions,
                    budgets=pattern.budgets,
                )
            ]

        for step in steps:
            url, query = _resolve_step(step, outcome.steps)

            # Repeat measurements (only meaningful for live + the final step
            # of single-op patterns; for multi-op we only repeat the last
            # step that has a budget — keep it simple and repeat each step
            # the same way).
            measurements: dict[str, Any] = {}
            elapsed_samples: list[int] = []
            for _ in range(repeats):
                measurements, elapsed = _run_operation(
                    step.op, url, query, dry_run=dry_run
                )
                elapsed_samples.append(elapsed)

            elapsed_p95 = (
                int(_p95(elapsed_samples)) if elapsed_samples else 0
            )

            assertion_failures = (
                _evaluate_assertions(step.assertions, measurements)
                if not dry_run else []
            )
            budget_failures = (
                _evaluate_budgets(step.budgets, measurements, elapsed_p95)
                if not dry_run else []
            )

            outcome.steps.append(
                StepOutcome(
                    op=step.op,
                    url=url,
                    query=query,
                    elapsed_ms=elapsed_p95,
                    measurements=measurements if verbose or not dry_run else {},
                    assertion_failures=assertion_failures,
                    budget_failures=budget_failures,
                )
            )

        outcome.total_ms_p95 = sum(s.elapsed_ms for s in outcome.steps)
    except Exception as e:  # noqa: BLE001
        outcome.error = f"{type(e).__name__}: {e}"
    return outcome


def _p95(samples: list[int]) -> float:
    if not samples:
        return 0.0
    if len(samples) == 1:
        return float(samples[0])
    # statistics.quantiles needs n >= 2.
    qs = statistics.quantiles(samples, n=20)  # 5% buckets
    return qs[-1]  # 95th percentile


# Reporting -----------------------------------------------------------


def _print_pattern_summary(outcome: PatternOutcome, *, verbose: bool) -> None:
    status = "PASS" if outcome.passed else "FAIL"
    head = f"[{status}] {outcome.shard}/{outcome.id} ({outcome.category})"
    if outcome.repeats:
        head += f"  total_p95={outcome.total_ms_p95}ms (repeats={outcome.repeats})"
    print(head)
    if outcome.error:
        print(f"    error: {outcome.error}")
        return
    for i, step in enumerate(outcome.steps):
        for f in step.assertion_failures:
            print(f"    step {i} ({step.op}): assertion failed → {f}")
        for f in step.budget_failures:
            print(f"    step {i} ({step.op}): budget failed → {f}")
    if verbose:
        for i, step in enumerate(outcome.steps):
            m = step.measurements
            print(
                f"    step {i} ({step.op}) {step.elapsed_ms}ms "
                f"chunks={len(m.get('chunks') or [])} "
                f"path={m.get('path')!r} "
                f"profile_used={m.get('profile_used')}"
            )


def _write_results(outcomes: list[PatternOutcome], *, results_dir: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out_dir = results_dir / f"agent_patterns_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # JSONL of every outcome
    jsonl = out_dir / "patterns.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for o in outcomes:
            f.write(json.dumps(_outcome_to_dict(o), ensure_ascii=False) + "\n")

    # summary.md — pivot by shard×category
    summary = _render_summary(outcomes)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")

    # failures detail
    fdir = out_dir / "failures"
    fdir.mkdir(exist_ok=True)
    for o in outcomes:
        if o.passed:
            continue
        (fdir / f"{o.id}.md").write_text(_render_failure(o), encoding="utf-8")

    return out_dir


def _outcome_to_dict(o: PatternOutcome) -> dict:
    d = asdict(o)
    d["passed"] = o.passed
    return d


def _render_summary(outcomes: list[PatternOutcome]) -> str:
    by_shard: dict[str, list[PatternOutcome]] = {}
    for o in outcomes:
        by_shard.setdefault(o.shard, []).append(o)

    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.passed)
    lines = [
        "# Agent patterns — run summary",
        "",
        f"- Total: **{passed}/{total}** patterns passed",
        "",
    ]
    for shard in sorted(by_shard):
        items = by_shard[shard]
        sp = sum(1 for o in items if o.passed)
        lines.append(f"## {shard} — {sp}/{len(items)}")
        lines.append("")
        lines.append("| pattern | category | result | total_p95 |")
        lines.append("|---|---|---|---|")
        for o in items:
            status = "PASS" if o.passed else "FAIL"
            lines.append(
                f"| `{o.id}` | {o.category} | {status} | {o.total_ms_p95}ms |"
            )
        lines.append("")
    return "\n".join(lines)


def _render_failure(o: PatternOutcome) -> str:
    lines = [f"# {o.id} — FAIL", "", f"- shard: `{o.shard}`",
             f"- category: `{o.category}`", ""]
    if o.error:
        lines += ["## error", "", "```", o.error, "```", ""]
    for i, step in enumerate(o.steps):
        if not (step.assertion_failures or step.budget_failures):
            continue
        lines += [f"## step {i} — {step.op}", "",
                  f"- url: `{step.url}`", f"- query: `{step.query}`", ""]
        for f in step.assertion_failures:
            lines.append(f"- assertion: {f}")
        for f in step.budget_failures:
            lines.append(f"- budget: {f}")
        lines.append("")
    return "\n".join(lines)


# Baseline / regression -----------------------------------------------


def _load_baseline() -> dict[str, dict]:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_baseline(outcomes: list[PatternOutcome]) -> None:
    payload = {
        o.id: {
            "total_ms_p95": o.total_ms_p95,
            "category": o.category,
            "shard": o.shard,
        }
        for o in outcomes
        if o.passed and o.total_ms_p95 > 0
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"baseline written: {BASELINE_PATH} ({len(payload)} entries)")


def _check_regression(outcomes: list[PatternOutcome]) -> list[str]:
    baseline = _load_baseline()
    if not baseline:
        print("warning: no baseline found at", BASELINE_PATH, "— skipping regression check")
        return []
    regressions: list[str] = []
    for o in outcomes:
        if not o.passed:
            continue
        prev = baseline.get(o.id)
        if not prev:
            continue
        prev_ms = int(prev.get("total_ms_p95") or 0)
        if prev_ms <= 0:
            continue
        if o.total_ms_p95 > prev_ms * REGRESSION_TOLERANCE:
            regressions.append(
                f"{o.id}: {o.total_ms_p95}ms > {prev_ms}ms × {REGRESSION_TOLERANCE} "
                f"(baseline + {int((REGRESSION_TOLERANCE - 1) * 100)}%)"
            )
    return regressions


# CLI -----------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--shard")
    p.add_argument("--only", help="run only the pattern with this id")
    p.add_argument("--category")
    p.add_argument("--primary-agent")
    p.add_argument("--limit", type=int)
    p.add_argument("--repeats", type=int, default=1,
                   help="repeat each measurement N times for p95 (live mode only)")
    p.add_argument("--dry-run", action="store_true",
                   help="schema validation + filter, no live fetches")
    p.add_argument("--baseline", action="store_true",
                   help="write current p95 measurements as new baseline")
    p.add_argument("--regression", action="store_true",
                   help="fail if any pattern's p95 exceeds baseline + 20%%")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        if args.shard:
            patterns = load_shard(args.shard)
        else:
            patterns = load_all_patterns()
    except (FileNotFoundError, PatternValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    selected = _filter_patterns(
        patterns,
        shard=args.shard,
        only=args.only,
        category=args.category,
        primary_agent=args.primary_agent,
        limit=args.limit,
    )

    if not selected:
        print("no patterns matched the filters", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"dry-run: {len(selected)} pattern(s) parsed and validated")
        for p in selected:
            print(f"  [{p.shard}] {p.id} ({p.category}) — {p.description[:80]}")
        return 0

    outcomes = [
        _run_pattern(p, dry_run=False, repeats=args.repeats, verbose=args.verbose)
        for p in selected
    ]
    for o in outcomes:
        _print_pattern_summary(o, verbose=args.verbose)

    out_dir = _write_results(outcomes, results_dir=RESULTS_DIR)
    print(f"\nresults: {out_dir}")

    if args.baseline:
        _write_baseline(outcomes)

    failed = [o for o in outcomes if not o.passed]
    regressions = _check_regression(outcomes) if args.regression else []
    for r in regressions:
        print(f"REGRESSION: {r}", file=sys.stderr)

    print(
        f"\n{len(outcomes) - len(failed)}/{len(outcomes)} pass"
        + (f", {len(regressions)} regression(s)" if regressions else "")
    )

    return 0 if not failed and not regressions else 1


if __name__ == "__main__":
    sys.exit(main())
