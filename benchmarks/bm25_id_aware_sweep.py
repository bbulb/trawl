"""C6 follow-up — identifier-aware BM25 tokenizer spike.

Compares three retrieval modes on the 16 ``code_heavy_query`` patterns
in ``tests/agent_patterns/coding.yaml``:

    - ``dense_only``        (baseline reference, hybrid off)
    - ``hybrid_legacy``     (hybrid on, BM25 legacy tokenizer, k=60)
    - ``hybrid_id_aware``   (hybrid on, BM25 identifier-aware, k=60)

The gate compares ``hybrid_id_aware`` against ``hybrid_legacy`` (not
``dense_only``) — the tokenizer change only affects the sparse leg, so
legacy is the right A/B reference.

Pre-registered gates (design doc
``docs/superpowers/specs/2026-04-20-bm25-id-aware-tokenizer-design.md``):

    (a) Adopt identifier-aware if parity 15/15 on the id-aware path
        AND net_assertion_delta (vs hybrid_legacy) >= +1 AND
        flipped_to_fail == 0.
    (b) Reject if parity holds but (a) is not met.
    (c) Drop parity-regressing mode.

Invoke:
    python benchmarks/bm25_id_aware_sweep.py --dry-run
    python benchmarks/bm25_id_aware_sweep.py                 # full sweep
    python benchmarks/bm25_id_aware_sweep.py --iterations 3
    python benchmarks/bm25_id_aware_sweep.py --skip-parity   # fast path
    python benchmarks/bm25_id_aware_sweep.py --only <pattern_id>

Writes ``benchmarks/results/bm25-id-aware-sweep/<ts>/``:
    summary.json   aggregated metrics + diff vs baseline + gate decision
    raw_runs.json  per-run outcomes
    report.md      human-readable report

Exit code:
    0  — measurement completed (regardless of gate outcome)
    1  — measurement failure (>25% patterns errored in any mode, or
         parity subprocess failed to launch)
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
RESULTS_ROOT = BENCH_DIR / "results" / "bm25-id-aware-sweep"
TESTS_DIR = REPO_ROOT / "tests"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_patterns import load_shard  # noqa: E402
from agent_patterns.schema import evaluate_comparison  # noqa: E402

MODES = ["dense_only", "hybrid_legacy", "hybrid_id_aware"]
BASELINE_MODE = "hybrid_legacy"  # the A/B reference for tokenizer isolation
ERROR_RATE_MAX = 0.25


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
    pattern_id: str
    mode: str
    runs: list[RunOutcome] = field(default_factory=list)

    def stable_assertion_pass(self) -> bool:
        return bool(self.runs) and all(r.assertion_pass for r in self.runs)

    def rank1_sig_stable(self) -> str | None:
        sigs = {r.top1_sig for r in self.runs if r.ok and r.top1_sig}
        if len(sigs) == 1:
            return sigs.pop()
        return None


# --- Assertion evaluator (mirrors c6_rrf_k_sweep.py) -----------------


def _rank1_signature(chunks: list[dict]) -> str | None:
    if not chunks:
        return None
    top = chunks[0]
    body = (top.get("text") or "")[:80].strip()
    heading = top.get("heading") or ""
    return hashlib.sha1(f"{heading}||{body}".encode()).hexdigest()[:12]


def _evaluate_assertions(assertions: dict[str, Any], measurements: dict[str, Any]) -> list[str]:
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
    return fails


# --- Environment handling --------------------------------------------


def _set_mode_env(mode: str) -> None:
    """Apply env vars for a sweep mode."""
    if mode == "dense_only":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "0"
        os.environ.pop("TRAWL_HYBRID_RRF_K", None)
        os.environ["TRAWL_BM25_IDENTIFIER_AWARE"] = "0"
    elif mode == "hybrid_legacy":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = "60"
        os.environ["TRAWL_BM25_IDENTIFIER_AWARE"] = "0"
    elif mode == "hybrid_id_aware":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = "60"
        os.environ["TRAWL_BM25_IDENTIFIER_AWARE"] = "1"
    else:
        raise ValueError(f"unknown mode {mode!r}")


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
        score_s = f"score={outcome.top1_score:.3f}" if outcome.top1_score is not None else ""
        print(
            f"    iter {iteration} [{status}] ast={ap} "
            f"retr={outcome.retrieval_ms}ms "
            f"top1={outcome.top1_sig} {score_s}",
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


def _run_parity(mode: str, repo_root: Path, verbose: bool) -> dict[str, Any]:
    """Run `tests/test_pipeline.py` under the given sweep mode."""
    env = os.environ.copy()
    if mode == "dense_only":
        env["TRAWL_HYBRID_RETRIEVAL"] = "0"
        env.pop("TRAWL_HYBRID_RRF_K", None)
        env["TRAWL_BM25_IDENTIFIER_AWARE"] = "0"
    elif mode == "hybrid_legacy":
        env["TRAWL_HYBRID_RETRIEVAL"] = "1"
        env["TRAWL_HYBRID_RRF_K"] = "60"
        env["TRAWL_BM25_IDENTIFIER_AWARE"] = "0"
    elif mode == "hybrid_id_aware":
        env["TRAWL_HYBRID_RETRIEVAL"] = "1"
        env["TRAWL_HYBRID_RRF_K"] = "60"
        env["TRAWL_BM25_IDENTIFIER_AWARE"] = "1"
    else:
        raise ValueError(f"unknown mode {mode!r}")

    t0 = time.monotonic()
    if verbose:
        print(f"  parity {mode}: launching test_pipeline.py ...", file=sys.stderr)
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


def _decide_gate(
    per_mode: dict[str, Any],
    diffs: dict[str, Any],
    parity: dict[str, Any],
) -> dict[str, Any]:
    """Apply the pre-registered gates (a / b / c) from the design doc.

    baseline = hybrid_legacy; experiment = hybrid_id_aware.
    """
    par_exp = parity.get("hybrid_id_aware") or {}
    exp_par_ok = par_exp.get("ok", False) and par_exp.get("pass_count") == par_exp.get("total")
    par_base = parity.get("hybrid_legacy") or {}
    base_par_ok = par_base.get("ok", False) and par_base.get("pass_count") == par_base.get("total")

    parity_failed_modes: list[str] = []
    if not exp_par_ok and par_exp:
        parity_failed_modes.append("hybrid_id_aware")
    if not base_par_ok and par_base:
        parity_failed_modes.append("hybrid_legacy")

    if not exp_par_ok:
        return {
            "outcome": "c_parity_regression",
            "parity_failed_modes": parity_failed_modes,
            "reason": "hybrid_id_aware parity regression",
        }

    diff = diffs.get("hybrid_id_aware") or {}
    net_delta = diff.get("net_assertion_delta", 0)
    flipped_to_fail = diff.get("flipped_to_fail", []) or []

    if net_delta >= 1 and len(flipped_to_fail) == 0:
        return {
            "outcome": "a_adopt",
            "net_assertion_delta": net_delta,
            "flipped_to_pass": diff.get("flipped_to_pass", []),
            "flipped_to_fail": flipped_to_fail,
            "parity_failed_modes": parity_failed_modes,
        }

    return {
        "outcome": "b_reject",
        "net_assertion_delta": net_delta,
        "flipped_to_pass": diff.get("flipped_to_pass", []),
        "flipped_to_fail": flipped_to_fail,
        "parity_failed_modes": parity_failed_modes,
        "reason": (
            f"net_delta={net_delta} (need >=1), "
            f"flipped_to_fail={flipped_to_fail} (need ==[])"
        ),
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
    lines.append("# BM25 identifier-aware tokenizer — sweep report")
    lines.append("")
    lines.append(f"**Generated:** {summary['generated_at']}")
    lines.append(f"**Iterations:** {summary['iterations']}")
    lines.append(f"**Modes:** {', '.join(summary['modes'])}")
    lines.append(f"**Baseline for diff:** `{summary['baseline_mode']}`")
    lines.append("")
    lines.append("## Gate decision")
    lines.append("")
    outcome = gate.get("outcome", "unknown")
    lines.append(f"- **Outcome:** `{outcome}`")
    if outcome == "a_adopt":
        lines.append(f"- **Net assertion delta:** +{gate['net_assertion_delta']}")
        lines.append(f"- **Flipped to pass:** {gate['flipped_to_pass']}")
        lines.append(f"- **Flipped to fail:** {gate['flipped_to_fail']}")
    elif outcome == "b_reject":
        lines.append(f"- **Net assertion delta:** {gate['net_assertion_delta']}")
        lines.append(f"- **Flipped to pass:** {gate['flipped_to_pass']}")
        lines.append(f"- **Flipped to fail:** {gate['flipped_to_fail']}")
        lines.append(f"- **Reason:** {gate.get('reason', '')}")
    elif outcome == "c_parity_regression":
        lines.append(f"- **Reason:** {gate.get('reason', '')}")
        lines.append(f"- **Parity-regressed:** {gate.get('parity_failed_modes', [])}")
    lines.append("")
    lines.append("## Per-mode summary")
    lines.append("")
    lines.append("| mode | assertion pass | errors | retrieval median | retrieval p95 |")
    lines.append("|---|---|---:|---:|---:|")
    for mode in summary["modes"]:
        m = per_mode[mode]
        lines.append(
            f"| `{mode}` | {m['assertion_pass']}/{m['assertion_total']} | "
            f"{m['error_count']} | {_fmt(m['retrieval_ms_median'], ' ms')} | "
            f"{_fmt(m['retrieval_ms_p95'], ' ms')} |"
        )
    lines.append("")
    lines.append(f"## Diff vs baseline (`{summary['baseline_mode']}`)")
    lines.append("")
    lines.append("| mode | net Δ | flipped→pass | flipped→fail | top1 changed |")
    lines.append("|---|---:|---|---|---:|")
    for mode in summary["modes"]:
        if mode == summary["baseline_mode"]:
            continue
        d = diffs.get(mode)
        if d is None:
            continue
        net = d["net_assertion_delta"]
        sign = "+" if net >= 0 else ""
        top_changed_total = d["top1_identity_changed"] + d["top1_identity_stable"]
        lines.append(
            f"| `{mode}` | {sign}{net} | {d['flipped_to_pass']} | "
            f"{d['flipped_to_fail']} | "
            f"{d['top1_identity_changed']}/{top_changed_total} |"
        )
    lines.append("")
    lines.append("## Parity (15-case matrix)")
    lines.append("")
    lines.append("| mode | pass / total | ok | elapsed |")
    lines.append("|---|---|---|---:|")
    for mode in summary["modes"]:
        p = parity.get(mode) or {}
        if not p:
            continue
        ok = "PASS" if p.get("ok") else "FAIL"
        pc = p.get("pass_count")
        tot = p.get("total")
        pcs = f"{pc}/{tot}" if pc is not None else "?"
        lines.append(f"| `{mode}` | {pcs} | {ok} | {_fmt(p.get('elapsed_s'), 's')} |")
    lines.append("")
    lines.append("## Per-pattern detail")
    lines.append("")
    pattern_ids = [p["id"] for p in per_mode[summary["modes"][0]]["patterns"]]
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

    modes = MODES
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

    per_mode = {m: _mode_aggregate(raw_results[m]) for m in modes}
    diffs = {
        m: _diff_vs_baseline(per_mode[BASELINE_MODE], per_mode[m])
        for m in modes if m != BASELINE_MODE
    }

    parity: dict[str, Any] = {}
    if args.skip_parity:
        print("\n[parity] skipped (--skip-parity)", file=sys.stderr)
    else:
        print("\n[parity]", file=sys.stderr)
        # Only run parity on the two hybrid modes (dense_only parity is
        # already implicitly covered by hybrid_legacy with hybrid off
        # semantically being the same chunk ordering prior to fusion).
        # Running the experiment mode + baseline tokenizer mode gives the
        # gate everything it needs.
        for mode in ("hybrid_legacy", "hybrid_id_aware"):
            result = _run_parity(mode, repo_root=REPO_ROOT, verbose=args.verbose)
            parity[mode] = result
            tag = "PASS" if result.get("ok") else "FAIL"
            print(
                f"  {mode}: {tag} ({result.get('pass_count')}/{result.get('total')}, {result.get('elapsed_s')}s)",
                file=sys.stderr,
            )

    # Measurement-failure check
    for mode, m in per_mode.items():
        if m["assertion_total"] == 0:
            continue
        rate = m["error_count"] / m["assertion_total"]
        if rate > ERROR_RATE_MAX:
            print(
                f"measurement failure: mode {mode} errored on {m['error_count']}/{m['assertion_total']} patterns",
                file=sys.stderr,
            )
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
        "modes": modes,
        "baseline_mode": BASELINE_MODE,
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
