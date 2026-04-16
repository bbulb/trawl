"""Unit tests for WCXB aggregation + report rendering."""

from benchmarks.wcxb.aggregate import aggregate, render_report


def _mk(id_, ptype, trawl_f1, traf_f1, trawl_err=None, traf_err=None):
    return {
        "id": id_,
        "url": None,
        "page_type": ptype,
        "trawl": {
            "f1": trawl_f1,
            "precision": trawl_f1,
            "recall": trawl_f1,
            "time_ms": 30,
            "output_len": 100,
            "error": trawl_err,
        },
        "trafilatura": {
            "f1": traf_f1,
            "precision": traf_f1,
            "recall": traf_f1,
            "time_ms": 20,
            "output_len": 90,
            "error": traf_err,
        },
        "with_snippets_hit": {"trawl": 0, "trafilatura": 0, "total": 0},
        "without_snippets_hit": {"trawl": 0, "trafilatura": 0, "total": 0},
    }


def test_aggregate_overall_excludes_errored_rows():
    entries = [
        _mk("a", "article", 0.9, 0.9),
        _mk("b", "article", 0.8, 0.7),
        _mk("c", "article", 0.0, 0.0, trawl_err="boom"),  # excluded from averages
    ]
    agg = aggregate(entries)
    assert agg["overall"]["n_included"] == 2
    assert abs(agg["overall"]["trawl"]["f1"] - 0.85) < 1e-9
    assert agg["errors"]["trawl"] == 1
    assert agg["errors"]["trafilatura"] == 0


def test_aggregate_by_type_groups():
    entries = [
        _mk("a", "article", 0.9, 0.9),
        _mk("b", "product", 0.5, 0.4),
        _mk("c", "article", 0.8, 0.8),
    ]
    agg = aggregate(entries)
    by_type = {r["type"]: r for r in agg["by_type"]}
    assert by_type["article"]["n"] == 2
    assert abs(by_type["article"]["trawl_f1"] - 0.85) < 1e-9
    assert by_type["product"]["n"] == 1
    assert abs(by_type["product"]["delta"] - 0.1) < 1e-9  # 0.5 - 0.4


def test_aggregate_top_wins_and_losses_sorted_by_delta():
    entries = [
        _mk("win1", "article", 0.9, 0.5),  # +0.4
        _mk("win2", "article", 0.8, 0.5),  # +0.3
        _mk("loss1", "article", 0.5, 0.9),  # -0.4
        _mk("tie", "article", 0.5, 0.5),  #  0.0
    ]
    agg = aggregate(entries, top_n=2)
    assert [w["id"] for w in agg["top_wins"]] == ["win1", "win2"]
    assert [loss["id"] for loss in agg["top_losses"]] == ["loss1"]


def test_render_report_contains_required_sections():
    entries = [_mk("a", "article", 0.9, 0.9)]
    agg = aggregate(entries)
    md = render_report(agg, corpus_label="dev", commit="abc123", n_pages=1)

    assert "# WCXB" in md
    assert "Corpus:" in md
    assert "Commit: abc123" in md
    assert "## Overall" in md
    assert "## By page type" in md
    assert "## Top" in md
    assert "## Errors" in md
