"""WCXB result aggregation + report rendering.

Pure functions: (list of raw entries) -> summary dict -> markdown string.
No IO. Keeping the reporting layer independently testable and reusable
by future Phase 2 tooling.
"""

from __future__ import annotations

from statistics import mean, median
from typing import Iterable


def _ok(entry: dict, key: str) -> bool:
    return entry[key]["error"] is None


def _mean_or_zero(values: list[float]) -> float:
    return mean(values) if values else 0.0


def aggregate(entries: Iterable[dict], top_n: int = 10) -> dict:
    """Compute overall + per-type F1 summaries and top wins/losses.

    Rows with an error on either extractor are excluded from the averaged
    comparison (both columns need to be valid for delta to be meaningful) but
    errors are counted separately.
    """
    entries = list(entries)
    both_ok = [e for e in entries if _ok(e, "trawl") and _ok(e, "trafilatura")]

    def _agg_block(rows: list[dict], key: str) -> dict:
        if not rows:
            return {"f1": 0.0, "precision": 0.0, "recall": 0.0, "median_time_ms": 0}
        return {
            "f1": _mean_or_zero([r[key]["f1"] for r in rows]),
            "precision": _mean_or_zero([r[key]["precision"] for r in rows]),
            "recall": _mean_or_zero([r[key]["recall"] for r in rows]),
            "median_time_ms": int(median([r[key]["time_ms"] for r in rows])),
        }

    overall = {
        "n_included": len(both_ok),
        "trawl": _agg_block(both_ok, "trawl"),
        "trafilatura": _agg_block(both_ok, "trafilatura"),
    }
    overall["delta_f1"] = overall["trawl"]["f1"] - overall["trafilatura"]["f1"]

    by_type: dict[str, list[dict]] = {}
    for e in both_ok:
        by_type.setdefault(e["page_type"] or "unknown", []).append(e)

    by_type_rows = []
    for ptype, rows in sorted(by_type.items()):
        t_f1 = _mean_or_zero([r["trawl"]["f1"] for r in rows])
        b_f1 = _mean_or_zero([r["trafilatura"]["f1"] for r in rows])
        by_type_rows.append({
            "type": ptype,
            "n": len(rows),
            "trawl_f1": t_f1,
            "trafilatura_f1": b_f1,
            "delta": t_f1 - b_f1,
        })

    ranked = sorted(
        both_ok,
        key=lambda e: e["trawl"]["f1"] - e["trafilatura"]["f1"],
        reverse=True,
    )
    top_wins = [
        {"id": e["id"], "delta": e["trawl"]["f1"] - e["trafilatura"]["f1"]}
        for e in ranked[:top_n]
        if (e["trawl"]["f1"] - e["trafilatura"]["f1"]) > 0
    ]
    top_losses = [
        {"id": e["id"], "delta": e["trawl"]["f1"] - e["trafilatura"]["f1"]}
        for e in list(reversed(ranked))[:top_n]
        if (e["trawl"]["f1"] - e["trafilatura"]["f1"]) < 0
    ]

    errors = {
        "trawl": sum(1 for e in entries if e["trawl"]["error"] is not None),
        "trafilatura": sum(1 for e in entries if e["trafilatura"]["error"] is not None),
        "trawl_ids": [e["id"] for e in entries if e["trawl"]["error"] is not None],
        "trafilatura_ids": [e["id"] for e in entries if e["trafilatura"]["error"] is not None],
    }

    return {
        "overall": overall,
        "by_type": by_type_rows,
        "top_wins": top_wins,
        "top_losses": top_losses,
        "errors": errors,
    }


def render_report(
    agg: dict,
    *,
    corpus_label: str,
    commit: str,
    n_pages: int,
    timestamp: str = "",
) -> str:
    o = agg["overall"]
    header = f"# WCXB extraction benchmark — {timestamp}".rstrip()
    lines = [
        header,
        "",
        f"Corpus: WCXB {corpus_label} split, {n_pages} pages.",
        f"Commit: {commit}",
        "",
        "## Overall",
        "",
        "| Extractor   | F1    | Precision | Recall | Median time |",
        "|-------------|-------|-----------|--------|-------------|",
        f"| trawl       | {o['trawl']['f1']:.3f} | {o['trawl']['precision']:.3f}     | {o['trawl']['recall']:.3f}  | {o['trawl']['median_time_ms']:>3d} ms      |",
        f"| trafilatura | {o['trafilatura']['f1']:.3f} | {o['trafilatura']['precision']:.3f}     | {o['trafilatura']['recall']:.3f}  | {o['trafilatura']['median_time_ms']:>3d} ms      |",
        "",
        f"Delta F1 (trawl - trafilatura) = {o['delta_f1']:+.3f}",
        "",
        "## By page type",
        "",
        "| Type          |   N  | trawl F1 | traf F1 | Delta  |",
        "|---------------|------|----------|---------|--------|",
    ]
    for r in agg["by_type"]:
        lines.append(
            f"| {r['type']:<13} | {r['n']:>4d} | {r['trawl_f1']:.3f}    | {r['trafilatura_f1']:.3f}   | {r['delta']:+.3f} |"
        )

    lines += ["", f"## Top {len(agg['top_wins'])} trawl wins (Delta F1)", ""]
    for w in agg["top_wins"]:
        lines.append(f"- {w['id']}: {w['delta']:+.3f}")

    lines += ["", f"## Top {len(agg['top_losses'])} trawl losses (Delta F1)", ""]
    for l in agg["top_losses"]:
        lines.append(f"- {l['id']}: {l['delta']:+.3f}")

    err = agg["errors"]
    lines += [
        "",
        "## Errors",
        f"trawl: {err['trawl']} ({', '.join(err['trawl_ids']) or 'none'})",
        f"trafilatura: {err['trafilatura']} ({', '.join(err['trafilatura_ids']) or 'none'})",
        "",
    ]
    return "\n".join(lines)
