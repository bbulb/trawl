from __future__ import annotations

from pathlib import Path

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

