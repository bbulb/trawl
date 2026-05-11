"""Query-based reader comparison benchmark."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
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
FIRECRAWL_SCRAPE_URL = os.environ.get("FIRECRAWL_SCRAPE_URL", "https://api.firecrawl.dev/v2/scrape")
FIRECRAWL_TIMEOUT = float(os.environ.get("FIRECRAWL_TIMEOUT", "60"))
DEFAULT_PROVIDERS = ["trawl", "jina", "trafilatura"]
PROVIDER_CHOICES = ["trawl", "jina", "trafilatura", "firecrawl", "crawl4ai"]
RETRIEVAL_MODE_CHOICES = ["dense", "hybrid", "contextual-auto", "contextual-forced"]
RETRIEVAL_MODE_ENV = {
    "dense": {"TRAWL_HYBRID_RETRIEVAL": "0", "TRAWL_CONTEXTUAL_RETRIEVAL": "0"},
    "hybrid": {"TRAWL_HYBRID_RETRIEVAL": "1", "TRAWL_CONTEXTUAL_RETRIEVAL": "0"},
    "contextual-auto": {"TRAWL_HYBRID_RETRIEVAL": "0", "TRAWL_CONTEXTUAL_RETRIEVAL": "auto"},
    "contextual-forced": {"TRAWL_HYBRID_RETRIEVAL": "0", "TRAWL_CONTEXTUAL_RETRIEVAL": "1"},
}

REQUIRED_CASE_FIELDS = {"id", "category", "url", "query", "expected_facts", "failure_class"}
RESULT_FIELDS = [
    "case_id",
    "category",
    "provider",
    "status",
    "latency_ms",
    "repeat_index",
    "cache_phase",
    "retrieval_mode_requested",
    "retrieval_mode_observed",
    "retrieval_query_type",
    "contextual_retrieval_used",
    "retrieval_ms",
    "cache_hit",
    "embed_cache_ttl",
    "tokens_returned",
    "n_chunks_total",
    "n_chunks_embedded",
    "embed_cache_hits",
    "embed_cache_misses",
    "rank1_identity",
    "first_fact_rank",
    "rank_movement",
    "flipped_to_fail",
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


def expand_repeated_cases(cases: list[dict[str, Any]], *, repeats: int) -> list[dict[str, Any]]:
    """Duplicate selected cases and label cold/warm repeat phases."""
    expanded: list[dict[str, Any]] = []
    for case in cases:
        for repeat_index in range(max(repeats, 1)):
            item = dict(case)
            item["_repeat_index"] = repeat_index
            item["_cache_phase"] = "cold" if repeat_index == 0 else "warm"
            expanded.append(item)
    return expanded


def expand_retrieval_mode_cases(
    cases: list[dict[str, Any]], *, modes: list[str]
) -> list[dict[str, Any]]:
    """Duplicate cases for each requested trawl retrieval mode."""
    if not modes:
        return [dict(case) for case in cases]
    expanded: list[dict[str, Any]] = []
    for case in cases:
        for mode in modes:
            item = dict(case)
            item["_retrieval_mode"] = mode
            expanded.append(item)
    return expanded


def iter_provider_cases(
    case: dict[str, Any],
    provider: str,
    retrieval_modes: list[str],
):
    """Yield cases for one provider, expanding retrieval modes for trawl only."""
    if provider != "trawl" or not retrieval_modes:
        yield dict(case)
        return
    yield from expand_retrieval_mode_cases([case], modes=retrieval_modes)


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
        "first_fact_rank": first_rank,
        "fact_ranks": found,
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


def rank1_identity(ranked_texts: list[str]) -> str | None:
    """Return a stable non-content identity for the top returned text."""
    if not ranked_texts:
        return None
    normalized = " ".join(ranked_texts[0].split())
    if not normalized:
        return None
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


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
        "repeat_index": case.get("_repeat_index", 0),
        "cache_phase": case.get("_cache_phase", "cold"),
        "retrieval_mode_requested": case.get("_retrieval_mode"),
        "retrieval_mode_observed": None,
        "retrieval_query_type": None,
        "contextual_retrieval_used": None,
        "retrieval_ms": None,
        "cache_hit": None,
        "embed_cache_ttl": os.environ.get("TRAWL_EMBED_CACHE_TTL", "0"),
        "tokens_returned": estimate_tokens(text),
        "n_chunks_total": n_chunks_total,
        "n_chunks_embedded": n_chunks_total,
        "embed_cache_hits": None,
        "embed_cache_misses": None,
        "rank1_identity": rank1_identity(ranked_texts),
        "first_fact_rank": score["first_fact_rank"],
        "rank_movement": None,
        "flipped_to_fail": False,
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
        "repeat_index": case.get("_repeat_index", 0),
        "cache_phase": case.get("_cache_phase", "cold"),
        "retrieval_mode_requested": case.get("_retrieval_mode"),
        "retrieval_mode_observed": None,
        "retrieval_query_type": None,
        "contextual_retrieval_used": None,
        "retrieval_ms": None,
        "cache_hit": None,
        "embed_cache_ttl": os.environ.get("TRAWL_EMBED_CACHE_TTL", "0"),
        "tokens_returned": 0,
        "n_chunks_total": None,
        "n_chunks_embedded": None,
        "embed_cache_hits": None,
        "embed_cache_misses": None,
        "rank1_identity": None,
        "first_fact_rank": None,
        "rank_movement": None,
        "flipped_to_fail": False,
        "recall_at_k": 0.0,
        "mrr_at_k": 0.0,
        "answer_grounding_hit": False,
        "failure_phase": "not_configured",
        "missing_facts": [fact["id"] for fact in case.get("expected_facts", [])],
        "error": reason,
    }


def _fetch_relevant_for_trawl(url: str, query: str, *, use_rerank: bool):
    from trawl import fetch_relevant

    return fetch_relevant(url, query, use_rerank=use_rerank)


def _to_dict_for_trawl(result) -> dict[str, Any]:
    from trawl import to_dict

    return to_dict(result)


@contextmanager
def temporary_env(values: dict[str, str]):
    """Temporarily override environment variables for one provider call."""
    old_values: dict[str, str | None] = {}
    for key, value in values.items():
        old_values[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _retrieval_mode_env(mode: str | None) -> dict[str, str]:
    if not mode:
        return {}
    return RETRIEVAL_MODE_ENV.get(mode, {})


def _observed_retrieval_mode(payload: dict[str, Any], requested: str | None) -> str | None:
    diagnostics = payload.get("retrieval_diagnostics") or {}
    if isinstance(diagnostics, dict) and diagnostics.get("mode"):
        return str(diagnostics["mode"])
    if requested == "hybrid":
        return "hybrid"
    if requested in {"dense", "contextual-auto", "contextual-forced"}:
        return "dense"
    return None


def _retrieval_query_type(payload: dict[str, Any]) -> str | None:
    diagnostics = payload.get("retrieval_diagnostics") or {}
    if isinstance(diagnostics, dict) and diagnostics.get("query_type"):
        return str(diagnostics["query_type"])
    return None


def run_trawl_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Run trawl selective retrieval for a case."""
    started = time.monotonic()
    requested_mode = case.get("_retrieval_mode")
    try:
        with temporary_env(_retrieval_mode_env(requested_mode)):
            result = _fetch_relevant_for_trawl(case["url"], case["query"], use_rerank=True)
        elapsed = int((time.monotonic() - started) * 1000)
        payload = _to_dict_for_trawl(result)
        chunks = payload.get("chunks") or []
        ranked_texts = [
            "\n".join(part for part in (chunk.get("heading"), chunk.get("text")) if part)
            for chunk in chunks
        ]
        scored = build_scored_result(
            case=case,
            provider="trawl",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=ranked_texts,
            n_chunks_total=payload.get("n_chunks_total"),
            error=payload.get("error"),
        )
        scored.update(
            {
                "retrieval_mode_requested": requested_mode,
                "retrieval_mode_observed": _observed_retrieval_mode(payload, requested_mode),
                "retrieval_query_type": _retrieval_query_type(payload),
                "contextual_retrieval_used": bool(payload.get("contextual_retrieval_used")),
                "retrieval_ms": payload.get("retrieval_ms"),
                "cache_hit": payload.get("cache_hit"),
                "embed_cache_ttl": os.environ.get("TRAWL_EMBED_CACHE_TTL", "0"),
                "n_chunks_embedded": payload.get("n_chunks_embedded"),
                "embed_cache_hits": payload.get("embed_cache_hits"),
                "embed_cache_misses": payload.get("embed_cache_misses"),
            }
        )
        return scored
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
    """Run Firecrawl scrape as an optional full-page markdown baseline."""
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        return build_skip_result(case, "firecrawl", "FIRECRAWL_API_KEY not set")

    started = time.monotonic()
    try:
        response = httpx.post(
            FIRECRAWL_SCRAPE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"url": case["url"], "formats": ["markdown"]},
            timeout=FIRECRAWL_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        markdown = ""
        if isinstance(data, dict):
            markdown = str(data.get("markdown") or "")
        elif isinstance(body, dict):
            markdown = str(body.get("markdown") or "")
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="firecrawl",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=[markdown] if markdown else [],
            n_chunks_total=None,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - network provider behavior
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="firecrawl",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )


