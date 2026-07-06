"""Run rs-trafilatura against Trafilatura on the WCXB dev split."""

from __future__ import annotations

# ruff: noqa: I001

import argparse
import gzip
import json
import platform
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from statistics import mean, median
from typing import Any

import rs_trafilatura
import trafilatura

from evaluate import word_f1, get_page_type


DATA_DIR = Path(__file__).resolve().parent / "data" / "dev"

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


def _iter_page_ids(data_dir: Path, type_filter: str | None) -> list[str]:
    """Enumerate sorted page IDs without requiring both files to be present."""
    data_dir = Path(data_dir)
    stems: set[str] = set()

    for path in data_dir.glob("*.json"):
        stems.add(path.stem)
    for path in data_dir.glob("*.html.gz"):
        stems.add(path.name.removesuffix(".html.gz"))

    gt_dir = data_dir / "ground-truth"
    if gt_dir.is_dir():
        for path in gt_dir.glob("*.json"):
            stems.add(path.stem)

    html_dir = data_dir / "html"
    if html_dir.is_dir():
        for path in html_dir.glob("*.html.gz"):
            stems.add(path.name.removesuffix(".html.gz"))

    ids = sorted(stems)
    if not type_filter:
        return ids

    filtered: list[str] = []
    for page_id in ids:
        json_path = data_dir / f"{page_id}.json"
        if not json_path.exists():
            json_path = data_dir / "ground-truth" / f"{page_id}.json"
        if not json_path.exists():
            continue
        try:
            gt = json.loads(json_path.read_text())
        except Exception:
            continue
        if get_page_type(gt) == type_filter:
            filtered.append(page_id)
    return filtered


def _score(output: str, ground_truth_text: str) -> dict[str, float]:
    if not output:
        return {"f1": 0.0, "precision": 0.0, "recall": 0.0}
    precision, recall, f1 = word_f1(output, ground_truth_text)
    return {"f1": f1, "precision": precision, "recall": recall}


def _empty_result(time_ms: int, error: str) -> dict[str, Any]:
    return {
        **_score("", ""),
        "time_ms": time_ms,
        "output_len": 0,
        "error": error,
    }


def _extractor_result(output: str | None, time_ms: int, error: str | None) -> dict[str, Any]:
    if error is not None:
        return _empty_result(time_ms, error)
    text = output or ""
    if not text.strip():
        return _empty_result(time_ms, "empty output")
    return {
        "output": text,
        "time_ms": time_ms,
        "output_len": len(text),
        "error": None,
    }


def _score_result(result: dict[str, Any], ground_truth_text: str) -> dict[str, Any]:
    if result["error"] is not None:
        return {
            **_score("", ground_truth_text),
            "time_ms": result["time_ms"],
            "output_len": result["output_len"],
            "error": result["error"],
        }
    output = result.pop("output")
    return {
        **_score(output, ground_truth_text),
        "time_ms": result["time_ms"],
        "output_len": result["output_len"],
        "error": None,
    }


def _run_rs(html: str) -> tuple[Any | None, int, str | None]:
    t0 = time.perf_counter()
    try:
        result = rs_trafilatura.extract(html)
    except Exception as exc:
        return None, int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}"
    return result, int((time.perf_counter() - t0) * 1000), None


def _run_trafilatura(html: str) -> tuple[str | None, int, str | None]:
    t0 = time.perf_counter()
    try:
        output = trafilatura.extract(html, **_TRAF_KWARGS)
    except Exception as exc:
        return None, int((time.perf_counter() - t0) * 1000), f"{type(exc).__name__}: {exc}"
    return output, int((time.perf_counter() - t0) * 1000), None


def evaluate_page(data_dir: Path, page_id: str) -> dict[str, Any]:
    html, gt = _load_page(data_dir, page_id)
    gt_body = gt["ground_truth"]
    ground_truth_text = gt_body.get("main_content", "")
    page_type = get_page_type(gt)

    rs_result, rs_ms, rs_error = _run_rs(html)
    rs_md_output = None
    rs_md_ms = rs_ms
    rs_md_error = rs_error
    if rs_md_error is None:
        content_html = getattr(rs_result, "content_html", None)
        if content_html:
            t0 = time.perf_counter()
            try:
                # rs-trafilatura 0.1.1 leaves content_markdown unset.
                rs_md_output = rs_trafilatura.html_to_markdown(content_html)
            except Exception as exc:
                rs_md_error = f"{type(exc).__name__}: {exc}"
            finally:
                rs_md_ms += int((time.perf_counter() - t0) * 1000)
    rs_md = _extractor_result(rs_md_output, rs_md_ms, rs_md_error)
    rs_text = _extractor_result(getattr(rs_result, "main_content", None), rs_ms, rs_error)

    traf_output, traf_ms, traf_error = _run_trafilatura(html)
    traf_baseline = _extractor_result(traf_output, traf_ms, traf_error)

    return {
        "id": page_id,
        "url": gt.get("url"),
        "page_type": page_type,
        "rs_page_type": getattr(rs_result, "page_type", None),
        "rs_classification_confidence": getattr(rs_result, "classification_confidence", None),
        "rs_md": _score_result(rs_md, ground_truth_text),
        "rs_text": _score_result(rs_text, ground_truth_text),
        "traf_baseline": _score_result(traf_baseline, ground_truth_text),
    }


