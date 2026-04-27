from __future__ import annotations

from pathlib import Path
import json

import pytest

from benchmarks import reader_comparison as rc


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
