"""Parity test for the trawl package.

Runs the 12-case parity matrix and exits non-zero on any regression.
This protects the pipeline against drift in the chunker, retrieval,
extraction, or fetcher code — all tuned empirically and guarded by
ground-truth assertions in tests/test_cases.yaml.

Invoke:
    python tests/test_pipeline.py
    python tests/test_pipeline.py --only kbo_schedule
    python tests/test_pipeline.py --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

from trawl import fetch_relevant, to_dict

TESTS_DIR = Path(__file__).parent
RESULTS_DIR = TESTS_DIR / "results"
TEST_CASES_FILE = TESTS_DIR / "test_cases.yaml"


def load_cases() -> list[dict]:
    with TEST_CASES_FILE.open() as f:
        return yaml.safe_load(f)["cases"]


def score_case(case: dict, result_dict: dict) -> dict:
    gt = case.get("ground_truth") or {}
    chunks = result_dict.get("chunks") or []
    blob = "\n\n".join((c.get("heading") or "") + "\n" + c.get("text", "") for c in chunks)

    failures: list[str] = []

    must_all = gt.get("must_contain_all") or []
    for s in must_all:
        if s not in blob:
            failures.append(f"missing required: {s!r}")

    must_any = gt.get("must_contain_any") or []
    if must_any and not any(s in blob for s in must_any):
        failures.append(f"none of any-group present: {must_any!r}")

    must_any_2 = gt.get("must_contain_any_2") or []
    if must_any_2 and not any(s in blob for s in must_any_2):
        failures.append(f"none of any-group-2 present: {must_any_2!r}")

    pattern = gt.get("must_contain_pattern")
    if pattern and not re.search(pattern, blob):
        failures.append(f"pattern not matched: {pattern!r}")

    min_chunks = gt.get("min_chunks_returned")
    if min_chunks and len(chunks) < min_chunks:
        failures.append(f"only {len(chunks)} chunks (need ≥ {min_chunks})")

    return {
        "pass": len(failures) == 0 and not result_dict.get("error"),
        "failures": failures,
    }


def print_summary(rows: list[dict]) -> None:
    print()
    print(
        f"{'case':<22} {'pass':<5} {'recall':<8} {'tokens~':<8} {'chunks':<7} {'compress':<10} {'latency':<10} {'fetcher':<25}"
    )
    print("-" * 105)
    n_pass = 0
    for r in rows:
        if r["score"]["pass"]:
            n_pass += 1
        out_chars = r["result"]["output_chars"]
        approx_tokens = out_chars // 3
        compress = r["result"]["compression_ratio"]
        latency = r["result"]["total_ms"]
        chunks = len(r["result"]["chunks"])
        fetcher = r["result"]["fetcher_used"]
        marker = "PASS" if r["score"]["pass"] else "FAIL"
        print(
            f"{r['id']:<22} {marker:<5} "
            f"{'OK' if r['score']['pass'] else 'FAIL':<8} "
            f"{approx_tokens:<8} {chunks:<7} {compress:<10}× {latency:>6}ms  {fetcher:<25}"
        )
    print()
    print(f"Total: {n_pass}/{len(rows)} cases pass.")


def print_verbose(case_id: str, result: dict) -> None:
    print(f"\n--- {case_id} returned chunks ---")
    if result.get("hyde_used") and result.get("hyde_text"):
        print(f"HyDE: {result['hyde_text'][:300]}")
        print()
    for i, c in enumerate(result.get("chunks") or []):
        score = c.get("score")
        score_str = f"score={score:.3f}" if score is not None else "(structured)"
        heading = c.get("heading") or "(no heading)"
        print(f"  [{i}] {score_str}  {heading}")
        text = c.get("text") or ""
        print(f"      {text[:200]}{'...' if len(text) > 200 else ''}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run trawl parity tests against the spike test matrix."
    )
    parser.add_argument("--only", help="Only run the case with this id")
    parser.add_argument("--hyde", action="store_true", help="Enable HyDE query expansion")
    parser.add_argument(
        "--k", type=int, default=None, help="Top-k override. Default: adaptive by chunk count."
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print returned chunks for each case"
    )
    parser.add_argument(
        "--no-rerank", action="store_true", help="Disable cross-encoder reranking (use cosine only)"
    )
    args = parser.parse_args()

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"No case with id={args.only}", file=sys.stderr)
            return 2

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir()
    print(f"Writing results to {run_dir}")
    if args.hyde:
        print("HyDE: ENABLED")
    if args.no_rerank:
        print("Reranking: DISABLED")
    else:
        print("Reranking: ENABLED (default)")

    rows = []
    for case in cases:
        print(f"\n=== {case['id']}: {case['url']}")
        print(f"    query: {case['query']}")
        result = fetch_relevant(
            case["url"],
            case["query"],
            k=args.k,
            use_hyde=args.hyde,
            use_rerank=not args.no_rerank,
        )
        result_dict = to_dict(result)

        (run_dir / f"{case['id']}.result.json").write_text(
            json.dumps(result_dict, ensure_ascii=False, indent=2)
        )

        score = score_case(case, result_dict)
        if not score["pass"]:
            for f in score["failures"]:
                print(f"    FAIL {f}")
        elif result_dict.get("error"):
            print(f"    FAIL error: {result_dict['error']}")
        else:
            print(
                f"    PASS {len(result_dict['chunks'])} chunks, "
                f"~{result_dict['output_chars'] // 3} tokens, "
                f"{result_dict['total_ms']}ms"
            )

        if args.verbose:
            print_verbose(case["id"], result_dict)

        rows.append({"id": case["id"], "result": result_dict, "score": score})

    summary = {
        "run_id": run_id,
        "hyde": args.hyde,
        "k": args.k,
        "rows": rows,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print_summary(rows)
    print(f"Full results: {run_dir}")
    # Exit non-zero if any case failed — makes this usable as a regression test.
    return 0 if all(r["score"]["pass"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
