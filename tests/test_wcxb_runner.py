"""Unit tests for the WCXB runner's single-page evaluation path."""

import json as _json
from pathlib import Path

from benchmarks.wcxb.run import (
    evaluate_page,
    evaluate_page_with_baseline,
    run_all,
)

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


def test_evaluate_page_with_baseline_has_both_extractors():
    result = evaluate_page_with_baseline(FIXTURES, "article_sample")
    assert "trawl" in result
    assert "trafilatura" in result

    traf = result["trafilatura"]
    assert isinstance(traf["f1"], float)
    assert traf["time_ms"] >= 0
    assert isinstance(traf["output_len"], int)
    assert traf["error"] is None


def test_evaluate_page_with_baseline_snippet_counts_present():
    result = evaluate_page_with_baseline(FIXTURES, "article_sample")
    w = result["with_snippets_hit"]
    wo = result["without_snippets_hit"]
    # article_sample fixture has 2 with-snippets and 2 without-snippets
    assert w["total"] == 2
    assert wo["total"] == 2
    assert 0 <= w["trawl"] <= w["total"]
    assert 0 <= w["trafilatura"] <= w["total"]
    assert 0 <= wo["trawl"] <= wo["total"]
    assert 0 <= wo["trafilatura"] <= wo["total"]


def test_run_all_writes_raw_and_report(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    exit_code = run_all(
        data_dir=FIXTURES,
        out_dir=out_dir,
        limit=None,
        type_filter=None,
        no_baseline=False,
    )
    assert exit_code == 0

    raw = _json.loads((out_dir / "raw.json").read_text())
    assert isinstance(raw, list)
    # fixture has 3 samples — all should be processed
    assert {e["id"] for e in raw} == {"article_sample", "product_sample", "empty_sample"}

    report = (out_dir / "report.md").read_text()
    assert "# WCXB" in report
    assert "## Overall" in report


def test_run_all_respects_limit(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(data_dir=FIXTURES, out_dir=out_dir, limit=1, type_filter=None, no_baseline=False)
    raw = _json.loads((out_dir / "raw.json").read_text())
    assert len(raw) == 1


def test_run_all_type_filter(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(
        data_dir=FIXTURES, out_dir=out_dir, limit=None, type_filter="product", no_baseline=False
    )
    raw = _json.loads((out_dir / "raw.json").read_text())
    assert {e["id"] for e in raw} == {"product_sample"}


def test_evaluate_page_with_baseline_includes_sanity_field():
    result = evaluate_page_with_baseline(FIXTURES, "article_sample")
    assert "sanity_traf_default" in result
    sd = result["sanity_traf_default"]
    assert "f1" in sd
    assert isinstance(sd["f1"], float)
    assert sd["error"] is None


def test_run_all_no_baseline_sets_trafilatura_null(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    run_all(data_dir=FIXTURES, out_dir=out_dir, limit=None, type_filter=None, no_baseline=True)
    raw = _json.loads((out_dir / "raw.json").read_text())
    for e in raw:
        assert e["trafilatura"] is None
