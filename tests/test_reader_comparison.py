from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import reader_comparison as rc


def _case() -> dict:
    return {
        "id": "alpha",
        "category": "docs",
        "url": "https://example.test",
        "query": "alpha",
        "expected_facts": [{"id": "alpha", "any_of": ["alpha fact"]}],
        "failure_class": {
            "on_empty_output": "extraction",
            "on_fetch_error": "provider_error",
            "on_missing_facts": "retrieval",
        },
    }


def test_score_ranked_texts_computes_recall_mrr_and_missing_facts():
    facts = [
        {"id": "fetch_call", "any_of": ["fetch(", "fetch ("]},
        {"id": "post_method", "all_of": ["method", "POST"]},
        {"id": "json_body", "pattern": r"JSON\.stringify"},
    ]
    ranked = [
        "Use fetch(url, { method: 'GET' })",
        "Use method: 'POST' with JSON.stringify(payload)",
    ]

    score = rc.score_ranked_texts(ranked, facts)

    assert score["recall_at_k"] == 1.0
    assert score["mrr_at_k"] == 1.0
    assert score["answer_grounding_hit"] is True
    assert score["missing_facts"] == []


def test_score_ranked_texts_reports_missing_facts():
    facts = [
        {"id": "fetch_call", "any_of": ["fetch(", "fetch ("]},
        {"id": "post_method", "all_of": ["method", "POST"]},
    ]

    score = rc.score_ranked_texts(["Use fetch(url)"], facts)

    assert score["recall_at_k"] == 0.5
    assert score["mrr_at_k"] == 1.0
    assert score["answer_grounding_hit"] is False
    assert score["missing_facts"] == ["post_method"]


