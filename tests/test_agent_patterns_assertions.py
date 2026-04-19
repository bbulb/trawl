"""Schema + harness evaluator tests for the C16 assertion keys.

Covers the four enrichment-payload assertions added on top of the
`tests/agent_patterns/` catalog:

    excerpts_min_count
    outbound_links_contain_any
    page_entities_contain_any
    chain_hints_has_key

Schema side: verify the whitelist accepts valid shapes and rejects
malformed ones. Harness side: verify `_evaluate_assertions` returns
empty / non-empty failure lists for representative measurement dicts.
No network, no pipeline imports — these are pure-function checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_patterns.schema import (  # noqa: E402
    ASSERTION_KEYS,
    PatternValidationError,
    parse_pattern,
)
from test_agent_patterns import _evaluate_assertions  # noqa: E402

# ---------- schema whitelist


def test_new_keys_are_whitelisted():
    for key in (
        "excerpts_min_count",
        "outbound_links_contain_any",
        "page_entities_contain_any",
        "chain_hints_has_key",
    ):
        assert key in ASSERTION_KEYS


def _make_pattern(assertions: dict) -> dict:
    return {
        "id": "t",
        "primary_agent": ["hermes"],
        "category": "single_fetch",
        "description": "x",
        "url": "https://example.com",
        "query": "q",
        "assertions": assertions,
    }


# ---------- excerpts_min_count shape


def test_excerpts_min_count_accepts_int():
    parse_pattern(_make_pattern({"excerpts_min_count": 2}))


def test_excerpts_min_count_accepts_comparison_string():
    parse_pattern(_make_pattern({"excerpts_min_count": ">= 2"}))


def test_excerpts_min_count_rejects_negative_int():
    with pytest.raises(PatternValidationError, match="non-negative"):
        parse_pattern(_make_pattern({"excerpts_min_count": -1}))


def test_excerpts_min_count_rejects_nonsense_string():
    with pytest.raises(PatternValidationError, match="comparison"):
        parse_pattern(_make_pattern({"excerpts_min_count": "abc"}))


# ---------- outbound_links_contain_any shape


def test_outbound_links_contain_any_accepts_list_of_strings():
    parse_pattern(_make_pattern({"outbound_links_contain_any": ["arxiv.org"]}))


def test_outbound_links_contain_any_rejects_empty_list():
    with pytest.raises(PatternValidationError, match="non-empty"):
        parse_pattern(_make_pattern({"outbound_links_contain_any": []}))


def test_outbound_links_contain_any_rejects_non_string_items():
    with pytest.raises(PatternValidationError, match="list of strings"):
        parse_pattern(_make_pattern({"outbound_links_contain_any": [123]}))


# ---------- page_entities_contain_any shape


def test_page_entities_contain_any_accepts_list_of_strings():
    parse_pattern(_make_pattern({"page_entities_contain_any": ["BGE"]}))


def test_page_entities_contain_any_rejects_scalar():
    with pytest.raises(PatternValidationError, match="list of strings"):
        parse_pattern(_make_pattern({"page_entities_contain_any": "BGE"}))


# ---------- chain_hints_has_key shape


def test_chain_hints_has_key_accepts_non_empty_string():
    parse_pattern(_make_pattern({"chain_hints_has_key": "pdf_template"}))


def test_chain_hints_has_key_rejects_empty_string():
    with pytest.raises(PatternValidationError, match="non-empty string"):
        parse_pattern(_make_pattern({"chain_hints_has_key": ""}))


def test_chain_hints_has_key_rejects_list():
    with pytest.raises(PatternValidationError, match="non-empty string"):
        parse_pattern(_make_pattern({"chain_hints_has_key": ["pdf_template"]}))


# ---------- evaluator — excerpts_min_count


def test_excerpts_min_count_passes_when_enough():
    fails = _evaluate_assertions(
        {"excerpts_min_count": ">= 2"},
        {"excerpts": [{"chunk_idx": 0, "summary_120c": "a"},
                      {"chunk_idx": 1, "summary_120c": "b"}]},
    )
    assert fails == []


def test_excerpts_min_count_fails_when_too_few():
    fails = _evaluate_assertions(
        {"excerpts_min_count": 3},
        {"excerpts": [{"chunk_idx": 0, "summary_120c": "a"}]},
    )
    assert len(fails) == 1
    assert "excerpts_min_count" in fails[0]


def test_excerpts_min_count_int_passes_when_equal():
    # int form is normalised to ">= N" inside the evaluator.
    fails = _evaluate_assertions(
        {"excerpts_min_count": 1},
        {"excerpts": [{"chunk_idx": 0, "summary_120c": "a"}]},
    )
    assert fails == []


# ---------- evaluator — outbound_links_contain_any


def test_outbound_links_contain_any_passes_on_url_match():
    fails = _evaluate_assertions(
        {"outbound_links_contain_any": ["arxiv.org"]},
        {"outbound_links": [
            {"url": "https://arxiv.org/pdf/2402.03216", "anchor_text": "PDF",
             "in_chunk_idx": 0},
        ]},
    )
    assert fails == []


def test_outbound_links_contain_any_passes_on_anchor_match():
    fails = _evaluate_assertions(
        {"outbound_links_contain_any": ["whitepaper"]},
        {"outbound_links": [
            {"url": "https://example.com/p.pdf",
             "anchor_text": "Download whitepaper",
             "in_chunk_idx": 0},
        ]},
    )
    assert fails == []


def test_outbound_links_contain_any_fails_when_missing():
    fails = _evaluate_assertions(
        {"outbound_links_contain_any": ["wikipedia"]},
        {"outbound_links": [
            {"url": "https://arxiv.org/pdf/x", "anchor_text": "PDF",
             "in_chunk_idx": 0},
        ]},
    )
    assert len(fails) == 1
    assert "outbound_links_contain_any" in fails[0]


def test_outbound_links_contain_any_fails_on_empty_links():
    fails = _evaluate_assertions(
        {"outbound_links_contain_any": ["arxiv"]},
        {"outbound_links": []},
    )
    assert len(fails) == 1


# ---------- evaluator — page_entities_contain_any


def test_page_entities_contain_any_passes_on_substring():
    fails = _evaluate_assertions(
        {"page_entities_contain_any": ["BGE"]},
        {"page_entities": ["BGE M3-Embedding", "Multi-Lingual Benchmarks"]},
    )
    assert fails == []


def test_page_entities_contain_any_fails_when_missing():
    fails = _evaluate_assertions(
        {"page_entities_contain_any": ["BERT"]},
        {"page_entities": ["BGE M3-Embedding"]},
    )
    assert len(fails) == 1
    assert "page_entities_contain_any" in fails[0]


# ---------- evaluator — chain_hints_has_key


def test_chain_hints_has_key_passes_when_present():
    fails = _evaluate_assertions(
        {"chain_hints_has_key": "pdf_template"},
        {"chain_hints": {"pdf_template": "https://arxiv.org/pdf/{id}",
                         "recommended_followup_filter": "site:arxiv.org"}},
    )
    assert fails == []


def test_chain_hints_has_key_fails_when_missing():
    fails = _evaluate_assertions(
        {"chain_hints_has_key": "pdf_template"},
        {"chain_hints": {"recommended_followup_filter": "site:github.com"}},
    )
    assert len(fails) == 1
    assert "pdf_template" in fails[0]


def test_chain_hints_has_key_fails_on_empty_hints():
    fails = _evaluate_assertions(
        {"chain_hints_has_key": "any_key"},
        {"chain_hints": {}},
    )
    assert len(fails) == 1
