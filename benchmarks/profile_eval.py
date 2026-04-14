"""Profile generation evaluator for VLM prompt tuning.

Runs generate_profile on each URL and records detailed intermediate
results: VLM response, anchor matching, LCA path, selector quality.
This data drives prompt iteration — run before and after prompt changes
to measure improvement.

Invoke:
    python benchmarks/profile_eval.py
    python benchmarks/profile_eval.py --only wiki_llm
    python benchmarks/profile_eval.py --category docs
    python benchmarks/profile_eval.py --save
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from trawl.fetchers.playwright import render_session
from trawl.profiles import (
    VLMError,
    call_vlm,
    load_profile,
    profile_path_for,
)
from trawl.profiles.mapper import find_main_subtree

BENCH_DIR = Path(__file__).parent
CASES_FILE = BENCH_DIR / "profile_eval_cases.yaml"
RESULTS_DIR = BENCH_DIR / "results"


def load_cases() -> list[dict]:
    with CASES_FILE.open() as f:
        return yaml.safe_load(f)["cases"]


def _screenshot_workdir(url: str) -> Path:
    from trawl.profiles import url_hash
    d = Path("/tmp/trawl_profile_eval") / url_hash(url)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _capture_screenshot(page, work_dir: Path) -> Path:
    """Capture a clipped screenshot, same as generate_profile."""
    path = work_dir / "screenshot.png"
    # Clip to viewport height * 3 or page height, whichever is smaller
    viewport = page.viewport_size or {"width": 1280, "height": 720}
    max_h = viewport["height"] * 3
    page_h = page.evaluate("() => document.documentElement.scrollHeight")
    clip_h = min(page_h, max_h)
    page.screenshot(
        path=str(path),
        clip={"x": 0, "y": 0, "width": viewport["width"], "height": clip_h},
    )
    return path


def eval_one(case: dict) -> dict:
    """Evaluate profile generation for one URL, returning detailed results."""
    cid = case["id"]
    url = case["url"]
    result = {
        "id": cid,
        "category": case.get("category", ""),
        "url": url,
        "description": case.get("description", ""),
    }

    # Clear any existing profile
    p = profile_path_for(url)
    if p.exists():
        p.unlink()

    work_dir = _screenshot_workdir(url)
    try:
        # Stage 1: Render
        t0 = time.monotonic()
        try:
            ctx = render_session(url)
            r = ctx.__enter__()
            render_s = round(time.monotonic() - t0, 2)
            result["render_s"] = render_s
            result["render_ok"] = True
        except Exception as e:
            render_s = round(time.monotonic() - t0, 2)
            result["render_s"] = render_s
            result["render_ok"] = False
            result["render_error"] = f"{type(e).__name__}: {e}"
            result["overall"] = "fail"
            result["fail_stage"] = "render"
            return result

        try:
            # Stage 2: Screenshot + VLM
            screenshot_path = _capture_screenshot(r.page, work_dir)
            t1 = time.monotonic()
            try:
                vlm_response = call_vlm(screenshot_path)
                vlm_s = round(time.monotonic() - t1, 2)
                result["vlm_s"] = vlm_s
                result["vlm_ok"] = True
                result["vlm_page_type"] = vlm_response.page_type
                result["vlm_structure"] = vlm_response.structure_description
                result["vlm_anchors"] = vlm_response.content_anchors
                result["vlm_noise_labels"] = vlm_response.noise_labels
                result["vlm_n_anchors"] = len(vlm_response.content_anchors)
                if vlm_response.item_hints:
                    result["vlm_has_repeating"] = vlm_response.item_hints.has_repeating_items
                    result["vlm_item_desc"] = vlm_response.item_hints.item_description
                else:
                    result["vlm_has_repeating"] = False
            except VLMError as e:
                vlm_s = round(time.monotonic() - t1, 2)
                result["vlm_s"] = vlm_s
                result["vlm_ok"] = False
                result["vlm_error"] = str(e)
                result["overall"] = "fail"
                result["fail_stage"] = "vlm"
                return result

            # Stage 3: Mapper (anchor matching + LCA)
            t2 = time.monotonic()
            map_result = find_main_subtree(r.page, vlm_response.content_anchors)
            mapper_s = round(time.monotonic() - t2, 2)
            result["mapper_s"] = mapper_s

            result["anchors_found"] = [
                {
                    "anchor": a.anchor,
                    "found_count": a.found_count,
                    "container_tag": a.container_path[-1] if a.container_path else "",
                    "container_depth": len(a.container_path),
                    "container_chars": a.container_chars,
                }
                for a in map_result.anchors_found
            ]
            result["anchors_missed"] = map_result.anchors_missed
            result["anchors_outlier"] = map_result.outlier_anchors
            result["n_found"] = len(map_result.anchors_found)
            result["n_missed"] = len(map_result.anchors_missed)
            result["n_outlier"] = len(map_result.outlier_anchors)
            result["mapper_notes"] = list(map_result.notes)

            result["lca_tag"] = map_result.lca_tag
            result["lca_path"] = map_result.lca_path
            result["lca_depth"] = len(map_result.lca_path)
            result["subtree_chars"] = map_result.subtree_chars

            if map_result.selector is None:
                result["selector"] = None
                result["overall"] = "fail"
                result["fail_stage"] = "mapper"
            else:
                result["selector"] = map_result.selector
                # Quality assessment
                tag = map_result.lca_tag
                depth = len(map_result.lca_path)
                chars = map_result.subtree_chars

                # Heuristic quality rating
                if tag in ("ARTICLE", "MAIN") or (
                    depth >= 4 and tag not in ("BODY", "HTML", "DIV")
                ):
                    result["overall"] = "ideal"
                elif depth >= 3:
                    result["overall"] = "acceptable"
                else:
                    result["overall"] = "too_wide"

            result["total_s"] = round(render_s + vlm_s + mapper_s, 2)

        finally:
            ctx.__exit__(None, None, None)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        # Clean up profile
        if p.exists():
            p.unlink()

    return result


def print_results(results: list[dict]):
    """Print evaluation results."""
    print()
    print("=" * 120)
    print("PROFILE EVALUATION RESULTS")
    print("=" * 120)

    header = (
        f"{'ID':<22} {'Cat':<14} {'Overall':<11} "
        f"{'VLM type':<20} {'Anchors':>8} {'Found':>6} {'Miss':>5} {'Drop':>5} "
        f"{'LCA tag':<10} {'Depth':>5} {'Chars':>8} "
        f"{'Time':>6}"
    )
    print(header)
    print("-" * 120)

    # Category stats
    cat_stats: dict[str, dict] = {}
    overall_counts = {"ideal": 0, "acceptable": 0, "too_wide": 0, "fail": 0}

    for r in results:
        cat = r.get("category", "?")
        overall = r.get("overall", "?")
        vlm_type = r.get("vlm_page_type", "")[:18]
        n_anchors = r.get("vlm_n_anchors", 0)
        n_found = r.get("n_found", 0)
        n_missed = r.get("n_missed", 0)
        n_outlier = r.get("n_outlier", 0)
        lca_tag = r.get("lca_tag", "")
        lca_depth = r.get("lca_depth", 0)
        subtree_chars = r.get("subtree_chars", 0)
        total_s = r.get("total_s", 0)
        selector = r.get("selector", "")

        # Colorize overall
        if overall == "ideal":
            ov_str = "IDEAL"
        elif overall == "acceptable":
            ov_str = "OK"
        elif overall == "too_wide":
            ov_str = "WIDE"
        else:
            stage = r.get("fail_stage", "")
            ov_str = f"FAIL({stage})"

        print(
            f"{r['id']:<22} {cat:<14} {ov_str:<11} "
            f"{vlm_type:<20} {n_anchors:>8} {n_found:>6} {n_missed:>5} {n_outlier:>5} "
            f"{lca_tag:<10} {lca_depth:>5} {subtree_chars:>8,} "
            f"{total_s:>5.1f}s"
        )
        if selector:
            sel_display = selector[:70] + "..." if len(selector) > 70 else selector
            print(f"{'':>22} selector: {sel_display}")

        # Accumulate stats
        overall_counts[overall] = overall_counts.get(overall, 0) + 1
        if cat not in cat_stats:
            cat_stats[cat] = {"total": 0, "ideal": 0, "acceptable": 0, "too_wide": 0, "fail": 0}
        cat_stats[cat]["total"] += 1
        cat_stats[cat][overall] = cat_stats[cat].get(overall, 0) + 1

    print("-" * 120)

    # Summary by category
    print()
    print("BY CATEGORY:")
    print(f"{'Category':<16} {'Total':>6} {'Ideal':>6} {'OK':>6} {'Wide':>6} {'Fail':>6} {'Success%':>9}")
    print("-" * 60)
    for cat, s in sorted(cat_stats.items()):
        success = s["ideal"] + s["acceptable"]
        pct = f"{100*success/s['total']:.0f}%" if s["total"] else "n/a"
        print(
            f"{cat:<16} {s['total']:>6} {s['ideal']:>6} {s['acceptable']:>6} "
            f"{s['too_wide']:>6} {s['fail']:>6} {pct:>9}"
        )

    total = len(results)
    success = overall_counts.get("ideal", 0) + overall_counts.get("acceptable", 0)
    print("-" * 60)
    print(
        f"{'TOTAL':<16} {total:>6} {overall_counts.get('ideal',0):>6} "
        f"{overall_counts.get('acceptable',0):>6} {overall_counts.get('too_wide',0):>6} "
        f"{overall_counts.get('fail',0):>6} {100*success/total:.0f}%"
    )

    # Failure analysis
    fails = [r for r in results if r.get("overall") == "fail"]
    if fails:
        print()
        print("FAILURE DETAILS:")
        for r in fails:
            stage = r.get("fail_stage", "?")
            print(f"  {r['id']}: stage={stage}")
            if stage == "render":
                print(f"    error: {r.get('render_error', '')}")
            elif stage == "vlm":
                print(f"    error: {r.get('vlm_error', '')}")
            elif stage == "mapper":
                missed = r.get("anchors_missed", [])
                notes = r.get("mapper_notes", [])
                print(f"    anchors total={r.get('vlm_n_anchors',0)} "
                      f"found={r.get('n_found',0)} missed={r.get('n_missed',0)}")
                if missed:
                    print(f"    missed anchors: {missed[:5]}")
                if notes:
                    for n in notes[:3]:
                        print(f"    note: {n}")

    # Too-wide analysis
    wides = [r for r in results if r.get("overall") == "too_wide"]
    if wides:
        print()
        print("TOO-WIDE SELECTORS:")
        for r in wides:
            print(f"  {r['id']}: tag={r.get('lca_tag','')} depth={r.get('lca_depth',0)} "
                  f"chars={r.get('subtree_chars',0):,}")
            print(f"    selector: {r.get('selector','')[:80]}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Profile generation evaluator")
    parser.add_argument("--only", help="Run only this case ID")
    parser.add_argument("--category", help="Run only this category")
    parser.add_argument("--save", action="store_true", help="Save results to JSON")
    args = parser.parse_args()

    # Check VLM server
    vlm_url = os.environ.get("TRAWL_VLM_URL", "http://localhost:8080/v1")
    try:
        r = httpx.get(f"{vlm_url.rstrip('/')}/models", timeout=3.0)
        if r.status_code != 200:
            print(f"ERROR: VLM server at {vlm_url} returned {r.status_code}", file=sys.stderr)
            sys.exit(1)
    except Exception:
        print(f"ERROR: VLM server at {vlm_url} unreachable", file=sys.stderr)
        sys.exit(1)

    cases = load_cases()
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if not cases:
        print("No matching cases", file=sys.stderr)
        sys.exit(1)

    print(f"Running {len(cases)} profile evaluations...")

    results = []
    for i, case in enumerate(cases, 1):
        print(f"\n[{i}/{len(cases)}] {case['id']}: {case.get('description', '')}")
        print(f"  URL: {case['url']}")
        r = eval_one(case)
        overall = r.get("overall", "?")
        print(f"  Result: {overall}", end="")
        if r.get("selector"):
            sel = r["selector"][:60]
            print(f" — {sel}")
        else:
            stage = r.get("fail_stage", "")
            print(f" (stage: {stage})")
        results.append(r)

    print_results(results)

    if args.save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = RESULTS_DIR / f"profile_eval_{ts}.json"
        with out_path.open("w") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
