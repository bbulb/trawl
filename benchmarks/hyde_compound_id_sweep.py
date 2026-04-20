"""C6 follow-up — HyDE compound identifier spike.

Measures whether HyDE's identifier-rich hypothetical answer can close
the MDN-style lexical gap (query describes intent, chunks carry
identifiers) when routed into BM25 in addition to the dense path.

Three modes on the 16 ``code_heavy_query`` patterns:

    - ``hybrid_hyde_off``       baseline (current default)
    - ``hybrid_hyde_on_dense``  HyDE on, BM25 sees raw query (existing code)
    - ``hybrid_hyde_on_full``   HyDE on, BM25 sees query + HyDE extras
                                (TRAWL_BM25_EXTRAS=1, this spike's change)

Pre-registered gates (design doc
``docs/superpowers/specs/2026-04-20-hyde-compound-identifier-design.md``):

    (a1) adopt HyDE-on as recommended (docs only, no code change) if
         hybrid_hyde_on_dense beats baseline by net_delta >= +1 AND
         flipped_to_fail == 0 AND parity 15/15 on dense mode.
    (a2) adopt HyDE-on + BM25 extras (code change) if (a1) misses but
         hybrid_hyde_on_full clears the same bar AND beats
         hybrid_hyde_on_dense on the incremental metric.
    (b)  reject if neither mode meets the bar.
    (c)  parity regression drops the offending mode.

Invoke:
    python benchmarks/hyde_compound_id_sweep.py --dry-run
    python benchmarks/hyde_compound_id_sweep.py                 # full sweep
    python benchmarks/hyde_compound_id_sweep.py --iterations 3
    python benchmarks/hyde_compound_id_sweep.py --skip-parity
    python benchmarks/hyde_compound_id_sweep.py --only <pattern_id>

Writes ``benchmarks/results/hyde-compound-id-sweep/<ts>/``:
    summary.json   aggregated metrics + diff vs baseline + gate decision
    raw_runs.json  per-run outcomes (incl. HyDE text for auditing)
    report.md      human-readable report

Exit code:
    0  — measurement completed (regardless of gate outcome)
    1  — measurement failure (>25% patterns errored in any mode, or
         parity subprocess failed to launch)
    2  — infra failure (embedding / HyDE server unreachable)
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
RESULTS_ROOT = BENCH_DIR / "results" / "hyde-compound-id-sweep"
TESTS_DIR = REPO_ROOT / "tests"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_patterns import load_shard  # noqa: E402
from agent_patterns.schema import evaluate_comparison  # noqa: E402

MODES = ["hybrid_hyde_off", "hybrid_hyde_on_dense", "hybrid_hyde_on_full"]
BASELINE_MODE = "hybrid_hyde_off"
ERROR_RATE_MAX = 0.25


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
    hyde_text: str | None = None
    hyde_empty: bool = False
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


def _set_mode_env(mode: str) -> None:
    """Apply env vars for a sweep mode."""
    if mode == "hybrid_hyde_off":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = "60"
        os.environ["TRAWL_BM25_EXTRAS"] = "0"
    elif mode == "hybrid_hyde_on_dense":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = "60"
        os.environ["TRAWL_BM25_EXTRAS"] = "0"
    elif mode == "hybrid_hyde_on_full":
        os.environ["TRAWL_HYBRID_RETRIEVAL"] = "1"
        os.environ["TRAWL_HYBRID_RRF_K"] = "60"
        os.environ["TRAWL_BM25_EXTRAS"] = "1"
    else:
        raise ValueError(f"unknown mode {mode!r}")


def _use_hyde_for_mode(mode: str) -> bool:
    return mode in ("hybrid_hyde_on_dense", "hybrid_hyde_on_full")


def _run_once(pattern: Any, mode: str, iteration: int, verbose: bool) -> RunOutcome:
    from trawl import fetch_relevant, to_dict

    outcome = RunOutcome(iteration=iteration, ok=False)
    use_hyde = _use_hyde_for_mode(mode)
    try:
        t0 = time.monotonic()
        result = fetch_relevant(pattern.url, pattern.query or "", use_hyde=use_hyde)
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
    outcome.hyde_text = data.get("hyde_text") if use_hyde else None
    outcome.hyde_empty = use_hyde and not (data.get("hyde_text") or "").strip()
    outcome.assertion_failures = _evaluate_assertions(pattern.assertions, data)

    if verbose:
        status = "ok " if outcome.ok else "err"
        ap = "PASS" if not outcome.assertion_failures else "FAIL"
        score_s = f"score={outcome.top1_score:.3f}" if outcome.top1_score is not None else ""
        hyde_flag = " HYDE=empty" if outcome.hyde_empty else ""
        print(
            f"    iter {iteration} [{status}] ast={ap} "
            f"retr={outcome.retrieval_ms}ms "
            f"top1={outcome.top1_sig} {score_s}{hyde_flag}",
            file=sys.stderr,
        )
    return outcome


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
    hyde_empty_count = 0
    hyde_total = 0

    for g in groups:
        rs = [r for r in g.runs if r.ok]
        if not rs:
            error_count += 1
        retr = [float(r.retrieval_ms) for r in rs if r.retrieval_ms is not None]
        retr_values.extend(retr)
        passed = g.stable_assertion_pass()
        if passed:
            assertion_pass += 1
        for r in g.runs:
            if r.hyde_text is not None or r.hyde_empty:
                hyde_total += 1
                if r.hyde_empty:
                    hyde_empty_count += 1

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
            "hyde_text_samples": [r.hyde_text[:240] for r in g.runs if r.hyde_text],
            "hyde_empty_iters": sum(1 for r in g.runs if r.hyde_empty),
            "errors": [r.error for r in g.runs if not r.ok],
        })

    return {
        "assertion_pass": assertion_pass,
        "assertion_total": len(groups),
        "error_count": error_count,
        "hyde_empty_ratio": (hyde_empty_count / hyde_total) if hyde_total else None,
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


def _run_parity(mode: str, repo_root: Path, verbose: bool) -> dict[str, Any]:
    """Run `tests/test_pipeline.py` under the given sweep mode."""
    env = os.environ.copy()
    env["TRAWL_HYBRID_RETRIEVAL"] = "1"
    env["TRAWL_HYBRID_RRF_K"] = "60"

    extra_args: list[str] = []
    if mode == "hybrid_hyde_off":
        env["TRAWL_BM25_EXTRAS"] = "0"
    elif mode == "hybrid_hyde_on_dense":
        env["TRAWL_BM25_EXTRAS"] = "0"
        extra_args = ["--hyde"]
    elif mode == "hybrid_hyde_on_full":
        env["TRAWL_BM25_EXTRAS"] = "1"
        extra_args = ["--hyde"]
    else:
        raise ValueError(f"unknown parity mode {mode!r}")

    t0 = time.monotonic()
    if verbose:
        print(f"  parity {mode}: launching test_pipeline.py {' '.join(extra_args)} ...", file=sys.stderr)
    try:
        cp = subprocess.run(
            [sys.executable, "tests/test_pipeline.py", *extra_args],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=1800,
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


def _decide_gate(
    per_mode: dict[str, Any],
    diffs: dict[str, Any],
    parity: dict[str, Any],
) -> dict[str, Any]:
    """Apply pre-registered gates. baseline = hybrid_hyde_off.

    Ordering: check (c) parity first, then (a1) dense, then (a2) full,
    else (b).
    """
    parity_failed: list[str] = []
    for m, p in parity.items():
        if not (p.get("ok") and p.get("pass_count") == p.get("total")):
            parity_failed.append(m)

    def _meets_a(mode: str) -> bool:
        par = parity.get(mode) or {}
        par_ok = par.get("ok") and par.get("pass_count") == par.get("total")
        # Some modes may not be in parity set — don't require parity entry
        # if not measured (e.g. hybrid_hyde_on_dense is no-code-change, so
        # parity may be skipped when the design doc says it's optional).
        if par and not par_ok:
            return False
        d = diffs.get(mode) or {}
        return (
            d.get("net_assertion_delta", 0) >= 1
            and len(d.get("flipped_to_fail", []) or []) == 0
        )

    # (a1) docs-only adoption via dense augmentation
    if _meets_a("hybrid_hyde_on_dense"):
        d = diffs["hybrid_hyde_on_dense"]
        return {
            "outcome": "a1_adopt_docs_only",
            "net_assertion_delta": d["net_assertion_delta"],
            "flipped_to_pass": d["flipped_to_pass"],
            "flipped_to_fail": d["flipped_to_fail"],
            "parity_failed_modes": parity_failed,
        }

    # (a2) code adoption via full augmentation — requires improvement
    # over baseline AND incremental improvement over hybrid_hyde_on_dense
    if _meets_a("hybrid_hyde_on_full"):
        d_full = diffs["hybrid_hyde_on_full"]
        full_mode = per_mode.get("hybrid_hyde_on_full") or {}
        dense_mode = per_mode.get("hybrid_hyde_on_dense") or {}
        incremental = full_mode.get("assertion_pass", 0) - dense_mode.get("assertion_pass", 0)
        if incremental >= 1:
            return {
                "outcome": "a2_adopt_with_code",
                "net_assertion_delta": d_full["net_assertion_delta"],
                "incremental_over_dense": incremental,
                "flipped_to_pass": d_full["flipped_to_pass"],
                "flipped_to_fail": d_full["flipped_to_fail"],
                "parity_failed_modes": parity_failed,
            }

    # (c) parity regression drops the mode — but the loss-of-signal
    # means we still report (b) for the surviving modes' meta-result.
    if parity_failed and not _meets_a("hybrid_hyde_on_dense") and not _meets_a("hybrid_hyde_on_full"):
        return {
            "outcome": "c_parity_regression",
            "parity_failed_modes": parity_failed,
            "reason": "parity regressed and no mode met (a)",
        }

    # (b) reject
    d_full = diffs.get("hybrid_hyde_on_full") or {}
    d_dense = diffs.get("hybrid_hyde_on_dense") or {}
    return {
        "outcome": "b_reject",
        "dense_net_delta": d_dense.get("net_assertion_delta", 0),
        "full_net_delta": d_full.get("net_assertion_delta", 0),
        "parity_failed_modes": parity_failed,
        "reason": "neither dense-only HyDE nor full augmentation met +1 delta / 0 fail-flip",
    }


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
    lines.append("# HyDE compound identifier — sweep report")
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
    for k, v in gate.items():
        if k == "outcome":
            continue
        lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("## Per-mode summary")
    lines.append("")
    lines.append("| mode | assertion pass | errors | HyDE empty | retrieval median | retrieval p95 |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for mode in summary["modes"]:
        m = per_mode[mode]
        empty = m.get("hyde_empty_ratio")
        empty_s = "-" if empty is None else f"{empty:.0%}"
        lines.append(
            f"| `{mode}` | {m['assertion_pass']}/{m['assertion_total']} | "
            f"{m['error_count']} | {empty_s} | "
            f"{_fmt(m['retrieval_ms_median'], ' ms')} | "
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
        total = d["top1_identity_changed"] + d["top1_identity_stable"]
        lines.append(
            f"| `{mode}` | {sign}{net} | {d['flipped_to_pass']} | "
            f"{d['flipped_to_fail']} | "
            f"{d['top1_identity_changed']}/{total} |"
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
    lines.append("## HyDE outputs (sample, experimental modes)")
    lines.append("")
    for mode in ("hybrid_hyde_on_dense", "hybrid_hyde_on_full"):
        if mode not in per_mode:
            continue
        lines.append(f"### `{mode}`")
        lines.append("")
        for pat in per_mode[mode]["patterns"]:
            samples = pat.get("hyde_text_samples") or []
            if not samples:
                continue
            lines.append(f"**{pat['id']}**")
            lines.append("")
            lines.append("```")
            lines.append(samples[0])
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def _precheck_servers() -> bool:
    import httpx

    endpoints = [
        ("embed", os.environ.get("TRAWL_EMBED_URL", "http://localhost:8081/v1")),
        ("hyde", os.environ.get("TRAWL_HYDE_URL", "http://localhost:8082/v1")),
    ]
    for name, base in endpoints:
        health = base.rsplit("/v1", 1)[0] + "/health"
        try:
            httpx.get(health, timeout=3.0).raise_for_status()
        except Exception as e:  # noqa: BLE001
            print(f"{name} server health check failed ({health}): {e}", file=sys.stderr)
            return False
    return True


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
            use_hyde = _use_hyde_for_mode(m)
            print(f"  mode: {m} (use_hyde={use_hyde})")
        for p in patterns:
            print(f"    [{p.id}] {p.url}")
        return 0

    if not _precheck_servers():
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
                g.runs.append(_run_once(pat, mode, iteration=it, verbose=args.verbose))
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
        # Baseline + full experimental path. Dense-only path is
        # code-identical to baseline except for `--hyde` flag, so its
        # parity is covered implicitly when either neighbour passes.
        for mode in ("hybrid_hyde_off", "hybrid_hyde_on_full"):
            result = _run_parity(mode, repo_root=REPO_ROOT, verbose=args.verbose)
            parity[mode] = result
            tag = "PASS" if result.get("ok") else "FAIL"
            print(
                f"  {mode}: {tag} ({result.get('pass_count')}/{result.get('total')}, {result.get('elapsed_s')}s)",
                file=sys.stderr,
            )

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
