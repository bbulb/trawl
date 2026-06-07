"""Spike A helper — Qwen3-Embedding-0.6B-GGUF drop-in A/B harness.

Runs the three existing harnesses (parity matrix, agent_patterns
coding shard, reader_comparison.py) against whatever embedding
endpoint trawl is currently configured for (via TRAWL_EMBED_URL /
TRAWL_EMBED_MODEL) and writes a single summary.json so two runs
(baseline + experiment) can be compared in one place.

Design doc: docs/superpowers/specs/2026-05-18-qwen3-embedding-swap-design.md

Usage:

    # Baseline run against BGE-M3
    TRAWL_EMBED_MODEL=bge-m3 \
      mamba run -n trawl python benchmarks/qwen3_embedding_swap.py \
        --label baseline --out benchmarks/results/qwen3-embedding-swap/<ts>-baseline

    # Swap llama-server to Qwen3-Embedding (see design doc), then:
    TRAWL_EMBED_MODEL=qwen3-embedding \
      mamba run -n trawl python benchmarks/qwen3_embedding_swap.py \
        --label experiment --out benchmarks/results/qwen3-embedding-swap/<ts>-experiment

    # Compare the two runs side-by-side
    mamba run -n trawl python benchmarks/qwen3_embedding_swap.py --compare \
        benchmarks/results/qwen3-embedding-swap/<ts>-baseline \
        benchmarks/results/qwen3-embedding-swap/<ts>-experiment

The harness sets TRAWL_EMBED_CACHE_TTL=0 internally so cold metrics
are not contaminated by warm-cache hits. Existing fetch cache stays
enabled — only embedding cache is bypassed.
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
    """Return the most recently modified subdirectory of `dir_`."""
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
    return {
        "total": len(rows),
        "passed": sum(1 for v in by_id.values() if v),
        "failed_ids": sorted(rid for rid, ok in by_id.items() if not ok),
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


def _parse_reader_comparison(out_dir: Path) -> dict:
    jsonl = out_dir / "results.jsonl"
    if not jsonl.exists():
        return {"error": f"missing {jsonl}"}
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    trawl_rows = [r for r in rows if r.get("provider") == "trawl"]
    passing = sum(1 for r in trawl_rows if r.get("answer_grounding_hit"))
    retrieval_ms = [
        r["retrieval_ms"] for r in trawl_rows if isinstance(r.get("retrieval_ms"), (int, float))
    ]
    return {
        "total": len(trawl_rows),
        "passed": passing,
        "rank1_identity_by_case": {
            r["case_id"]: r.get("rank1_identity") for r in trawl_rows if r.get("case_id")
        },
        "retrieval_ms_p50": _percentile(retrieval_ms, 50),
        "retrieval_ms_p95": _percentile(retrieval_ms, 95),
        "tokens_avg": (
            statistics.mean(r["tokens_returned"] for r in trawl_rows if r.get("tokens_returned"))
            if trawl_rows
            else None
        ),
        "run_dir": str(out_dir),
    }


def run_once(out_dir: Path, label: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # Cache-disabled measurement: embed cache off, fetch cache stays.
    env["TRAWL_EMBED_CACHE_TTL"] = "0"
    env["TRAWL_EMBED_CACHE_PATH"] = str(out_dir / "embed-cache-isolated")

    print(f"\n=== {label} run @ {out_dir} ===", flush=True)
    print(f"TRAWL_EMBED_URL  = {env.get('TRAWL_EMBED_URL', '(default :8081)')}")
    print(f"TRAWL_EMBED_MODEL= {env.get('TRAWL_EMBED_MODEL', '(default bge-m3)')}")
    print(f"TRAWL_EMBED_CACHE_TTL= {env['TRAWL_EMBED_CACHE_TTL']}")

    started = time.time()

    # 1. Parity matrix.
    rc_parity = _run(["python", "tests/test_pipeline.py"], env=env)
    parity_dir = _latest(REPO_ROOT / "tests" / "results", prefix="2")
    parity = _parse_parity(parity_dir) if parity_dir else {"error": "no results dir"}
    parity["exit_code"] = rc_parity

    # 2. Agent patterns — coding shard.
    rc_coding = _run(
        ["python", "tests/test_agent_patterns.py", "--shard", "coding"],
        env=env,
    )
    coding_dir = _latest(REPO_ROOT / "tests" / "results", prefix="agent_patterns_")
    coding = _parse_coding(coding_dir) if coding_dir else {"error": "no results dir"}
    coding["exit_code"] = rc_coding

    # 3. Reader comparison — trawl provider only, cold pass (no --repeat).
    rc_dir = out_dir / "reader-comparison"
    rc_reader = _run(
        [
            "python",
            "benchmarks/reader_comparison.py",
            "--provider",
            "trawl",
            "--output-dir",
            str(rc_dir),
        ],
        env=env,
    )
    reader = _parse_reader_comparison(rc_dir)
    reader["exit_code"] = rc_reader

    summary = {
        "label": label,
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_seconds": round(time.time() - started, 1),
        "env": {
            "TRAWL_EMBED_URL": env.get("TRAWL_EMBED_URL", "(default http://localhost:8081/v1)"),
            "TRAWL_EMBED_MODEL": env.get("TRAWL_EMBED_MODEL", "(default bge-m3)"),
            "TRAWL_EMBED_CACHE_TTL": env["TRAWL_EMBED_CACHE_TTL"],
        },
        "parity": parity,
        "coding_shard": coding,
        "reader_comparison": reader,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\n=== {label} summary written to {out_dir / 'summary.json'} ===")
    return summary


def _flag(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def compare(baseline_dir: Path, experiment_dir: Path) -> int:
    bl = json.loads((baseline_dir / "summary.json").read_text())
    ex = json.loads((experiment_dir / "summary.json").read_text())

    print(f"\n=== Comparison ===")
    print(f"baseline   : {baseline_dir} ({bl['env']['TRAWL_EMBED_MODEL']})")
    print(f"experiment : {experiment_dir} ({ex['env']['TRAWL_EMBED_MODEL']})")
    print()

    def row(label, b, e):
        print(f"  {label:32s} baseline={b!s:20s} experiment={e!s}")

    # Gate 1: parity
    g1 = ex["parity"]["passed"] >= bl["parity"]["passed"] and ex["parity"]["total"] == 15
    row("parity passed", f"{bl['parity']['passed']}/{bl['parity']['total']}",
        f"{ex['parity']['passed']}/{ex['parity']['total']}")

    # Gate 4: Korean (subset of parity)
    bl_kor = bl["parity"]["korean_passed"]
    ex_kor = ex["parity"]["korean_passed"]
    g4 = all(ex_kor.get(k) for k in KOREAN_CASE_IDS) and (
        sum(bool(v) for v in bl_kor.values()) == sum(bool(v) for v in ex_kor.values())
    )
    row("korean (3)", json.dumps(bl_kor, ensure_ascii=False),
        json.dumps(ex_kor, ensure_ascii=False))

    # Gate 2: coding shard
    g2 = ex["coding_shard"]["passed"] >= bl["coding_shard"]["passed"]
    row("coding shard passed",
        f"{bl['coding_shard']['passed']}/{bl['coding_shard']['total']}",
        f"{ex['coding_shard']['passed']}/{ex['coding_shard']['total']}")

    # Gate 3: reader-comparison net assertion delta
    rc_delta = ex["reader_comparison"]["passed"] - bl["reader_comparison"]["passed"]
    g3 = rc_delta >= 1 and (
        ex["reader_comparison"]["total"] >= bl["reader_comparison"]["total"]
    )
    row("reader-comp passed",
        f"{bl['reader_comparison']['passed']}/{bl['reader_comparison']['total']}",
        f"{ex['reader_comparison']['passed']}/{ex['reader_comparison']['total']}  (Δ={rc_delta:+d})")

    # Gate 5: retrieval p95 within +20%
    bl_p95 = bl["reader_comparison"].get("retrieval_ms_p95")
    ex_p95 = ex["reader_comparison"].get("retrieval_ms_p95")
    g5 = (bl_p95 is not None and ex_p95 is not None and ex_p95 <= bl_p95 * 1.2)
    pct = "n/a"
    if bl_p95 and ex_p95:
        pct = f"{(ex_p95 / bl_p95 - 1) * 100:+.1f}%"
    row("retrieval p95 (ms)", f"{bl_p95}", f"{ex_p95}  ({pct})")

    print()
    print(f"  Gate 1 parity  : {_flag(g1)}")
    print(f"  Gate 2 coding  : {_flag(g2)}")
    print(f"  Gate 3 reader  : {_flag(g3)} (need delta ≥ +1)")
    print(f"  Gate 4 korean  : {_flag(g4)}")
    print(f"  Gate 5 p95     : {_flag(g5)} (need ≤ baseline × 1.2)")
    print()

    all_pass = all([g1, g2, g3, g4, g5])
    print(f"  Decision       : {'ADOPT' if all_pass else 'REJECT'}")
    return 0 if all_pass else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", help="Run label (baseline | experiment).")
    parser.add_argument("--out", help="Output directory for this run.")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE", "EXPERIMENT"),
        help="Compare two pre-existing run directories and print the gate table.",
    )
    args = parser.parse_args(argv)

    if args.compare:
        return compare(Path(args.compare[0]), Path(args.compare[1]))

    if not (args.label and args.out):
        parser.print_help()
        print("\nerror: --label and --out are required when not using --compare", file=sys.stderr)
        return 2

    run_once(Path(args.out), args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
