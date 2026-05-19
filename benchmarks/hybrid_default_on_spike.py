"""Hybrid Retrieval Default-On Spike — A/B harness.

Drives the 6-gate decision matrix from
``notes/bge-m3-reader-comparison-gap-outcome.md``:

    1. reader-comparison 6/6 pass (hybrid), flipped_to_fail=0
    2. parity matrix 15/15 in both modes
    3. coding shard 24/24 in both modes
    4. coding shard `code_heavy_query` category — hybrid regression 0
    5. Korean parity 3/3 in both modes (any fail → immediate REJECT)
    6. reader-comparison retrieval_ms p95 — hybrid ≤ baseline × 1.2

Baseline = ``TRAWL_HYBRID_RETRIEVAL=0`` (current default).
Experiment = ``TRAWL_HYBRID_RETRIEVAL=1``.

Both modes use the same llama-server endpoints (bge-m3 :8081,
reranker :8083), so this is a single-process A/B — no swap.

Reader comparison runs once in ``--retrieval-mode dense
--retrieval-mode hybrid`` (12 rows) so both modes share identical
fetch / chunk artefacts; everything else is two separate subprocess
runs.

Usage::

    mamba run -n trawl python benchmarks/hybrid_default_on_spike.py \\
        --out benchmarks/results/hybrid-default-on-spike/<ts>

Writes ``summary.json`` with all 6 gate verdicts and the final
ADOPT/REJECT decision.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

KOREAN_CASE_IDS = {"pricing_page_ko", "korean_wiki_person", "korean_news_ranking"}


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())


def _run(cmd: list[str], *, cwd: Path = REPO_ROOT, env: dict | None = None) -> int:
    """Stream a subprocess to stdout/stderr and return its exit code."""
    print(f"\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=cwd, env=env)
    return proc.returncode


def _latest(dir_: Path, prefix: str = "") -> Path | None:
    if not dir_.exists():
        return None
    candidates = [p for p in dir_.iterdir() if p.is_dir() and p.name.startswith(prefix)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _parse_parity(results_dir: Path) -> dict:
    summary = results_dir / "summary.json"
    if not summary.exists():
        return {"error": f"missing {summary}"}
    data = json.loads(summary.read_text())
    rows = data.get("rows", [])
    by_id = {r["id"]: bool(r.get("score", {}).get("pass")) for r in rows}
    return {
        "total": len(rows),
        "passed": sum(1 for v in by_id.values() if v),
        "failed_ids": sorted(rid for rid, ok in by_id.items() if not ok),
        "korean_passed": {rid: by_id.get(rid) for rid in sorted(KOREAN_CASE_IDS)},
        "run_dir": str(results_dir),
    }


def _parse_coding(results_dir: Path) -> dict:
    jsonl = results_dir / "patterns.jsonl"
    if not jsonl.exists():
        return {"error": f"missing {jsonl}"}
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    by_id = {r["id"]: bool(r.get("passed")) for r in rows}
    by_id_with_category = {
        r["id"]: {"passed": bool(r.get("passed")), "category": r.get("category")} for r in rows
    }
    code_heavy = {
        rid: entry["passed"]
        for rid, entry in by_id_with_category.items()
        if entry["category"] == "code_heavy_query"
    }
    return {
        "total": len(rows),
        "passed": sum(1 for v in by_id.values() if v),
        "failed_ids": sorted(rid for rid, ok in by_id.items() if not ok),
        "code_heavy_query": {
            "total": len(code_heavy),
            "passed": sum(1 for v in code_heavy.values() if v),
            "passed_ids": sorted(rid for rid, ok in code_heavy.items() if ok),
            "failed_ids": sorted(rid for rid, ok in code_heavy.items() if not ok),
        },
        "run_dir": str(results_dir),
    }


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    sv = sorted(values)
    k = (len(sv) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sv) - 1)
    if f == c:
        return float(sv[f])
    return float(sv[f] + (sv[c] - sv[f]) * (k - f))


def _parse_reader_comparison_modes(out_dir: Path) -> dict:
    """Bucket reader-comparison rows by retrieval_mode_requested."""
    jsonl = out_dir / "results.jsonl"
    if not jsonl.exists():
        return {"error": f"missing {jsonl}"}
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    trawl_rows = [r for r in rows if r.get("provider") == "trawl"]

    def bucket(mode: str) -> dict:
        mode_rows = [r for r in trawl_rows if r.get("retrieval_mode_requested") == mode]
        passing = sum(1 for r in mode_rows if r.get("answer_grounding_hit"))
        retrieval_ms = [
            r["retrieval_ms"] for r in mode_rows if isinstance(r.get("retrieval_ms"), (int, float))
        ]
        return {
            "total": len(mode_rows),
            "passed": passing,
            "passed_ids": sorted(r["case_id"] for r in mode_rows if r.get("answer_grounding_hit")),
            "failed_ids": sorted(
                r["case_id"] for r in mode_rows if not r.get("answer_grounding_hit")
            ),
            "flipped_to_fail": sum(1 for r in mode_rows if r.get("flipped_to_fail")),
            "retrieval_ms_p50": _percentile(retrieval_ms, 50),
            "retrieval_ms_p95": _percentile(retrieval_ms, 95),
            "tokens_avg": (
                statistics.mean(r["tokens_returned"] for r in mode_rows if r.get("tokens_returned"))
                if mode_rows
                else None
            ),
        }

    return {
        "dense": bucket("dense"),
        "hybrid": bucket("hybrid"),
        "run_dir": str(out_dir),
    }


def run_test_subprocess(*, label: str, hybrid_on: bool, out_dir: Path) -> dict:
    """Run parity + coding shard once with a specific hybrid env value."""
    env = dict(os.environ)
    env["TRAWL_HYBRID_RETRIEVAL"] = "1" if hybrid_on else "0"
    # Embed cache disabled for clean measurement; fetch cache stays on.
    env["TRAWL_EMBED_CACHE_TTL"] = "0"
    env["TRAWL_EMBED_CACHE_PATH"] = str(out_dir / f"embed-cache-{label}")

    print(f"\n=== {label} (hybrid={'on' if hybrid_on else 'off'}) ===", flush=True)
    started = time.time()

    rc_parity = _run(["python", "tests/test_pipeline.py"], env=env)
    parity_dir = _latest(REPO_ROOT / "tests" / "results", prefix="2")
    parity = _parse_parity(parity_dir) if parity_dir else {"error": "no results dir"}
    parity["exit_code"] = rc_parity

    rc_coding = _run(
        ["python", "tests/test_agent_patterns.py", "--shard", "coding"],
        env=env,
    )
    coding_dir = _latest(REPO_ROOT / "tests" / "results", prefix="agent_patterns_")
    coding = _parse_coding(coding_dir) if coding_dir else {"error": "no results dir"}
    coding["exit_code"] = rc_coding

    return {
        "label": label,
        "hybrid_on": hybrid_on,
        "duration_seconds": round(time.time() - started, 1),
        "parity": parity,
        "coding_shard": coding,
    }


def run_reader_comparison_both_modes(out_dir: Path) -> dict:
    """Single reader-comparison run, expanding dense + hybrid via flags."""
    env = dict(os.environ)
    env["TRAWL_EMBED_CACHE_TTL"] = "0"
    env["TRAWL_EMBED_CACHE_PATH"] = str(out_dir / "embed-cache-reader")
    rc_dir = out_dir / "reader-comparison"
    rc = _run(
        [
            "python",
            "benchmarks/reader_comparison.py",
            "--provider",
            "trawl",
            "--retrieval-mode",
            "dense",
            "--retrieval-mode",
            "hybrid",
            "--output-dir",
            str(rc_dir),
        ],
        env=env,
    )
    reader = _parse_reader_comparison_modes(rc_dir)
    reader["exit_code"] = rc
    return reader


def _flag(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def evaluate(summary: dict) -> dict:
    """Apply the 6 gates and return per-gate verdicts + final decision."""
    bl = summary["baseline"]
    ex = summary["experiment"]
    reader = summary["reader_comparison"]

    # Gate 1 — reader-comparison hybrid passes ≥ 6 AND flipped_to_fail=0.
    hybrid = reader.get("hybrid", {})
    dense = reader.get("dense", {})
    g1 = (
        hybrid.get("passed", 0) == hybrid.get("total", 0)
        and hybrid.get("total", 0) >= 6
        and hybrid.get("flipped_to_fail", 0) == 0
    )

    # Gate 2 — parity 15/15 in both modes.
    g2 = bl["parity"]["passed"] == 15 and ex["parity"]["passed"] == 15

    # Gate 3 — coding 24/24 in both modes (regression 0).
    g3 = (
        ex["coding_shard"]["passed"] >= bl["coding_shard"]["passed"]
        and ex["coding_shard"]["passed"] == ex["coding_shard"]["total"]
    )

    # Gate 4 — code_heavy_query category regression 0.
    bl_ch = bl["coding_shard"]["code_heavy_query"]
    ex_ch = ex["coding_shard"]["code_heavy_query"]
    bl_ch_pass_ids = set(bl_ch.get("passed_ids", []))
    ex_ch_pass_ids = set(ex_ch.get("passed_ids", []))
    g4 = bl_ch_pass_ids.issubset(ex_ch_pass_ids)

    # Gate 5 — Korean 3/3 in both modes.
    bl_kor = bl["parity"]["korean_passed"]
    ex_kor = ex["parity"]["korean_passed"]
    g5 = all(bl_kor.get(k) for k in KOREAN_CASE_IDS) and all(ex_kor.get(k) for k in KOREAN_CASE_IDS)

    # Gate 6 — retrieval p95 ≤ baseline × 1.2.
    bl_p95 = dense.get("retrieval_ms_p95")
    ex_p95 = hybrid.get("retrieval_ms_p95")
    g6 = bl_p95 is not None and ex_p95 is not None and ex_p95 <= bl_p95 * 1.2
    p95_ratio = (ex_p95 / bl_p95) if (bl_p95 and ex_p95) else None

    verdicts = {
        "gate_1_reader_hybrid_pass": {
            "pass": g1,
            "detail": {
                "hybrid_passed": hybrid.get("passed"),
                "hybrid_total": hybrid.get("total"),
                "hybrid_flipped_to_fail": hybrid.get("flipped_to_fail"),
                "hybrid_failed_ids": hybrid.get("failed_ids"),
                "dense_passed": dense.get("passed"),
                "dense_total": dense.get("total"),
                "dense_failed_ids": dense.get("failed_ids"),
            },
        },
        "gate_2_parity_15_15_both": {
            "pass": g2,
            "detail": {
                "baseline": f"{bl['parity']['passed']}/{bl['parity']['total']}",
                "experiment": f"{ex['parity']['passed']}/{ex['parity']['total']}",
                "baseline_failed": bl["parity"]["failed_ids"],
                "experiment_failed": ex["parity"]["failed_ids"],
            },
        },
        "gate_3_coding_24_24_both": {
            "pass": g3,
            "detail": {
                "baseline": f"{bl['coding_shard']['passed']}/{bl['coding_shard']['total']}",
                "experiment": f"{ex['coding_shard']['passed']}/{ex['coding_shard']['total']}",
                "baseline_failed": bl["coding_shard"]["failed_ids"],
                "experiment_failed": ex["coding_shard"]["failed_ids"],
            },
        },
        "gate_4_code_heavy_regression_zero": {
            "pass": g4,
            "detail": {
                "baseline_passed_set": sorted(bl_ch_pass_ids),
                "experiment_passed_set": sorted(ex_ch_pass_ids),
                "regressed_ids": sorted(bl_ch_pass_ids - ex_ch_pass_ids),
            },
        },
        "gate_5_korean_3_3_both": {
            "pass": g5,
            "detail": {"baseline": bl_kor, "experiment": ex_kor},
        },
        "gate_6_retrieval_p95_within_120pct": {
            "pass": g6,
            "detail": {
                "baseline_p95_ms": bl_p95,
                "experiment_p95_ms": ex_p95,
                "ratio": p95_ratio,
                "limit_x": 1.2,
            },
        },
    }

    all_pass = all(v["pass"] for v in verdicts.values())
    decision = "ADOPT" if all_pass else "REJECT"

    return {"verdicts": verdicts, "decision": decision}


def print_table(summary: dict) -> None:
    print()
    print("=" * 80)
    print("HYBRID-DEFAULT-ON SPIKE — Gate Verdicts")
    print("=" * 80)
    for gate, payload in summary["evaluation"]["verdicts"].items():
        print(f"  [{_flag(payload['pass'])}] {gate}")
        for k, v in payload["detail"].items():
            print(f"        {k:32s} = {v}")
    print()
    print(f"  Decision: {summary['evaluation']['decision']}")
    print("=" * 80)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        help="Output directory for this run.",
        default=str(REPO_ROOT / "benchmarks/results/hybrid-default-on-spike" / _ts()),
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()

    # 1. reader-comparison runs both modes in one call.
    reader = run_reader_comparison_both_modes(out_dir)

    # 2. Baseline parity + coding (TRAWL_HYBRID_RETRIEVAL=0).
    baseline = run_test_subprocess(label="baseline", hybrid_on=False, out_dir=out_dir)

    # 3. Experiment parity + coding (TRAWL_HYBRID_RETRIEVAL=1).
    experiment = run_test_subprocess(label="experiment", hybrid_on=True, out_dir=out_dir)

    summary = {
        "label": "hybrid-default-on-spike",
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_seconds": round(time.time() - started, 1),
        "out_dir": str(out_dir),
        "baseline": baseline,
        "experiment": experiment,
        "reader_comparison": reader,
    }
    summary["evaluation"] = evaluate(summary)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print_table(summary)
    print(f"\nSummary: {out_dir / 'summary.json'}")
    return 0 if summary["evaluation"]["decision"] == "ADOPT" else 1


if __name__ == "__main__":
    sys.exit(main())
