"""WCXB runner — single-page evaluation.

Later tasks (4, 6, 7) extend this file with a Trafilatura baseline path,
the `run_all` orchestrator, CLI, and a sanity field. Kept deliberately
thin for now.
"""

from __future__ import annotations

import argparse
import gzip
import json
import json as _json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import trafilatura

from trawl.extraction import html_to_markdown

from benchmarks.wcxb.evaluate import word_f1, get_page_type


# Same options as trawl.extraction uses, minus favor_precision/favor_recall.
# This isolates the effect of trawl's 3-way + BS fallback vs plain Trafilatura
# markdown output on the same environment.
_TRAF_KWARGS = dict(
    output_format="markdown",
    include_links=True,
    include_images=False,
    include_tables=True,
    include_comments=False,
)


def _resolve_paths(data_dir: Path, page_id: str) -> tuple[Path, Path]:
    """Locate the html.gz + json for a page.

    Supports two layouts:
      1. Flat (fixtures):   <data_dir>/<id>.html.gz + <data_dir>/<id>.json
      2. Split (real WCXB): <data_dir>/html/<id>.html.gz + <data_dir>/ground-truth/<id>.json
    """
    data_dir = Path(data_dir)
    flat_html = data_dir / f"{page_id}.html.gz"
    flat_json = data_dir / f"{page_id}.json"
    if flat_html.exists() and flat_json.exists():
        return flat_html, flat_json
    split_html = data_dir / "html" / f"{page_id}.html.gz"
    split_json = data_dir / "ground-truth" / f"{page_id}.json"
    if split_html.exists() and split_json.exists():
        return split_html, split_json
    raise FileNotFoundError(f"WCXB page {page_id!r} not found under {data_dir}")


def _load_page(data_dir: Path, page_id: str) -> tuple[str, dict]:
    html_path, json_path = _resolve_paths(data_dir, page_id)
    html = gzip.decompress(html_path.read_bytes()).decode("utf-8", errors="replace")
    gt = json.loads(json_path.read_text())
    return html, gt


def _run_extractor(fn, html: str) -> tuple[str, int, str | None]:
    """Run an extractor; return (output, elapsed_ms, error_or_none)."""
    t0 = time.perf_counter()
    try:
        out = fn(html) or ""
    except Exception as exc:
        return "", int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}"
    return out, int((time.perf_counter() - t0) * 1000), None


def _score(output: str, ground_truth_text: str) -> dict:
    if not output:
        return {"f1": 0.0, "precision": 0.0, "recall": 0.0}
    p, r, f = word_f1(output, ground_truth_text)
    return {"f1": f, "precision": p, "recall": r}


def _trafilatura_baseline(html: str) -> str:
    return trafilatura.extract(html, **_TRAF_KWARGS) or ""


def _count_snippets_hit(output: str, snippets: list[str]) -> int:
    """Case-insensitive substring match count (mirrors WCXB's snippet_check)."""
    if not output:
        return 0
    out_lower = output.lower()
    return sum(1 for s in snippets if s and s.lower() in out_lower)


def evaluate_page(data_dir: Path, page_id: str) -> dict:
    """Evaluate trawl on a single WCXB page.

    Returns a dict with the trawl column of the raw.json schema. The
    Trafilatura baseline and snippet counts are added by Task 4's
    evaluate_page_with_baseline().
    """
    html, gt = _load_page(Path(data_dir), page_id)
    ground_truth_text = gt["ground_truth"]["main_content"]

    trawl_out, t_ms, err = _run_extractor(html_to_markdown, html)
    scores = _score(trawl_out, ground_truth_text)

    return {
        "id": page_id,
        "url": gt.get("url"),
        "page_type": get_page_type(gt),
        "trawl": {
            **scores,
            "time_ms": t_ms,
            "output_len": len(trawl_out),
            "error": err,
        },
    }


def evaluate_page_with_baseline(data_dir: Path, page_id: str) -> dict:
    """Evaluate trawl + Trafilatura baseline on a single WCXB page.

    Returns the full raw.json schema entry per the design spec.
    """
    html, gt = _load_page(Path(data_dir), page_id)
    gt_body = gt["ground_truth"]
    ground_truth_text = gt_body["main_content"]
    with_snips = gt_body.get("with") or []
    without_snips = gt_body.get("without") or []

    trawl_out, t_ms, t_err = _run_extractor(html_to_markdown, html)
    traf_out, b_ms, b_err = _run_extractor(_trafilatura_baseline, html)

    # Sanity: Trafilatura in default mode (no markdown flags), matching how
    # WCXB upstream measured the published F1=0.958. Used once after a full
    # run to verify the vendored evaluate.py reproduces the public number.
    sanity_out, s_ms, s_err = _run_extractor(
        lambda h: trafilatura.extract(h) or "", html
    )
    sanity = {
        **_score(sanity_out, ground_truth_text),
        "time_ms": s_ms,
        "error": s_err,
    }

    return {
        "id": page_id,
        "url": gt.get("url"),
        "page_type": get_page_type(gt),
        "trawl": {
            **_score(trawl_out, ground_truth_text),
            "time_ms": t_ms,
            "output_len": len(trawl_out),
            "error": t_err,
        },
        "trafilatura": {
            **_score(traf_out, ground_truth_text),
            "time_ms": b_ms,
            "output_len": len(traf_out),
            "error": b_err,
        },
        "with_snippets_hit": {
            "trawl": _count_snippets_hit(trawl_out, with_snips),
            "trafilatura": _count_snippets_hit(traf_out, with_snips),
            "total": len(with_snips),
        },
        "without_snippets_hit": {
            "trawl": _count_snippets_hit(trawl_out, without_snips),
            "trafilatura": _count_snippets_hit(traf_out, without_snips),
            "total": len(without_snips),
        },
        "sanity_traf_default": sanity,
    }