def _mean_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _median_or_zero(values: list[float | int]) -> float:
    return float(median(values)) if values else 0.0


def _summarize_extractor(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    ok_rows = [row for row in rows if row[key]["error"] is None]
    all_f1 = [row[key]["f1"] if row[key]["error"] is None else 0.0 for row in rows]
    ok_f1 = [row[key]["f1"] for row in ok_rows]
    return {
        "mean_f1_over_ok": _mean_or_zero(ok_f1),
        "mean_f1_over_all": _mean_or_zero(all_f1),
        "median_f1_over_ok": _median_or_zero(ok_f1),
        "n_ok": len(ok_rows),
        "n_fail": len(rows) - len(ok_rows),
        "median_time_ms": _median_or_zero([row[key]["time_ms"] for row in ok_rows]),
    }


def _summarize_by_type(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["page_type"] or "unknown", []).append(row)
    return {
        page_type: _summarize_extractor(type_rows, key)
        for page_type, type_rows in sorted(by_type.items())
    }


def _version(package_name: str) -> str:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return "unknown"


def summarize(
    rows: list[dict[str, Any]],
    *,
    skipped_missing: int,
    skipped_read_error: int,
) -> dict[str, Any]:
    extractor_names = ("rs_md", "rs_text", "traf_baseline")
    gt_distribution = Counter(row["page_type"] for row in rows)
    rs_distribution = Counter(
        row["rs_page_type"] for row in rows if row["rs_page_type"] is not None
    )
    gt_vocab = set(gt_distribution)
    rs_vocab = set(rs_distribution)
    vocabularies_match = bool(gt_vocab) and gt_vocab == rs_vocab

    page_type_summary: dict[str, Any] = {
        "ground_truth_distribution": dict(sorted(gt_distribution.items())),
        "rs_distribution": dict(sorted(rs_distribution.items())),
        "vocabularies_match": vocabularies_match,
    }
    if vocabularies_match:
        comparable = [row for row in rows if row["rs_page_type"] is not None]
        matches = sum(1 for row in comparable if row["rs_page_type"] == row["page_type"])
        page_type_summary["agreement_rate"] = matches / len(comparable) if comparable else 0.0

    return {
        "n_pages": len(rows),
        "skipped_missing": skipped_missing,
        "skipped_read_error": skipped_read_error,
        "extractors": {
            name: {
                **_summarize_extractor(rows, name),
                "by_type": _summarize_by_type(rows, name),
            }
            for name in extractor_names
        },
        "page_type": page_type_summary,
        "versions": {
            "python": sys.version.split()[0],
            "trafilatura": getattr(trafilatura, "__version__", "unknown"),
            "rs_trafilatura": _version("rs-trafilatura"),
            "platform": platform.platform(),
        },
    }


def _print_summary(summary: dict[str, Any], out_dir: Path) -> None:
    print(
        f"{'extractor':<14} {'ok':>5} {'fail':>5} {'f1_ok':>8} "
        f"{'f1_all':>8} {'med_f1':>8} {'med_ms':>8}"
    )
    print("-" * 66)
    for name, stats in summary["extractors"].items():
        print(
            f"{name:<14} {stats['n_ok']:>5d} {stats['n_fail']:>5d} "
            f"{stats['mean_f1_over_ok']:>8.3f} {stats['mean_f1_over_all']:>8.3f} "
            f"{stats['median_f1_over_ok']:>8.3f} {stats['median_time_ms']:>8.1f}"
        )
    print(
        f"skipped_missing={summary['skipped_missing']} "
        f"skipped_read_error={summary['skipped_read_error']}"
    )
    page_type = summary["page_type"]
    if page_type["vocabularies_match"]:
        print(f"rs_page_type_agreement={page_type['agreement_rate']:.3f}")
    else:
        print("rs_page_type_agreement=not_reported_vocab_mismatch")
    print(f"out={out_dir}")


def run_all(*, limit: int | None, type_filter: str | None, out_dir: Path) -> int:
    ids = _iter_page_ids(DATA_DIR, type_filter)
    if limit is not None:
        ids = ids[:limit]

    rows: list[dict[str, Any]] = []
    skipped_missing = 0
    skipped_read_error = 0

    for page_id in ids:
        try:
            rows.append(evaluate_page(DATA_DIR, page_id))
        except FileNotFoundError:
            skipped_missing += 1
        except Exception:
            skipped_read_error += 1

    summary = summarize(
        rows,
        skipped_missing=skipped_missing,
        skipped_read_error=skipped_read_error,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    if not rows:
        print(
            "no pages found "
            f"(skipped_missing={skipped_missing}, skipped_read_error={skipped_read_error})",
            file=sys.stderr,
        )
        return 1

    _print_summary(summary, out_dir)
    return 0


def _default_out_dir() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "results" / f"rs_traf_{ts}"


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run rs-trafilatura and Trafilatura on WCXB dev.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--type",
        dest="type_filter",
        default=None,
        help="Restrict to a single page_type (e.g. article, product, forum)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output directory (default: benchmarks/wcxb/results/rs_traf_<UTC timestamp>)",
    )
    args = parser.parse_args()

    return run_all(
        limit=args.limit,
        type_filter=args.type_filter,
        out_dir=args.out or _default_out_dir(),
    )


if __name__ == "__main__":
    sys.exit(_main())