def test_load_cases_rejects_missing_expected_facts(tmp_path: Path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        """
cases:
  - id: bad
    category: docs
    url: https://example.test
    query: example
    failure_class:
      on_missing_facts: retrieval
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected_facts"):
        rc.load_cases(path)


def test_load_cases_rejects_missing_failure_class(tmp_path: Path):
    path = tmp_path / "cases.yaml"
    path.write_text(
        """
cases:
  - id: bad
    category: docs
    url: https://example.test
    query: example
    expected_facts:
      - id: example
        any_of: ["example"]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failure_class"):
        rc.load_cases(path)


def test_build_result_classifies_missing_facts_as_retrieval_failure():
    case = {
        "id": "mdn_fetch_post",
        "category": "docs",
        "url": "https://example.test",
        "query": "fetch post",
        "expected_facts": [{"id": "post", "any_of": ["POST"]}],
        "failure_class": {
            "on_empty_output": "extraction",
            "on_missing_facts": "retrieval",
        },
    }

    result = rc.build_scored_result(
        case=case,
        provider="example",
        status="ok",
        latency_ms=12,
        ranked_texts=["GET example"],
        n_chunks_total=None,
        error=None,
    )

    assert result["status"] == "fail"
    assert result["failure_phase"] == "retrieval"
    assert result["tokens_returned"] > 0
    assert result["missing_facts"] == ["post"]


def test_build_result_classifies_empty_output_as_extraction_failure():
    case = {
        "id": "empty",
        "category": "docs",
        "url": "https://example.test",
        "query": "example",
        "expected_facts": [{"id": "example", "any_of": ["example"]}],
        "failure_class": {
            "on_empty_output": "extraction",
            "on_missing_facts": "retrieval",
        },
    }

    result = rc.build_scored_result(
        case=case,
        provider="example",
        status="ok",
        latency_ms=12,
        ranked_texts=[],
        n_chunks_total=None,
        error=None,
    )

    assert result["status"] == "fail"
    assert result["failure_phase"] == "extraction"
    assert result["tokens_returned"] == 0


def test_build_skip_result_marks_optional_provider_not_configured():
    case = {
        "id": "mdn_fetch_post",
        "category": "docs",
        "url": "https://example.test",
        "query": "fetch post",
        "expected_facts": [{"id": "post", "any_of": ["POST"]}],
        "failure_class": {"on_missing_facts": "retrieval"},
    }

    result = rc.build_skip_result(case, "firecrawl", "FIRECRAWL_API_KEY not set")

    assert result["case_id"] == "mdn_fetch_post"
    assert result["provider"] == "firecrawl"
    assert result["status"] == "skipped"
    assert result["failure_phase"] == "not_configured"
    assert result["error"] == "FIRECRAWL_API_KEY not set"


def test_write_outputs_creates_jsonl_csv_and_markdown(tmp_path: Path):
    results = [
        {
            "case_id": "mdn_fetch_post",
            "category": "docs",
            "provider": "trawl",
            "status": "ok",
            "latency_ms": 25,
            "tokens_returned": 40,
            "n_chunks_total": 3,
            "recall_at_k": 1.0,
            "mrr_at_k": 1.0,
            "answer_grounding_hit": True,
            "failure_phase": None,
            "missing_facts": [],
            "error": None,
        },
        {
            "case_id": "mdn_fetch_post",
            "category": "docs",
            "provider": "firecrawl",
            "status": "skipped",
            "latency_ms": 0,
            "tokens_returned": 0,
            "n_chunks_total": None,
            "recall_at_k": 0.0,
            "mrr_at_k": 0.0,
            "answer_grounding_hit": False,
            "failure_phase": "not_configured",
            "missing_facts": ["post"],
            "error": "FIRECRAWL_API_KEY not set",
        },
    ]

    rc.write_outputs(tmp_path, results)

    jsonl = tmp_path / "results.jsonl"
    csv = tmp_path / "summary.csv"
    report = tmp_path / "report.md"
    assert jsonl.exists()
    assert csv.exists()
    assert report.exists()

    first = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert first["tokens_returned"] == 40
    csv_text = csv.read_text(encoding="utf-8")
    assert "tokens_returned" in csv_text
    assert "latency_ms" in csv_text
    assert "recall_at_k" in csv_text
    assert "mrr_at_k" in csv_text
    assert "answer_grounding_hit" in csv_text
    assert "failure_phase" in csv_text
    report_text = report.read_text(encoding="utf-8")
    assert "Recall@k" in report_text
    assert "MRR@k" in report_text
    assert "answer_grounding_hit" in report_text


def test_select_cases_filters_only_and_limit():
    cases = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

    assert rc.select_cases(cases, only="b", limit=None) == [{"id": "b"}]
    assert rc.select_cases(cases, only=None, limit=2) == [{"id": "a"}, {"id": "b"}]


def test_default_case_manifest_loads_and_classifies_failures():
    cases = rc.load_cases(rc.DEFAULT_CASES_FILE)

    assert len(cases) >= 5
    assert all(case["expected_facts"] for case in cases)
    assert all(case["failure_class"] for case in cases)


def test_expand_repeated_cases_marks_repeat_index():
    cases = [_case()]

    expanded = rc.expand_repeated_cases(cases, repeats=2)

    assert [case["_repeat_index"] for case in expanded] == [0, 1]
    assert [case["_cache_phase"] for case in expanded] == ["cold", "warm"]


def test_expand_retrieval_mode_cases_marks_requested_modes():
    cases = [_case()]

    expanded = rc.expand_retrieval_mode_cases(
        cases,
        modes=["dense", "hybrid", "contextual-auto", "contextual-forced"],
    )

    assert [case["_retrieval_mode"] for case in expanded] == [
        "dense",
        "hybrid",
        "contextual-auto",
        "contextual-forced",
    ]


def test_iter_provider_cases_only_expands_trawl_modes():
    case = _case()

    trawl_cases = list(rc.iter_provider_cases(case, "trawl", ["dense", "hybrid"]))
    jina_cases = list(rc.iter_provider_cases(case, "jina", ["dense", "hybrid"]))

    assert [item["_retrieval_mode"] for item in trawl_cases] == ["dense", "hybrid"]
    assert len(jina_cases) == 1
    assert "_retrieval_mode" not in jina_cases[0]


def test_trawl_provider_includes_embedding_cache_metadata(monkeypatch):
    case = _case()
    case["_repeat_index"] = 1
    case["_cache_phase"] = "warm"

    result_obj = SimpleNamespace()

    def fake_fetch_relevant(url, query, *, use_rerank):
        assert url == "https://example.test"
        assert query == "alpha"
        assert use_rerank is True
        return result_obj

    def fake_to_dict(result):
        assert result is result_obj
        return {
            "chunks": [{"heading": "", "text": "alpha fact"}],
            "error": None,
            "retrieval_ms": 11,
            "cache_hit": False,
            "n_chunks_total": 1,
            "n_chunks_embedded": 1,
            "embed_cache_hits": 2,
            "embed_cache_misses": 3,
        }

    monkeypatch.setattr(rc, "_fetch_relevant_for_trawl", fake_fetch_relevant)
    monkeypatch.setattr(rc, "_to_dict_for_trawl", fake_to_dict)

    result = rc.run_trawl_provider(case)

    assert result["retrieval_ms"] == 11
    assert result["cache_hit"] is False
    assert result["n_chunks_embedded"] == 1
    assert result["embed_cache_hits"] == 2
    assert result["embed_cache_misses"] == 3
    assert result["repeat_index"] == 1
    assert result["cache_phase"] == "warm"


@pytest.mark.parametrize(
    ("mode", "expected_hybrid", "expected_contextual", "observed_mode"),
    [
        ("dense", "0", "0", "dense"),
        ("hybrid", "1", "0", "hybrid"),
        ("contextual-auto", "0", "auto", "dense"),
        ("contextual-forced", "0", "1", "dense"),
    ],
)
def test_trawl_provider_applies_retrieval_mode_env_and_restores(
    monkeypatch,
    mode,
    expected_hybrid,
    expected_contextual,
    observed_mode,
):
    monkeypatch.setenv("TRAWL_HYBRID_RETRIEVAL", "ambient-hybrid")
    monkeypatch.setenv("TRAWL_CONTEXTUAL_RETRIEVAL", "ambient-context")
    monkeypatch.setenv("TRAWL_EMBED_CACHE_TTL", "86400")
    case = _case()
    case["_retrieval_mode"] = mode
    result_obj = SimpleNamespace()

    def fake_fetch_relevant(_url, _query, *, use_rerank):
        assert use_rerank is True
        assert rc.os.environ["TRAWL_HYBRID_RETRIEVAL"] == expected_hybrid
        assert rc.os.environ["TRAWL_CONTEXTUAL_RETRIEVAL"] == expected_contextual
        return result_obj

    def fake_to_dict(result):
        assert result is result_obj
        return {
            "chunks": [{"heading": "Heading", "text": "alpha fact"}],
            "error": None,
            "retrieval_ms": 7,
            "n_chunks_total": 1,
            "n_chunks_embedded": 1,
            "retrieval_diagnostics": {"mode": observed_mode, "query_type": "identifier"},
            "contextual_retrieval_used": expected_contextual != "0",
        }

    monkeypatch.setattr(rc, "_fetch_relevant_for_trawl", fake_fetch_relevant)
    monkeypatch.setattr(rc, "_to_dict_for_trawl", fake_to_dict)

    result = rc.run_trawl_provider(case)

    assert result["retrieval_mode_requested"] == mode
    assert result["retrieval_mode_observed"] == observed_mode
    assert result["retrieval_query_type"] == "identifier"
    assert result["contextual_retrieval_used"] is (expected_contextual != "0")
    assert result["embed_cache_ttl"] == "86400"
    assert rc.os.environ["TRAWL_HYBRID_RETRIEVAL"] == "ambient-hybrid"
    assert rc.os.environ["TRAWL_CONTEXTUAL_RETRIEVAL"] == "ambient-context"


def test_annotate_retrieval_mode_comparisons_records_flips_and_rank_movement():
    results = [
        {
            "case_id": "alpha",
            "provider": "trawl",
            "repeat_index": 0,
            "cache_phase": "cold",
            "retrieval_mode_requested": "dense",
            "status": "ok",
            "answer_grounding_hit": True,
            "first_fact_rank": 1,
        },
        {
            "case_id": "alpha",
            "provider": "trawl",
            "repeat_index": 0,
            "cache_phase": "cold",
            "retrieval_mode_requested": "hybrid",
            "status": "fail",
            "answer_grounding_hit": False,
            "first_fact_rank": None,
        },
        {
            "case_id": "alpha",
            "provider": "trawl",
            "repeat_index": 0,
            "cache_phase": "cold",
            "retrieval_mode_requested": "contextual-auto",
            "status": "ok",
            "answer_grounding_hit": True,
            "first_fact_rank": 3,
        },
    ]

    annotated = rc.annotate_retrieval_mode_comparisons(results)
    by_mode = {row["retrieval_mode_requested"]: row for row in annotated}

    assert by_mode["dense"]["flipped_to_fail"] is False
    assert by_mode["dense"]["rank_movement"] == 0
    assert by_mode["hybrid"]["flipped_to_fail"] is True
    assert by_mode["hybrid"]["rank_movement"] is None
    assert by_mode["contextual-auto"]["flipped_to_fail"] is False
    assert by_mode["contextual-auto"]["rank_movement"] == 2


def test_render_report_includes_retrieval_mode_summary():
    rows = [
        {
            "case_id": "a",
            "category": "docs",
            "provider": "trawl",
            "status": "ok",
            "latency_ms": 100,
            "repeat_index": 0,
            "cache_phase": "cold",
            "retrieval_mode_requested": "dense",
            "retrieval_query_type": "identifier",
            "retrieval_ms": 10,
            "embed_cache_ttl": "86400",
            "tokens_returned": 20,
            "answer_grounding_hit": True,
            "first_fact_rank": 1,
            "flipped_to_fail": False,
            "rank_movement": 0,
        },
        {
            "case_id": "a",
            "category": "docs",
            "provider": "trawl",
            "status": "fail",
            "latency_ms": 130,
            "repeat_index": 0,
            "cache_phase": "cold",
            "retrieval_mode_requested": "hybrid",
            "retrieval_query_type": "identifier",
            "retrieval_ms": 20,
            "embed_cache_ttl": "86400",
            "tokens_returned": 30,
            "answer_grounding_hit": False,
            "first_fact_rank": None,
            "flipped_to_fail": True,
            "rank_movement": None,
        },
    ]

    report = rc.render_report(rows)

    assert "## Retrieval mode summary" in report
    assert "Query type" in report
    assert "Retrieval p50 ms" in report
    assert "Retrieval p95 ms" in report
    assert "Flipped-to-fail" in report
    assert "| hybrid | identifier | 86400 | 1 | 0.00 | 1 |" in report


def test_firecrawl_provider_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    result = rc.run_firecrawl_provider(_case())

    assert result["status"] == "skipped"
    assert result["failure_phase"] == "not_configured"


def test_firecrawl_provider_uses_markdown_response(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"success": True, "data": {"markdown": "alpha fact"}}

    def fake_post(url, *, headers, json, timeout):
        assert url == "https://api.firecrawl.dev/v2/scrape"
        assert headers["Authorization"] == "Bearer fc-test"
        assert json == {"url": "https://example.test", "formats": ["markdown"]}
        return Response()

    monkeypatch.setattr(rc.httpx, "post", fake_post)

    result = rc.run_firecrawl_provider(_case())

    assert result["provider"] == "firecrawl"
    assert result["status"] == "ok"
    assert result["answer_grounding_hit"] is True


def test_crawl4ai_provider_skips_when_package_missing(monkeypatch):
    monkeypatch.setattr(rc, "_load_crawl4ai", lambda: None)

    result = rc.run_crawl4ai_provider(_case())

    assert result["status"] == "skipped"
    assert result["failure_phase"] == "not_configured"


def test_crawl4ai_provider_uses_markdown_response(monkeypatch):
    class BrowserConfig:
        def __init__(self, *, headless):
            assert headless is True

    class CrawlerRunConfig:
        def __init__(self, *, cache_mode):
            assert cache_mode == "bypass"

    class AsyncWebCrawler:
        def __init__(self, *, config):
            assert isinstance(config, BrowserConfig)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def arun(self, *, url, config):
            assert url == "https://example.test"
            assert isinstance(config, CrawlerRunConfig)
            return SimpleNamespace(markdown="alpha fact")

    monkeypatch.setattr(
        rc,
        "_load_crawl4ai",
        lambda: {
            "AsyncWebCrawler": AsyncWebCrawler,
            "BrowserConfig": BrowserConfig,
            "CacheMode": SimpleNamespace(BYPASS="bypass"),
            "CrawlerRunConfig": CrawlerRunConfig,
        },
    )

    result = rc.run_crawl4ai_provider(_case())

    assert result["provider"] == "crawl4ai"
    assert result["status"] == "ok"
    assert result["answer_grounding_hit"] is True