# ---------------------------------------------------------------------------
# Task 6: run_all orchestrator + argparse CLI
# ---------------------------------------------------------------------------

from benchmarks.wcxb.aggregate import aggregate, render_report


def _iter_page_ids(data_dir: Path, type_filter: str | None) -> list[str]:
    """Enumerate page IDs under data_dir, supporting flat + split layouts.

    Flat:  <data_dir>/<id>.json + <data_dir>/<id>.html.gz
    Split: <data_dir>/ground-truth/<id>.json + <data_dir>/html/<id>.html.gz
    """
    data_dir = Path(data_dir)
    ids: list[str] = []
    seen: set[str] = set()

    def _accept(json_path: Path) -> None:
        stem = json_path.stem
        if stem in seen:
            return
        # Paired html.gz must exist in the matching layout
        if json_path.parent == data_dir:
            html_path = data_dir / f"{stem}.html.gz"
        else:
            html_path = data_dir / "html" / f"{stem}.html.gz"
        if not html_path.exists():
            return
        if type_filter:
            try:
                meta = _json.loads(json_path.read_text())
            except Exception:
                return
            if get_page_type(meta) != type_filter:
                return
        seen.add(stem)
        ids.append(stem)

    # Flat layout
    for jp in sorted(data_dir.glob("*.json")):
        _accept(jp)
    # Split layout
    gt_dir = data_dir / "ground-truth"
    if gt_dir.is_dir():
        for jp in sorted(gt_dir.glob("*.json")):
            _accept(jp)

    return ids


def _git_short_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def run_all(
    *,
    data_dir: Path,
    out_dir: Path,
    limit: int | None,
    type_filter: str | None,
    no_baseline: bool,
) -> int:
    data_dir = Path(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = _iter_page_ids(data_dir, type_filter)
    if limit:
        ids = ids[:limit]

    results: list[dict] = []
    for i, page_id in enumerate(ids, start=1):
        try:
            if no_baseline:
                entry = evaluate_page(data_dir, page_id)
                entry["trafilatura"] = None
                entry["with_snippets_hit"] = None
                entry["without_snippets_hit"] = None
            else:
                entry = evaluate_page_with_baseline(data_dir, page_id)
        except FileNotFoundError:
            continue
        results.append(entry)

        if i % 100 == 0 or i == len(ids):
            rows_ok = [
                r for r in results
                if r["trawl"]["error"] is None
                and (no_baseline or r["trafilatura"]["error"] is None)
            ]
            if rows_ok:
                t_avg = sum(r["trawl"]["f1"] for r in rows_ok) / len(rows_ok)
                if no_baseline:
                    print(f"[{i}/{len(ids)}] trawl avg F1={t_avg:.3f}", file=sys.stderr)
                else:
                    b_avg = sum(r["trafilatura"]["f1"] for r in rows_ok) / len(rows_ok)
                    print(
                        f"[{i}/{len(ids)}] trawl avg F1={t_avg:.3f}, "
                        f"traf avg F1={b_avg:.3f}",
                        file=sys.stderr,
                    )

    (out_dir / "raw.json").write_text(
        _json.dumps(results, indent=2, ensure_ascii=False)
    )

    if not no_baseline:
        agg = aggregate(results)
        report = render_report(
            agg,
            corpus_label="dev" if data_dir.name == "dev" else data_dir.name,
            commit=_git_short_sha(),
            n_pages=len(results),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        (out_dir / "report.md").write_text(report)
    else:
        (out_dir / "report.md").write_text(
            "# WCXB extraction benchmark — trawl-only run (--no-baseline)\n\n"
            "See raw.json for per-page results.\n"
        )

    # 5% trawl error-rate threshold -> non-zero exit
    n_err = sum(1 for r in results if r["trawl"]["error"] is not None)
    if results and n_err / len(results) >= 0.05:
        print(
            f"ERROR: trawl error rate {n_err}/{len(results)} >= 5%",
            file=sys.stderr,
        )
        return 1
    return 0


def _main() -> int:
    p = argparse.ArgumentParser(description="Run the WCXB extraction benchmark.")
    p.add_argument("--data-dir", default=Path("benchmarks/wcxb/data/dev"), type=Path)
    p.add_argument(
        "--out-dir", default=None, type=Path,
        help="Output directory (default: benchmarks/results/wcxb_<timestamp>)",
    )
    p.add_argument("--limit", type=int, default=None)
    p.add_argument(
        "--type", dest="type_filter", default=None,
        help="Restrict to a single page_type (e.g. article, product, forum)",
    )
    p.add_argument(
        "--no-baseline", action="store_true",
        help="Skip Trafilatura baseline (trawl only)",
    )
    args = p.parse_args()

    if args.out_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.out_dir = Path("benchmarks/results") / f"wcxb_{ts}"

    return run_all(
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        limit=args.limit,
        type_filter=args.type_filter,
        no_baseline=args.no_baseline,
    )


if __name__ == "__main__":
    sys.exit(_main())
