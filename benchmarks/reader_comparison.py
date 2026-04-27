"""Query-based reader comparison benchmark."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

BENCH_DIR = Path(__file__).resolve().parent
DEFAULT_CASES_FILE = BENCH_DIR / "reader_comparison_cases.yaml"
DEFAULT_RESULTS_ROOT = BENCH_DIR / "results" / "reader-comparison"
JINA_BASE = "https://r.jina.ai"
JINA_TIMEOUT = 60.0
DEFAULT_PROVIDERS = ["trawl", "jina", "trafilatura"]
PROVIDER_CHOICES = ["trawl", "jina", "trafilatura", "firecrawl", "crawl4ai"]

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


def select_cases(
    cases: list[dict[str, Any]], *, only: str | None, limit: int | None
) -> list[dict[str, Any]]:
    """Filter benchmark cases by id and optional limit."""
    selected = [case for case in cases if only is None or case["id"] == only]
    if only is not None and not selected:
        raise ValueError(f"unknown case id: {only}")
    if limit is not None:
        selected = selected[:limit]
    return selected


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


def run_trawl_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Run trawl selective retrieval for a case."""
    from trawl import fetch_relevant, to_dict

    started = time.monotonic()
    try:
        result = fetch_relevant(case["url"], case["query"], use_rerank=True)
        elapsed = int((time.monotonic() - started) * 1000)
        payload = to_dict(result)
        chunks = payload.get("chunks") or []
        ranked_texts = [
            "\n".join(part for part in (chunk.get("heading"), chunk.get("text")) if part)
            for chunk in chunks
        ]
        return build_scored_result(
            case=case,
            provider="trawl",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=ranked_texts,
            n_chunks_total=payload.get("n_chunks_total"),
            error=payload.get("error"),
        )
    except Exception as exc:  # pragma: no cover - network/runtime provider behavior
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="trawl",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_jina_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Run Jina Reader for a case."""
    started = time.monotonic()
    try:
        headers = {"Accept": "text/markdown"}
        api_key = os.environ.get("JINA_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(timeout=JINA_TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{JINA_BASE}/{case['url']}", headers=headers)
            response.raise_for_status()
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="jina",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=[response.text],
            n_chunks_total=None,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network provider behavior
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="jina",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_trafilatura_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Run local Trafilatura extraction as a full-page baseline."""
    try:
        import trafilatura
    except ImportError:
        return build_skip_result(case, "trafilatura", "trafilatura is not installed")

    started = time.monotonic()
    try:
        downloaded = trafilatura.fetch_url(case["url"])
        text = ""
        if downloaded:
            text = (
                trafilatura.extract(
                    downloaded,
                    output_format="markdown",
                    include_tables=True,
                    include_links=True,
                    include_images=False,
                    include_comments=False,
                )
                or ""
            )
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="trafilatura",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=[text] if text else [],
            n_chunks_total=None,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network provider behavior
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="trafilatura",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def run_firecrawl_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Record Firecrawl availability until an adapter is configured."""
    if not os.environ.get("FIRECRAWL_API_KEY"):
        return build_skip_result(case, "firecrawl", "FIRECRAWL_API_KEY not set")
    return build_skip_result(case, "firecrawl", "Firecrawl adapter is not implemented")


def run_crawl4ai_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Record Crawl4AI availability until an adapter is configured."""
    try:
        __import__("crawl4ai")
    except ImportError:
        return build_skip_result(case, "crawl4ai", "crawl4ai is not installed")
    return build_skip_result(case, "crawl4ai", "Crawl4AI adapter is not implemented")


def run_provider(case: dict[str, Any], provider: str) -> dict[str, Any]:
    """Run one configured provider for one benchmark case."""
    runners = {
        "trawl": run_trawl_provider,
        "jina": run_jina_provider,
        "trafilatura": run_trafilatura_provider,
        "firecrawl": run_firecrawl_provider,
        "crawl4ai": run_crawl4ai_provider,
    }
    return runners[provider](case)


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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--only", help="Run one case id")
    parser.add_argument("--limit", type=int, help="Limit selected cases")
    parser.add_argument(
        "--provider",
        action="append",
        choices=PROVIDER_CHOICES,
        help="Provider to run. May be repeated. Defaults to trawl, jina, trafilatura.",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args(argv)


def timestamped_output_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return DEFAULT_RESULTS_ROOT / stamp


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = select_cases(load_cases(args.cases), only=args.only, limit=args.limit)
    providers = args.provider or DEFAULT_PROVIDERS
    output_dir = args.output_dir or timestamped_output_dir()

    results: list[dict[str, Any]] = []
    for case in cases:
        for provider in providers:
            print(f"[{provider}] {case['id']}", file=sys.stderr, flush=True)
            results.append(run_provider(case, provider))

    write_outputs(output_dir, results)
    print(f"Wrote reader comparison results to {output_dir}", file=sys.stderr)

    active = [result for result in results if result["status"] != "skipped"]
    return 0 if active else 1


if __name__ == "__main__":
    raise SystemExit(main())
