"""Unit tests for the WCXB runner's single-page evaluation path."""

from pathlib import Path

from benchmarks.wcxb.run import evaluate_page

FIXTURES = Path(__file__).parent / "fixtures" / "wcxb"


def test_evaluate_page_article_returns_expected_shape():
    result = evaluate_page(FIXTURES, "article_sample")

    assert result["id"] == "article_sample"
    assert result["page_type"] == "article"
    assert result["url"] == "https://example.test/article_sample"

    trawl = result["trawl"]
    assert isinstance(trawl["f1"], float)
    assert 0.0 <= trawl["f1"] <= 1.0
    assert isinstance(trawl["precision"], float)
    assert isinstance(trawl["recall"], float)
    assert trawl["time_ms"] >= 0
    assert isinstance(trawl["output_len"], int)
    assert trawl["error"] is None


def test_evaluate_page_article_trawl_f1_is_substantial():
    result = evaluate_page(FIXTURES, "article_sample")
    assert result["trawl"]["f1"] > 0.5


def test_evaluate_page_empty_body_f1_is_zero_no_error():
    result = evaluate_page(FIXTURES, "empty_sample")
    assert result["trawl"]["error"] is None
    assert result["trawl"]["f1"] == 0.0


def test_evaluate_page_missing_files_raises():
    import pytest
    with pytest.raises(FileNotFoundError):
        evaluate_page(FIXTURES, "does_not_exist")
