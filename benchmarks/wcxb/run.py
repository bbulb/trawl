"""WCXB runner — single-page evaluation.

Later tasks (4, 6, 7) extend this file with a Trafilatura baseline path,
the `run_all` orchestrator, CLI, and a sanity field. Kept deliberately
thin for now.
"""

from __future__ import annotations

import gzip
import json
import time
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
    }