def _load_crawl4ai():
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
    except ImportError:
        return None
    return {
        "AsyncWebCrawler": AsyncWebCrawler,
        "BrowserConfig": BrowserConfig,
        "CacheMode": CacheMode,
        "CrawlerRunConfig": CrawlerRunConfig,
    }


async def _run_crawl4ai_async(case: dict[str, Any], api: dict[str, Any]) -> str:
    browser_config = api["BrowserConfig"](headless=True)
    run_config = api["CrawlerRunConfig"](cache_mode=api["CacheMode"].BYPASS)
    async with api["AsyncWebCrawler"](config=browser_config) as crawler:
        result = await crawler.arun(url=case["url"], config=run_config)
    markdown = getattr(result, "markdown", "") or ""
    if hasattr(markdown, "fit_markdown"):
        markdown = markdown.fit_markdown or getattr(markdown, "raw_markdown", "")
    return str(markdown or "")


def run_crawl4ai_provider(case: dict[str, Any]) -> dict[str, Any]:
    """Run Crawl4AI as an optional local markdown baseline."""
    api = _load_crawl4ai()
    if api is None:
        return build_skip_result(case, "crawl4ai", "crawl4ai is not installed")

    started = time.monotonic()
    try:
        markdown = asyncio.run(_run_crawl4ai_async(case, api))
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="crawl4ai",
            status="ok",
            latency_ms=elapsed,
            ranked_texts=[markdown] if markdown else [],
            n_chunks_total=None,
            error=None,
        )
    except Exception as exc:  # pragma: no cover - optional provider behavior
        elapsed = int((time.monotonic() - started) * 1000)
        return build_scored_result(
            case=case,
            provider="crawl4ai",
            status="error",
            latency_ms=elapsed,
            ranked_texts=[],
            n_chunks_total=None,
            error=f"{type(exc).__name__}: {exc}",
        )


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


