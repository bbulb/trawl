"""Query-based reader comparison benchmark."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import yaml

REQUIRED_CASE_FIELDS = {"id", "category", "url", "query", "expected_facts", "failure_class"}
RESULT_FIELDS = [
    "case_id",
    "category",
    "provider",
    "status",
    "latency_ms",
    "tokens_returned",
    "n_chunks_total",
    "recall_at_k",
    "mrr_at_k",
    "answer_grounding_hit",
    "failure_phase",
    "missing_facts",
    "error",
]


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load and validate reader-comparison benchmark cases."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("case file must contain a cases list")
    for case in cases:
        validate_case(case)
    return cases


def validate_case(case: dict[str, Any]) -> None:
    """Validate one reader-comparison benchmark case."""
    missing = sorted(REQUIRED_CASE_FIELDS - set(case))
    if missing:
        raise ValueError(f"case {case.get('id', '<unknown>')} missing fields: {', '.join(missing)}")
    if not case["expected_facts"]:
        raise ValueError(f"case {case['id']} must define expected_facts")
    for fact in case["expected_facts"]:
        if "id" not in fact:
            raise ValueError(f"case {case['id']} has fact without id")
        checks = [name for name in ("all_of", "any_of", "pattern") if name in fact]
        if len(checks) != 1:
            raise ValueError(f"fact {fact['id']} must define exactly one matcher")


def fact_matches(text: str, fact: dict[str, Any]) -> bool:
    """Return whether text satisfies a fact matcher."""
    if "all_of" in fact:
        return all(value in text for value in fact["all_of"])
    if "any_of" in fact:
        return any(value in text for value in fact["any_of"])
    return re.search(fact["pattern"], text) is not None


def score_ranked_texts(ranked_texts: list[str], facts: list[dict[str, Any]]) -> dict[str, Any]:
    """Score expected fact coverage over ranked chunks or documents."""
    found: dict[str, int] = {}
    for rank, _text in enumerate(ranked_texts, start=1):
        cumulative = "\n\n".join(ranked_texts[:rank])
        for fact in facts:
            if fact["id"] not in found and fact_matches(cumulative, fact):
                found[fact["id"]] = rank
    missing = [fact["id"] for fact in facts if fact["id"] not in found]
    recall = len(found) / len(facts) if facts else 0.0
    first_rank = min(found.values()) if found else None
    return {
        "recall_at_k": recall,
        "mrr_at_k": (1.0 / first_rank) if first_rank else 0.0,
        "answer_grounding_hit": not missing,
        "missing_facts": missing,
    }


def estimate_tokens(text: str) -> int:
    """Rough token count for reader output size comparisons."""
    if not text:
        return 0
    cjk = sum(
        1
        for char in text
        if "\u4e00" <= char <= "\u9fff"
        or "\uac00" <= char <= "\ud7af"
        or "\u3040" <= char <= "\u30ff"
    )
    non_cjk = len(text) - cjk
    return int(non_cjk / 4.0 + cjk / 1.5)


def build_scored_result(
    *,
    case: dict[str, Any],
    provider: str,
    status: str,
    latency_ms: int,
    ranked_texts: list[str],
    n_chunks_total: int | None,
    error: str | None,
) -> dict[str, Any]:
    """Build a normalized provider result and classify benchmark failures."""
    text = "\n\n".join(ranked_texts)
    score = score_ranked_texts(ranked_texts, case["expected_facts"])
    failure_phase = None
    result_status = status

    if error:
        result_status = "error"
        failure_phase = case.get("failure_class", {}).get("on_fetch_error", "provider_error")
    elif not text.strip():
        result_status = "fail"
        failure_phase = case.get("failure_class", {}).get("on_empty_output", "extraction")
    elif not score["answer_grounding_hit"]:
        result_status = "fail"
        failure_phase = case.get("failure_class", {}).get("on_missing_facts", "retrieval")

    return {
        "case_id": case["id"],
        "category": case.get("category"),
        "provider": provider,
        "status": result_status,
        "latency_ms": latency_ms,
        "tokens_returned": estimate_tokens(text),
        "n_chunks_total": n_chunks_total,
        "recall_at_k": score["recall_at_k"],
        "mrr_at_k": score["mrr_at_k"],
        "answer_grounding_hit": score["answer_grounding_hit"],
        "failure_phase": failure_phase,
        "missing_facts": score["missing_facts"],
        "error": error,
    }


def build_skip_result(case: dict[str, Any], provider: str, reason: str) -> dict[str, Any]:
    """Build a normalized skip record for unavailable optional providers."""
    return {
        "case_id": case["id"],
        "category": case.get("category"),
        "provider": provider,
        "status": "skipped",
        "latency_ms": 0,
        "tokens_returned": 0,
        "n_chunks_total": None,
        "recall_at_k": 0.0,
        "mrr_at_k": 0.0,
        "answer_grounding_hit": False,
        "failure_phase": "not_configured",
        "missing_facts": [fact["id"] for fact in case.get("expected_facts", [])],
        "error": reason,
    }


def write_outputs(output_dir: Path, results: list[dict[str, Any]]) -> None:
    """Write raw JSONL, compact CSV, and a Markdown benchmark report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = output_dir / "results.jsonl"
    csv_path = output_dir / "summary.csv"
    report_path = output_dir / "report.md"

    with jsonl_path.open("w", encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            row = dict(result)
            row["missing_facts"] = ",".join(result.get("missing_facts") or [])
            writer.writerow(row)

    report_path.write_text(render_report(results), encoding="utf-8")


def render_report(results: list[dict[str, Any]]) -> str:
    """Render a compact Markdown summary for reader-comparison results."""
    active = [result for result in results if result["status"] != "skipped"]
    by_provider = sorted({result["provider"] for result in results})
    lines = [
        "# Reader comparison benchmark",
        "",
        f"Rows: {len(results)}",
        "",
        "## Provider summary",
        "",
        "| Provider | Rows | Pass rate | Avg latency ms | Avg tokens |",
        "|---|---:|---:|---:|---:|",
    ]
    for provider in by_provider:
        provider_rows = [result for result in results if result["provider"] == provider]
        quality_rows = [result for result in provider_rows if result["status"] != "skipped"]
        ok_rows = [result for result in quality_rows if result["status"] == "ok"]
        pass_rate = (len(ok_rows) / len(quality_rows)) if quality_rows else 0.0
        avg_latency = _average([result["latency_ms"] for result in quality_rows])
        avg_tokens = _average([result["tokens_returned"] for result in quality_rows])
        lines.append(
            f"| {provider} | {len(provider_rows)} | {pass_rate:.2f} | "
            f"{avg_latency:.1f} | {avg_tokens:.1f} |"
        )

    if active:
        lines.extend(
            [
                "",
                "## Metrics",
                "",
                "- Recall@k: fact-group recall across returned ranked text.",
                "- MRR@k: reciprocal rank for the first satisfied fact group.",
                "- answer_grounding_hit: true when every expected fact group is present.",
                "- failure_phase: fetch, extraction, retrieval, rerank, not_configured, or provider_error.",
            ]
        )
    return "\n".join(lines) + "\n"


def _average(values: list[int | float]) -> float:
    return sum(values) / len(values) if values else 0.0
