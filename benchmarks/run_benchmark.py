"""trawl vs Jina Reader (r.jina.ai) benchmark.

Runs the same URLs + queries through both systems and compares:
  - Latency (seconds)
  - Output size (chars and estimated tokens)
  - Ground truth hit (does the output contain the expected facts?)
  - Compression ratio (output / input)

trawl is tested in three modes:
  - base:    no profile (embedding retrieval only)
  - profile: generate a VLM profile first, then fetch with profile
  - cached:  fetch again with the already-cached profile (measures
             the fast path without VLM overhead)

Invoke:
    python benchmarks/run_benchmark.py
    python benchmarks/run_benchmark.py --only wiki_llm
    python benchmarks/run_benchmark.py --verbose
    python benchmarks/run_benchmark.py --no-profile
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from trawl import fetch_relevant, to_dict
from trawl.profiles import generate_profile, load_profile, profile_dir

BENCH_DIR = Path(__file__).parent
CASES_FILE = BENCH_DIR / "benchmark_cases.yaml"
RESULTS_DIR = BENCH_DIR / "results"

JINA_BASE = "https://r.jina.ai"
JINA_TIMEOUT = 60.0


def load_cases() -> list[dict]:
    with CASES_FILE.open() as f:
        return yaml.safe_load(f)["cases"]


def estimate_tokens(text: str) -> int:
    """Rough token count: English ~4 chars/token, CJK ~1.5 chars/token."""
    if not text:
        return 0
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff'
              or '\uac00' <= c <= '\ud7af'
              or '\u3040' <= c <= '\u30ff')
    non_cjk = len(text) - cjk
    return int(non_cjk / 4.0 + cjk / 1.5)


def score_ground_truth(text: str, ground_truth: dict) -> tuple[bool, list[str]]:
    """Check ground truth rules against text. Returns (passed, failures)."""
    failures: list[str] = []

    must_all = ground_truth.get("must_contain_all") or []
    for s in must_all:
        if s not in text:
            failures.append(f"missing required: {s!r}")

    must_any = ground_truth.get("must_contain_any") or []
    if must_any and not any(s in text for s in must_any):
        failures.append(f"none of any-group: {must_any!r}")

    must_any_2 = ground_truth.get("must_contain_any_2") or []
    if must_any_2 and not any(s in text for s in must_any_2):
        failures.append(f"none of any-group-2: {must_any_2!r}")

    pattern = ground_truth.get("must_contain_pattern")
    if pattern and not re.search(pattern, text):
        failures.append(f"pattern not found: {pattern!r}")

    return (len(failures) == 0, failures)


def _trawl_text_from_result(result) -> tuple[str, list[dict]]:
    """Extract concatenated text and chunk list from a PipelineResult."""
    rd = to_dict(result)
    chunks = rd.get("chunks") or []
    text = "\n\n".join(
        (c.get("heading") or "") + "\n" + c.get("text", "")
        for c in chunks
    )
    return text, chunks


def _trawl_metrics(result, elapsed: float) -> dict:
    rd = to_dict(result)
    text, chunks = _trawl_text_from_result(result)
    return {
        "ok": True,
        "latency_s": round(elapsed, 2),
        "chars": len(text),
        "tokens_est": estimate_tokens(text),
        "n_chunks": len(chunks),
        "text": text,
        "input_chars": rd.get("input_chars", 0),
        "compression": rd.get("compression_ratio", 0),
    }


def _trawl_error(elapsed: float, error: str) -> dict:
    return {
        "ok": False,
        "latency_s": round(elapsed, 2),
        "chars": 0,
        "tokens_est": 0,
        "n_chunks": 0,
        "text": "",
        "input_chars": 0,
        "compression": 0,
        "error": error,
    }


def run_trawl_base(url: str, query: str) -> dict:
    """Run trawl without any profile (pure embedding retrieval)."""
    t0 = time.monotonic()
    try:
        result = fetch_relevant(url, query, use_rerank=True)
        return _trawl_metrics(result, time.monotonic() - t0)
    except Exception as e:
        return _trawl_error(time.monotonic() - t0, str(e))


def run_trawl_profile_generate(url: str) -> dict:
    """Generate a VLM profile for the URL. Returns profile metadata."""
    t0 = time.monotonic()
    try:
        result = generate_profile(url, force_refresh=True)
        elapsed = time.monotonic() - t0
        return {
            "ok": result.get("ok", False),
            "latency_s": round(elapsed, 2),
            "selector": result.get("main_selector", ""),
            "error": result.get("error", ""),
        }
    except Exception as e:
        return {
            "ok": False,
            "latency_s": round(time.monotonic() - t0, 2),
            "selector": "",
            "error": str(e),
        }


def run_trawl_with_profile(url: str, query: str) -> dict:
    """Run trawl with a cached profile (profile fast path)."""
    t0 = time.monotonic()
    try:
        result = fetch_relevant(url, query, use_rerank=True)
        return _trawl_metrics(result, time.monotonic() - t0)
    except Exception as e:
        return _trawl_error(time.monotonic() - t0, str(e))


def run_jina(url: str) -> dict:
    """Fetch via r.jina.ai, return metrics dict."""
    t0 = time.monotonic()
    try:
        headers = {"Accept": "text/markdown"}
        api_key = os.environ.get("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=JINA_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(
                f"{JINA_BASE}/{url}",
                headers=headers,
            )
            resp.raise_for_status()
        elapsed = time.monotonic() - t0
        text = resp.text
        return {
            "ok": True,
            "latency_s": round(elapsed, 2),
            "chars": len(text),
            "tokens_est": estimate_tokens(text),
            "text": text,
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {
            "ok": False,
            "latency_s": round(elapsed, 2),
            "chars": 0,
            "tokens_est": 0,
            "text": "",
            "error": str(e),
        }


def _clear_profile(url: str):
    """Remove any cached profile for this URL."""
    from trawl.profiles import profile_path_for
    p = profile_path_for(url)
    if p.exists():
        p.unlink()


def _print_run(label: str, metrics: dict, gt: dict):
    """Print one run line."""
    gt_pass, gt_failures = score_ground_truth(metrics.get("text", ""), gt)
    metrics["gt_pass"] = gt_pass
    metrics["gt_failures"] = gt_failures
    status = "PASS" if gt_pass else "FAIL"
    tok = metrics.get("tokens_est", 0)
    print(f"  [{label:<14}] {metrics['latency_s']:>6.1f}s  {tok:>8,} tok  GT: {status}")
    return gt_pass, gt_failures


def run_one(case: dict, *, verbose: bool = False, with_profile: bool = True) -> dict:
    """Run a single benchmark case, return combined result."""
    cid = case["id"]
    url = case["url"]
    query = case["query"]
    gt = case.get("ground_truth") or {}

    print(f"\n{'='*70}")
    print(f"  {cid}: {case.get('description', '')}")
    print(f"  URL: {url}")
    print(f"  Query: {query}")
    print(f"{'='*70}")

    # 1) trawl base (no profile)
    _clear_profile(url)
    print("  Running trawl (base)...", flush=True)
    trawl_base = run_trawl_base(url, query)
    _print_run("trawl-base", trawl_base, gt)

    # 2) trawl with profile
    trawl_profile_gen = None
    trawl_cached = None
    if with_profile:
        # Clear profile again to ensure clean state
        _clear_profile(url)

        # 2a) Generate profile
        print("  Generating profile...", flush=True)
        trawl_profile_gen = run_trawl_profile_generate(url)
        gen_status = "ok" if trawl_profile_gen["ok"] else "FAIL"
        selector = trawl_profile_gen.get("selector", "")[:50]
        print(f"  [profile-gen   ] {trawl_profile_gen['latency_s']:>6.1f}s  "
              f"status: {gen_status}  selector: {selector}")

        # 2b) Fetch with cached profile
        if trawl_profile_gen["ok"]:
            print("  Running trawl (cached profile)...", flush=True)
            trawl_cached = run_trawl_with_profile(url, query)
            _print_run("trawl-cached", trawl_cached, gt)

    # 3) Jina
    print("  Running jina...", flush=True)
    jina = run_jina(url)
    _print_run("jina", jina, gt)

    # Token ratios
    base_tok = trawl_base.get("tokens_est", 0)
    cached_tok = trawl_cached.get("tokens_est", 0) if trawl_cached else 0
    jina_tok = jina.get("tokens_est", 0)
    if base_tok > 0 and jina_tok > 0:
        print(f"  Ratio jina/trawl-base: {jina_tok/base_tok:.1f}x")
    if cached_tok > 0 and jina_tok > 0:
        print(f"  Ratio jina/trawl-cached: {jina_tok/cached_tok:.1f}x")

    if verbose:
        for label, m in [("trawl-base", trawl_base),
                         ("trawl-cached", trawl_cached),
                         ("jina", jina)]:
            if m is None:
                continue
            if m.get("gt_failures"):
                print(f"  [{label}] GT failures: {m['gt_failures']}")
            if m.get("ok") and m.get("text"):
                preview = m["text"][:300].replace("\n", " ")
                print(f"  [{label}] preview: {preview}...")

    # Clean up profile after test
    _clear_profile(url)

    return {
        "id": cid,
        "category": case.get("category", ""),
        "trawl_base": trawl_base,
        "trawl_profile_gen": trawl_profile_gen,
        "trawl_cached": trawl_cached,
        "jina": jina,
    }


def print_summary(results: list[dict], with_profile: bool = True):
    """Print a summary table."""
    print("\n")
    w = 130 if with_profile else 100
    print("=" * w)
    print("BENCHMARK SUMMARY: trawl vs Jina Reader")
    print("=" * w)

    if with_profile:
        header = (
            f"{'ID':<20} {'Cat':<8} "
            f"{'base s':>7} {'prof s':>7} {'cache s':>8} {'jina s':>7} "
            f"{'base tok':>9} {'cache tok':>10} {'jina tok':>9} "
            f"{'j/base':>7} {'j/cache':>8} "
            f"{'bGT':>4} {'cGT':>4} {'jGT':>4}"
        )
    else:
        header = (
            f"{'ID':<20} {'Cat':<8} "
            f"{'trawl s':>8} {'jina s':>8} "
            f"{'trawl tok':>10} {'jina tok':>10} {'ratio':>7} "
            f"{'trawl GT':>9} {'jina GT':>8}"
        )
    print(header)
    print("-" * w)

    # Accumulators
    stats = {
        "base_gt": 0, "cached_gt": 0, "jina_gt": 0,
        "base_tok": 0, "cached_tok": 0, "jina_tok": 0,
        "base_lat": 0.0, "prof_lat": 0.0, "cached_lat": 0.0, "jina_lat": 0.0,
        "base_wins": 0, "cached_wins": 0, "jina_wins": 0,
    }
    n = len(results)
    n_profiled = 0

    for r in results:
        b = r["trawl_base"]
        pg = r.get("trawl_profile_gen")
        c = r.get("trawl_cached")
        j = r["jina"]

        b_tok = b.get("tokens_est", 0)
        c_tok = c.get("tokens_est", 0) if c else 0
        j_tok = j.get("tokens_est", 0)

        jb_ratio = f"{j_tok/b_tok:.1f}x" if b_tok > 0 and j_tok > 0 else "n/a"
        jc_ratio = f"{j_tok/c_tok:.1f}x" if c_tok > 0 and j_tok > 0 else "n/a"

        b_gt = "P" if b.get("gt_pass") else "F"
        c_gt = "P" if (c and c.get("gt_pass")) else ("-" if not c else "F")
        j_gt = "P" if j.get("gt_pass") else "F"

        if with_profile:
            prof_s = f"{pg['latency_s']:>6.1f}s" if pg else "    n/a"
            cache_s = f"{c['latency_s']:>7.1f}s" if c else "     n/a"
            print(
                f"{r['id']:<20} {r['category']:<8} "
                f"{b['latency_s']:>6.1f}s {prof_s} {cache_s} {j['latency_s']:>6.1f}s "
                f"{b_tok:>9,} {c_tok:>10,} {j_tok:>9,} "
                f"{jb_ratio:>7} {jc_ratio:>8} "
                f"{b_gt:>4} {c_gt:>4} {j_gt:>4}"
            )
        else:
            print(
                f"{r['id']:<20} {r['category']:<8} "
                f"{b['latency_s']:>7.1f}s {j['latency_s']:>7.1f}s "
                f"{b_tok:>10,} {j_tok:>10,} {jb_ratio:>7} "
                f"{'PASS' if b.get('gt_pass') else 'FAIL':>9} "
                f"{'PASS' if j.get('gt_pass') else 'FAIL':>8}"
            )

        if b.get("gt_pass"):
            stats["base_gt"] += 1
        if c and c.get("gt_pass"):
            stats["cached_gt"] += 1
        if j.get("gt_pass"):
            stats["jina_gt"] += 1

        stats["base_tok"] += b_tok
        stats["cached_tok"] += c_tok
        stats["jina_tok"] += j_tok
        stats["base_lat"] += b["latency_s"]
        stats["jina_lat"] += j["latency_s"]
        if pg:
            stats["prof_lat"] += pg["latency_s"]
        if c:
            stats["cached_lat"] += c["latency_s"]
            n_profiled += 1

        # Token wins (base vs jina)
        if b_tok > 0 and j_tok > 0:
            if b_tok < j_tok:
                stats["base_wins"] += 1
            elif j_tok < b_tok:
                stats["jina_wins"] += 1
        # Token wins (cached vs jina)
        if c_tok > 0 and j_tok > 0:
            if c_tok < j_tok:
                stats["cached_wins"] += 1

    print("-" * w)

    # Averages
    ab = stats["base_lat"] / n if n else 0
    aj = stats["jina_lat"] / n if n else 0
    ap = stats["prof_lat"] / n if n else 0
    ac = stats["cached_lat"] / n_profiled if n_profiled else 0
    tb = stats["base_tok"] / n if n else 0
    tc = stats["cached_tok"] / n_profiled if n_profiled else 0
    tj = stats["jina_tok"] / n if n else 0
    rb = stats["jina_tok"] / stats["base_tok"] if stats["base_tok"] else 0
    rc = stats["jina_tok"] / stats["cached_tok"] if stats["cached_tok"] else 0

    print()
    print("Averages:")
    print(f"  Latency:  trawl-base {ab:.1f}s | profile-gen {ap:.1f}s | "
          f"trawl-cached {ac:.1f}s | jina {aj:.1f}s")
    print(f"  Tokens:   trawl-base {tb:,.0f} | trawl-cached {tc:,.0f} | jina {tj:,.0f}")
    print()
    print("Totals:")
    print(f"  Tokens:   trawl-base {stats['base_tok']:,} | "
          f"trawl-cached {stats['cached_tok']:,} | jina {stats['jina_tok']:,}")
    print(f"  Ratio:    jina/trawl-base = {rb:.1f}x | jina/trawl-cached = {rc:.1f}x")
    print()
    print("Ground truth:")
    print(f"  trawl-base {stats['base_gt']}/{n} | "
          f"trawl-cached {stats['cached_gt']}/{n_profiled} | "
          f"jina {stats['jina_gt']}/{n}")
    print()
    print(f"Token efficiency (smaller wins):")
    print(f"  trawl-base vs jina: trawl {stats['base_wins']}/{n}, "
          f"jina {stats['jina_wins']}/{n}")
    if n_profiled:
        print(f"  trawl-cached vs jina: trawl {stats['cached_wins']}/{n_profiled}")
    print()


def main():
    parser = argparse.ArgumentParser(description="trawl vs Jina Reader benchmark")
    parser.add_argument("--only", help="Run only this case ID")
    parser.add_argument("--verbose", action="store_true", help="Show output previews")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    parser.add_argument("--no-profile", action="store_true",
                        help="Skip profile generation/cached runs")
    args = parser.parse_args()

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
        if not cases:
            print(f"No case with id={args.only!r}", file=sys.stderr)
            sys.exit(1)

    with_profile = not args.no_profile

    # Check VLM server if profiling
    if with_profile:
        vlm_url = os.environ.get("TRAWL_VLM_URL", "http://localhost:8080/v1")
        try:
            r = httpx.get(f"{vlm_url.rstrip('/')}/models", timeout=3.0)
            if r.status_code != 200:
                print(f"WARNING: VLM server at {vlm_url} returned {r.status_code}. "
                      f"Profile tests may fail.", file=sys.stderr)
        except Exception:
            print(f"WARNING: VLM server at {vlm_url} is unreachable. "
                  f"Use --no-profile to skip profile tests.", file=sys.stderr)
            with_profile = False

    results = []
    for case in cases:
        r = run_one(case, verbose=args.verbose, with_profile=with_profile)
        results.append(r)

    print_summary(results, with_profile=with_profile)

    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"benchmark_{ts}.json"
        save_data = []
        for r in results:
            entry = {"id": r["id"], "category": r["category"]}
            for key in ("trawl_base", "trawl_profile_gen", "trawl_cached", "jina"):
                if r.get(key) is not None:
                    entry[key] = {k: v for k, v in r[key].items() if k != "text"}
            save_data.append(entry)
        with out_path.open("w") as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