def _passes(row: dict[str, Any]) -> bool:
    return row.get("status") == "ok" and bool(row.get("answer_grounding_hit"))


def _baseline_key(row: dict[str, Any]) -> tuple[Any, Any, Any, str]:
    return (
        row.get("case_id"),
        row.get("repeat_index", 0),
        row.get("cache_phase", "cold"),
        str(row.get("embed_cache_ttl", "0")),
    )


def annotate_retrieval_mode_comparisons(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate trawl retrieval-mode rows against dense baseline rows."""
    annotated = [dict(result) for result in results]
    dense_baselines = {
        _baseline_key(row): row
        for row in annotated
        if row.get("provider") == "trawl" and row.get("retrieval_mode_requested") == "dense"
    }
    for row in annotated:
        row.setdefault("flipped_to_fail", False)
        row.setdefault("rank_movement", None)
        if row.get("provider") != "trawl" or not row.get("retrieval_mode_requested"):
            continue
        baseline = dense_baselines.get(_baseline_key(row))
        if baseline is None:
            continue
        if row.get("retrieval_mode_requested") == "dense":
            row["flipped_to_fail"] = False
            row["rank_movement"] = 0 if baseline.get("first_fact_rank") is not None else None
            continue
        row["flipped_to_fail"] = _passes(baseline) and not _passes(row)
        baseline_rank = baseline.get("first_fact_rank")
        row_rank = row.get("first_fact_rank")
        if isinstance(baseline_rank, int) and isinstance(row_rank, int):
            row["rank_movement"] = row_rank - baseline_rank
    return annotated


def write_outputs(output_dir: Path, results: list[dict[str, Any]]) -> None:
    """Write raw JSONL, compact CSV, and a Markdown benchmark report."""
    results = annotate_retrieval_mode_comparisons(results)
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
    results = annotate_retrieval_mode_comparisons(results)
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
        mode_rows = [
            result
            for result in active
            if result.get("provider") == "trawl" and result.get("retrieval_mode_requested")
        ]
        if mode_rows:
            lines.extend(
                [
                    "",
                    "## Retrieval mode summary",
                    "",
                    "| Mode | Query type | Cache TTL | Rows | Pass rate | Flipped-to-fail | "
                    "Avg rank movement | Retrieval p50 ms | Retrieval p95 ms | Avg tokens |",
                    "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
                ]
            )
            groups = sorted(
                {
                    (
                        result.get("retrieval_mode_requested", ""),
                        str(result.get("retrieval_query_type") or "unknown"),
                        str(result.get("embed_cache_ttl", "0")),
                    )
                    for result in mode_rows
                },
                key=lambda item: (
                    RETRIEVAL_MODE_CHOICES.index(item[0])
                    if item[0] in RETRIEVAL_MODE_CHOICES
                    else len(RETRIEVAL_MODE_CHOICES),
                    item[1],
                    item[2],
                ),
            )
            for mode, query_type, cache_ttl in groups:
                rows = [
                    result
                    for result in mode_rows
                    if result.get("retrieval_mode_requested") == mode
                    and str(result.get("retrieval_query_type") or "unknown") == query_type
                    and str(result.get("embed_cache_ttl", "0")) == cache_ttl
                ]
                ok_rows = [result for result in rows if result["status"] == "ok"]
                pass_rate = (len(ok_rows) / len(rows)) if rows else 0.0
                flipped = sum(1 for result in rows if result.get("flipped_to_fail") is True)
                lines.append(
                    f"| {mode} | {query_type} | {cache_ttl} | {len(rows)} | "
                    f"{pass_rate:.2f} | "
                    f"{flipped} | {_format_average(rows, 'rank_movement')} | "
                    f"{_format_percentile(rows, 'retrieval_ms', 50)} | "
                    f"{_format_percentile(rows, 'retrieval_ms', 95)} | "
                    f"{_format_average(rows, 'tokens_returned')} |"
                )

        warm_rows = [result for result in active if result.get("cache_phase") == "warm"]
        if warm_rows:
            lines.extend(
                [
                    "",
                    "## Warm repeat summary",
                    "",
                    "| Provider | Phase | Rows | Avg retrieval ms | "
                    "Avg embed cache hits | Avg embed cache misses |",
                    "|---|---|---:|---:|---:|---:|",
                ]
            )
            phases = sorted({result.get("cache_phase", "cold") for result in active})
            for provider in by_provider:
                for phase in phases:
                    rows = [
                        result
                        for result in active
                        if result["provider"] == provider
                        and result.get("cache_phase", "cold") == phase
                    ]
                    if not rows:
                        continue
                    lines.append(
                        f"| {provider} | {phase} | {len(rows)} | "
                        f"{_format_average(rows, 'retrieval_ms')} | "
                        f"{_format_average(rows, 'embed_cache_hits')} | "
                        f"{_format_average(rows, 'embed_cache_misses')} |"
                    )
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


def _percentile(values: list[int | float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (percentile / 100.0) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _format_average(rows: list[dict[str, Any]], field: str) -> str:
    values = [
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float)) and not isinstance(row.get(field), bool)
    ]
    return f"{_average(values):.1f}" if values else ""


def _format_percentile(rows: list[dict[str, Any]], field: str, percentile: float) -> str:
    values = [
        float(row[field])
        for row in rows
        if isinstance(row.get(field), (int, float)) and not isinstance(row.get(field), bool)
    ]
    return f"{_percentile(values, percentile):.1f}" if values else ""


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
    parser.add_argument("--repeat", type=int, default=1, help="Repeat each case N times.")
    parser.add_argument(
        "--warm-repeat-embed-cache-ttl",
        type=int,
        default=None,
        help="Set TRAWL_EMBED_CACHE_TTL while running repeated trawl cases.",
    )
    parser.add_argument(
        "--retrieval-mode",
        action="append",
        choices=RETRIEVAL_MODE_CHOICES,
        help=(
            "Run trawl under a named retrieval mode. May be repeated; "
            "non-trawl providers are not expanded."
        ),
    )
    return parser.parse_args(argv)


def timestamped_output_dir() -> Path:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return DEFAULT_RESULTS_ROOT / stamp


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selected_cases = select_cases(load_cases(args.cases), only=args.only, limit=args.limit)
    cases = expand_repeated_cases(selected_cases, repeats=args.repeat)
    providers = args.provider or DEFAULT_PROVIDERS
    retrieval_modes = args.retrieval_mode or []
    output_dir = args.output_dir or timestamped_output_dir()

    results: list[dict[str, Any]] = []
    old_embed_cache_ttl = os.environ.get("TRAWL_EMBED_CACHE_TTL")
    if args.warm_repeat_embed_cache_ttl is not None:
        os.environ["TRAWL_EMBED_CACHE_TTL"] = str(args.warm_repeat_embed_cache_ttl)
    try:
        for case in cases:
            repeat_index = case.get("_repeat_index", 0)
            for provider in providers:
                for provider_case in iter_provider_cases(case, provider, retrieval_modes):
                    mode = provider_case.get("_retrieval_mode")
                    mode_suffix = f" mode={mode}" if mode else ""
                    print(
                        f"[{provider}] {case['id']} repeat={repeat_index}{mode_suffix}",
                        file=sys.stderr,
                        flush=True,
                    )
                    results.append(run_provider(provider_case, provider))

        write_outputs(output_dir, results)
    finally:
        if args.warm_repeat_embed_cache_ttl is not None:
            if old_embed_cache_ttl is None:
                os.environ.pop("TRAWL_EMBED_CACHE_TTL", None)
            else:
                os.environ["TRAWL_EMBED_CACHE_TTL"] = old_embed_cache_ttl

    print(f"Wrote reader comparison results to {output_dir}", file=sys.stderr)

    active = [result for result in results if result["status"] != "skipped"]
    return 0 if active else 1


if __name__ == "__main__":
    raise SystemExit(main())
