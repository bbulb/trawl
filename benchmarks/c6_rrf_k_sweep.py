"""C6 follow-up — RRF k tuning spike.

Sweeps `TRAWL_HYBRID_RRF_K` over {10, 30, 60, 100} plus a
`dense_only` baseline, running each mode against the 16
`code_heavy_query` patterns in `tests/agent_patterns/coding.yaml` and
reporting per-mode assertion pass rate, rank-1 chunk identity change
rate, and retrieval latency. Also runs the 15-case parity matrix once
per k to guard against regressions.

Pre-registered gates (design doc
``docs/superpowers/specs/2026-04-20-c6-rrf-k-tuning-design.md``):
    (a) Adopt some k if parity 15/15 AND net_assertion_delta >= +1 AND
        flipped_to_fail <= 1 for that k.
    (b) Keep k=60 if any k is parity-safe but (a) fails for all.
    (c) Drop any k that breaks parity.

Invoke:
    python benchmarks/c6_rrf_k_sweep.py --dry-run
    python benchmarks/c6_rrf_k_sweep.py                 # full sweep
    python benchmarks/c6_rrf_k_sweep.py --iterations 3
    python benchmarks/c6_rrf_k_sweep.py --skip-parity   # fast path

Writes `benchmarks/results/c6-rrf-k-sweep/<ts>/`:
    summary.json   aggregated metrics + diff vs baseline + gate decision
    report.md      human-readable report

Exit code:
    0  — measurement completed (regardless of gate outcome)
    1  — measurement failure (>25% patterns errored in any mode, or
         parity subprocess failed to run)
    2  — infra failure (embedding server unreachable)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_ROOT = BENCH_DIR / "results" / "c6-rrf-k-sweep"
TESTS_DIR = REPO_ROOT / "tests"

# Make agent_patterns importable for loading the code_heavy_query shard.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_patterns import load_shard  # noqa: E402
from agent_patterns.schema import evaluate_comparison  # noqa: E402

K_VALUES = [10, 30, 60, 100]
BASELINE_MODE = "dense_only"
ERROR_RATE_MAX = 0.25  # >25% errored patterns in any mode ⇒ measurement failure


# --- Data classes -----------------------------------------------------


@dataclass
class RunOutcome:
    iteration: int
    ok: bool
    error: str | None = None
    retrieval_ms: int | None = None
    total_ms: int | None = None
    n_chunks_returned: int | None = None
    n_chunks_total: int | None = None
    top1_sig: str | None = None
    top1_score: float | None = None
    assertion_failures: list[str] = field(default_factory=list)

    @property
    def assertion_pass(self) -> bool:
        return self.ok and not self.assertion_failures


@dataclass
class PatternRunGroup:
    """All runs for one (pattern, mode) pair."""
    pattern_id: str
    mode: str
    runs: list[RunOutcome] = field(default_factory=list)

    def stable_assertion_pass(self) -> bool:
        """A pattern passes only if all iterations pass.

        This is stricter than "any iteration passes" — we want the
        per-mode comparison to be robust to flakes.
        """
        return bool(self.runs) and all(r.assertion_pass for r in self.runs)

    def rank1_sig_stable(self) -> str | None:
        sigs = {r.top1_sig for r in self.runs if r.ok and r.top1_sig}
        if len(sigs) == 1:
            return sigs.pop()
        return None


# --- Assertion evaluator (subset used by code_heavy_query) -----------


def _rank1_signature(chunks: list[dict]) -> str | None:
    """Fingerprint of the rank-1 chunk — heading + first 80 body chars."""
    if not chunks:
        return None
    top = chunks[0]
    body = (top.get("text") or "")[:80].strip()
    heading = top.get("heading") or ""
    return hashlib.sha1(f"{heading}||{body}".encode()).hexdigest()[:12]


def _evaluate_assertions(assertions: dict[str, Any], measurements: dict[str, Any]) -> list[str]:
    """Re-implementation of the subset of assertion keys used by
    `code_heavy_query` patterns. Keeps this runner independent of
    `test_agent_patterns.py`'s private helpers.
    """
    fails: list[str] = []
    chunks = measurements.get("chunks") or []
    blob = "\n\n".join(((c.get("heading") or "") + "\n" + (c.get("text") or "")) for c in chunks)

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
        elif key == "error_is_none":
            actual_err = measurements.get("error")
            actual = actual_err is None
            if actual is not bool(expected):
                fails.append(f"error_is_none: expected {expected}, got error={actual_err!r}")
        elif key == "profile_used":
            actual = bool(measurements.get("profile_used"))
            if actual is not bool(expected):
                fails.append(f"profile_used: expected {expected}, got {actual}")
        # Other keys in the schema aren't used by code_heavy_query;
        # silently ignore so this runner stays focused.
    return fails


# --- Environment handling --------------------------------------------


def _set_mode_env(mode: str) -> None:
    """Apply env vars for a sweep mode. Modes are either
    `dense_only` or `hybrid_k{N}`.
    """
    if mode == "dense_only":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "0"
        os.environ.pop("TRAWL_HYBRID_RRF_K", None)
    elif mode.startswith("hybrid_k"):
        k = int(mode[len("hybrid_k"):])
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = str(k)
    else:
        raise ValueError(f"unknown mode {mode!r}")


def _mode_for_k(k: int | None) -> str:
    return BASELINE_MODE if k is None else f"hybrid_k{k}"


# --- Run one pattern one iteration -----------------------------------


def _run_once(pattern: Any, iteration: int, verbose: bool) -> RunOutcome:
    from trawl import fetch_relevant, to_dict

    outcome = RunOutcome(iteration=iteration, ok=False)
    try:
        t0 = time.monotonic()
        result = fetch_relevant(pattern.url, pattern.query or "")
        outcome.total_ms = int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        outcome.error = f"{type(e).__name__}: {e}"
        if verbose:
            print(f"    iter {iteration} EXC {outcome.error}", file=sys.stderr)
        return outcome

    data = to_dict(result)
    outcome.ok = data.get("error") is None
    outcome.error = data.get("error")
    outcome.retrieval_ms = data.get("retrieval_ms")
    chunks = data.get("chunks") or []
    outcome.n_chunks_returned = len(chunks)
    outcome.n_chunks_total = data.get("n_chunks_total")
    outcome.top1_sig = _rank1_signature(chunks)
    outcome.top1_score = chunks[0].get("score") if chunks else None
    outcome.assertion_failures = _evaluate_assertions(pattern.assertions, data)

    if verbose:
        status = "ok " if outcome.ok else "err"
        ap = "PASS" if not outcome.assertion_failures else "FAIL"
        print(
            f"    iter {iteration} [{status}] ast={ap} "
            f"retr={outcome.retrieval_ms}ms "
            f"top1={outcome.top1_sig} score={outcome.top1_score:.3f}"
            if outcome.top1_score is not None
            else f"    iter {iteration} [{status}] ast={ap}",
            file=sys.stderr,
        )
    return outcome


# --- Aggregation -----------------------------------------------------


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    idx = (len(vs) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(vs) - 1)
    if lo == hi:
        return float(vs[lo])
    frac = idx - lo
    return float(vs[lo] * (1 - frac) + vs[hi] * frac)


def _mode_aggregate(groups: list[PatternRunGroup]) -> dict[str, Any]:
    pattern_stats = []
    retr_values: list[float] = []
    assertion_pass = 0
    error_count = 0

    for g in groups:
        rs = [r for r in g.runs if r.ok]
        if not rs:
            error_count += 1
        retr = [float(r.retrieval_ms) for r in rs if r.retrieval_ms is not None]
        retr_values.extend(retr)
        passed = g.stable_assertion_pass()
        if passed:
            assertion_pass += 1

        fail_summaries: list[str] = []
        seen: set[str] = set()
        for r in g.runs:
            for f in r.assertion_failures:
                if f not in seen:
                    fail_summaries.append(f)
                    seen.add(f)

        pattern_stats.append({
            "id": g.pattern_id,
            "assertion_pass": passed,
            "assertion_failures": fail_summaries,
            "top1_sig": g.rank1_sig_stable(),
            "top1_scores": [r.top1_score for r in g.runs if r.top1_score is not None],
            "retrieval_ms": [r.retrieval_ms for r in g.runs if r.retrieval_ms is not None],
            "n_chunks_returned": [r.n_chunks_returned for r in g.runs if r.n_chunks_returned is not None],
            "n_chunks_total": [r.n_chunks_total for r in g.runs if r.n_chunks_total is not None],
            "errors": [r.error for r in g.runs if not r.ok],
        })

    return {
        "assertion_pass": assertion_pass,
        "assertion_total": len(groups),
        "error_count": error_count,
        "retrieval_ms_median": statistics.median(retr_values) if retr_values else None,
        "retrieval_ms_p95": _pct(retr_values, 0.95),
        "patterns": pattern_stats,
    }


def _diff_vs_baseline(baseline: dict[str, Any], experiment: dict[str, Any]) -> dict[str, Any]:
    b_pats = {p["id"]: p for p in baseline["patterns"]}
    flipped_to_pass: list[str] = []
    flipped_to_fail: list[str] = []
    top1_changed: list[str] = []
    top1_stable: list[str] = []

    for e_pat in experiment["patterns"]:
        b_pat = b_pats.get(e_pat["id"])
        if b_pat is None:
            continue
        if b_pat["assertion_pass"] and not e_pat["assertion_pass"]:
            flipped_to_fail.append(e_pat["id"])
        elif not b_pat["assertion_pass"] and e_pat["assertion_pass"]:
            flipped_to_pass.append(e_pat["id"])
        b_sig = b_pat.get("top1_sig")
        e_sig = e_pat.get("top1_sig")
        if b_sig and e_sig and b_sig != e_sig:
            top1_changed.append(e_pat["id"])
        elif b_sig and e_sig and b_sig == e_sig:
            top1_stable.append(e_pat["id"])

    net_delta = experiment["assertion_pass"] - baseline["assertion_pass"]
    return {
        "flipped_to_pass": flipped_to_pass,
        "flipped_to_fail": flipped_to_fail,
        "top1_identity_changed": len(top1_changed),
        "top1_identity_stable": len(top1_stable),
        "top1_changed_ids": top1_changed,
        "net_assertion_delta": net_delta,
    }


# --- Parity subprocess -----------------------------------------------


def _run_parity(k: int, repo_root: Path, verbose: bool) -> dict[str, Any]:
    """Run `tests/test_pipeline.py` with TRAWL_HYBRID_RETRIEVAL=1 and
    the given RRF k. Returns {ok, pass_count, total, exit_code,
    elapsed_s}.
    """
    env = os.environ.copy()
    env["TRAWL_HYBRID_RETRIEVAL"] = "1"
    env["TRAWL_HYBRID_RRF_K"] = str(k)
    t0 = time.monotonic()
    if verbose:
        print(f"  parity k={k}: launching test_pipeline.py ...", file=sys.stderr)
    try:
        cp = subprocess.run(
            [sys.executable, "tests/test_pipeline.py"],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "exit_code": -1,
            "pass_count": None,
            "total": None,
            "elapsed_s": int(time.monotonic() - t0),
            "error": f"timeout: {e}",
        }
    elapsed = int(time.monotonic() - t0)
    out = cp.stdout + "\n" + cp.stderr
    m = re.search(r"(\d+)/(\d+)\s*cases\s*pass", out)
    if not m:
        # Fall back: test_pipeline prints "N/M pass" without 'cases'.
        m = re.search(r"\b(\d+)\s*/\s*(\d+)\s*pass\b", out)
    pc = int(m.group(1)) if m else None
    tot = int(m.group(2)) if m else None
    return {
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "pass_count": pc,
        "total": tot,
        "elapsed_s": elapsed,
        "stdout_tail": "\n".join(out.strip().splitlines()[-12:]),
    }


# --- Gate decision ---------------------------------------------------


def _decide_gate(per_mode: dict[str, Any], diffs: dict[str, Any], parity: dict[str, Any]) -> dict[str, Any]:
    """Apply the pre-registered gates (a / b / c) from the design doc."""
    candidates: list[tuple[int, dict[str, Any]]] = []
    parity_failed_ks: list[int] = []

    for k in K_VALUES:
        mode = _mode_for_k(k)
        diff = diffs.get(mode) or {}
        par = parity.get(str(k)) or {}
        par_ok = par.get("ok", False) and par.get("pass_count") == par.get("total")
        if not par_ok:
            parity_failed_ks.append(k)
            continue
        if diff.get("net_assertion_delta", 0) >= 1 and len(diff.get("flipped_to_fail", [])) <= 1:
            candidates.append((k, diff))

    if candidates:
        # Prefer the k with the largest net delta; break ties by fewer regressions.
        candidates.sort(key=lambda pair: (-pair[1]["net_assertion_delta"], len(pair[1]["flipped_to_fail"])))
        best_k, best_diff = candidates[0]
        return {
            "outcome": "a_adopt",
            "selected_k": best_k,
            "net_assertion_delta": best_diff["net_assertion_delta"],
            "flipped_to_pass": best_diff["flipped_to_pass"],
            "flipped_to_fail": best_diff["flipped_to_fail"],
            "parity_failed_ks": parity_failed_ks,
        }

    any_parity_ok = any(
        (parity.get(str(k)) or {}).get("ok") for k in K_VALUES
    )
    if any_parity_ok:
        return {
            "outcome": "b_retain_60",
            "parity_failed_ks": parity_failed_ks,
        }
    return {
        "outcome": "c_parity_regression",
        "parity_failed_ks": parity_failed_ks,
    }


# --- Report rendering ------------------------------------------------


def _fmt(n: float | int | None, suffix: str = "") -> str:
    if n is None:
        return "-"
    return f"{int(n)}{suffix}" if suffix else f"{int(n)}"


def _render_report(summary: dict[str, Any]) -> str:
    per_mode = summary["per_mode"]
    diffs = summary["diff_vs_baseline"]
    parity = summary["parity"]
    gate = summary["gate_decision"]

    lines: list[str] = []
    lines.append("# C6 RRF-k tuning — sweep report")
    lines.append("")
    lines.append(f"**Generated:** {summary['generated_at']}")
    lines.append(f"**Iterations:** {summary['iterations']}")
    lines.append(f"**Modes:** {', '.join(summary['modes'])}")
    lines.append("")
    lines.append("## Gate decision")
    lines.append("")
    outcome = gate["outcome"]
    lines.append(f"- **Outcome:** `{outcome}`")
    if outcome == "a_adopt":
        lines.append(f"- **Selected k:** {gate['selected_k']}")
        lines.append(f"- **Net assertion delta:** +{gate['net_assertion_delta']}")
        lines.append(f"- **Flipped to pass:** {gate['flipped_to_pass']}")
        lines.append(f"- **Flipped to fail:** {gate['flipped_to_fail']}")
    if gate.get("parity_failed_ks"):
        lines.append(f"- **Parity-regressed k:** {gate['parity_failed_ks']}")
    lines.append("")
    lines.append("## Per-mode summary")
    lines.append("")
    lines.append("| mode | assertion pass | errors | retrieval median | retrieval p95 |")
    lines.append("|---|---|---:|---:|---:|")
    for mode in summary["modes"]:
        m = per_mode[mode]
        lines.append(
            f"| {mode} | {m['assertion_pass']}/{m['assertion_total']} | "
            f"{m['error_count']} | {_fmt(m['retrieval_ms_median'], ' ms')} | "
            f"{_fmt(m['retrieval_ms_p95'], ' ms')} |"
        )
    lines.append("")
    lines.append("## Diff vs baseline (`dense_only`)")
    lines.append("")
    lines.append("| mode | net Δ | flipped→pass | flipped→fail | top1 changed |")
    lines.append("|---|---:|---|---|---:|")
    for mode in summary["modes"]:
        if mode == BASELINE_MODE:
            continue
        d = diffs.get(mode)
        if d is None:
            continue
        net = d["net_assertion_delta"]
        sign = "+" if net >= 0 else ""
        lines.append(
            f"| {mode} | {sign}{net} | {d['flipped_to_pass']} | "
            f"{d['flipped_to_fail']} | "
            f"{d['top1_identity_changed']}/{d['top1_identity_changed'] + d['top1_identity_stable']} |"
        )
    lines.append("")
    lines.append("## Parity (15-case matrix per k)")
    lines.append("")
    lines.append("| k | pass / total | ok | elapsed |")
    lines.append("|---|---|---|---:|")
    for k in K_VALUES:
        p = parity.get(str(k)) or {}
        ok = "PASS" if p.get("ok") else "FAIL"
        pc = p.get("pass_count")
        tot = p.get("total")
        pcs = f"{pc}/{tot}" if pc is not None else "?"
        lines.append(f"| {k} | {pcs} | {ok} | {_fmt(p.get('elapsed_s'), 's')} |")
    lines.append("")
    lines.append("## Per-pattern detail")
    lines.append("")
    pattern_ids = [p["id"] for p in per_mode[BASELINE_MODE]["patterns"]]
    header = "| pattern |" + "|".join(f" {m} " for m in summary["modes"]) + "|"
    sep = "|---|" + "|".join(":---:" for _ in summary["modes"]) + "|"
    lines.append(header)
    lines.append(sep)
    for pid in pattern_ids:
        cells = [f"`{pid}`"]
        for mode in summary["modes"]:
            m = per_mode[mode]
            entry = next((x for x in m["patterns"] if x["id"] == pid), None)
            if entry is None:
                cells.append("?")
                continue
            token = "P" if entry["assertion_pass"] else "F"
            if entry["errors"]:
                token = "E"
            cells.append(token)
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("Legend: P = assertion pass, F = assertion fail, E = error.")
    lines.append("")
    return "\n".join(lines)


# --- Main ------------------------------------------------------------


def _precheck_embedding() -> bool:
    import httpx

    base = os.environ.get("TRAWL_EMBED_URL", "http://localhost:8081/v1")
    health = base.rsplit("/v1", 1)[0] + "/health"
    try:
        httpx.get(health, timeout=3.0).raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"embedding server health check failed: {e}", file=sys.stderr)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--iterations", type=int, default=2,
        help="Runs per (mode, pattern). Default 2.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List mode×pattern plan without fetching.")
    parser.add_argument("--skip-parity", action="store_true", help="Skip the parity subprocess runs.")
    parser.add_argument("--only", help="Run only one pattern id (debug).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    # Load patterns
    try:
        patterns = load_shard("coding")
    except Exception as e:  # noqa: BLE001
        print(f"failed to load coding shard: {e}", file=sys.stderr)
        return 2
    patterns = [p for p in patterns if p.category == "code_heavy_query"]
    if args.only:
        patterns = [p for p in patterns if p.id == args.only]
    if not patterns:
        print("no patterns selected", file=sys.stderr)
        return 2

    modes = [BASELINE_MODE] + [f"hybrid_k{k}" for k in K_VALUES]
    total_runs = len(modes) * len(patterns) * args.iterations

    if args.dry_run:
        print(f"plan: {len(modes)} modes × {len(patterns)} patterns × {args.iterations} iter = {total_runs} runs")
        for m in modes:
            print(f"  mode: {m}")
        for p in patterns:
            print(f"    [{p.id}] {p.url}")
        return 0

    if not _precheck_embedding():
        return 2

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"sweep → {out_dir}", file=sys.stderr)
    print(f"  {len(modes)} modes × {len(patterns)} patterns × {args.iterations} iter = {total_runs} runs", file=sys.stderr)

    # Run each mode as an outer loop so the fetch cache fills on the first
    # pass and later modes measure retrieval only. Patterns iterate inside.
    raw_results: dict[str, list[PatternRunGroup]] = {}
    for mode in modes:
        print(f"\n[mode {mode}]", file=sys.stderr)
        _set_mode_env(mode)
        groups: list[PatternRunGroup] = []
        for i, pat in enumerate(patterns, 1):
            print(f"  [{i}/{len(patterns)}] {pat.id}", file=sys.stderr)
            g = PatternRunGroup(pattern_id=pat.id, mode=mode)
            for it in range(1, args.iterations + 1):
                g.runs.append(_run_once(pat, iteration=it, verbose=args.verbose))
            groups.append(g)
        raw_results[mode] = groups

    # Aggregate
    per_mode = {m: _mode_aggregate(raw_results[m]) for m in modes}
    diffs = {m: _diff_vs_baseline(per_mode[BASELINE_MODE], per_mode[m]) for m in modes if m != BASELINE_MODE}

    # Parity (optional)
    parity: dict[str, Any] = {}
    if args.skip_parity:
        print("\n[parity] skipped (--skip-parity)", file=sys.stderr)
    else:
        print("\n[parity]", file=sys.stderr)
        for k in K_VALUES:
            result = _run_parity(k, repo_root=REPO_ROOT, verbose=args.verbose)
            parity[str(k)] = result
            tag = "PASS" if result.get("ok") else "FAIL"
            print(
                f"  k={k}: {tag} ({result.get('pass_count')}/{result.get('total')}, {result.get('elapsed_s')}s)",
                file=sys.stderr,
            )

    # Error-rate measurement-failure check
    for mode, m in per_mode.items():
        if m["assertion_total"] == 0:
            continue
        rate = m["error_count"] / m["assertion_total"]
        if rate > ERROR_RATE_MAX:
            print(
                f"measurement failure: mode {mode} errored on {m['error_count']}/{m['assertion_total']} patterns",
                file=sys.stderr,
            )
            # Still write results so the failure is persisted.
            gate = {"outcome": "measurement_failure", "reason": f"error rate {rate:.2%} in {mode}"}
            _persist(out_dir, per_mode, diffs, parity, gate, modes, raw_results, args.iterations)
            return 1

    gate = _decide_gate(per_mode, diffs, parity) if parity else {"outcome": "gate_skipped", "reason": "--skip-parity"}
    _persist(out_dir, per_mode, diffs, parity, gate, modes, raw_results, args.iterations)

    print(f"\ngate decision: {gate.get('outcome')}", file=sys.stderr)
    print(f"report: {out_dir}/report.md", file=sys.stderr)
    return 0


def _persist(
    out_dir: Path,
    per_mode: dict[str, Any],
    diffs: dict[str, Any],
    parity: dict[str, Any],
    gate: dict[str, Any],
    modes: list[str],
    raw_results: dict[str, list[PatternRunGroup]],
    iterations: int,
) -> None:
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary = {
        "generated_at": generated_at,
        "iterations": iterations,
        "k_values": K_VALUES,
        "baseline_mode": BASELINE_MODE,
        "modes": modes,
        "per_mode": per_mode,
        "diff_vs_baseline": diffs,
        "parity": parity,
        "gate_decision": gate,
    }
    raw = {
        mode: [
            {
                "id": g.pattern_id,
                "runs": [asdict(r) for r in g.runs],
            }
            for g in groups
        ]
        for mode, groups in raw_results.items()
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "raw_runs.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(_render_report(summary), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
