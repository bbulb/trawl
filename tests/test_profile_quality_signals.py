from __future__ import annotations

import pytest

from trawl import pipeline


def test_profile_query_coverage_tokenization_math():
    chunks = [
        {"text": "ALPHA and beta are returned.", "score": 0.42},
        {"text": "unrelated text", "score": 0.1},
    ]

    assert pipeline._profile_query_coverage("alpha beta gamma", chunks) == pytest.approx(2 / 3)
    assert pipeline._profile_top_score(chunks) == 0.42


def test_profile_quality_signals_none_safety():
    chunks = [{"text": "alpha beta", "score": None}]

    assert pipeline._profile_top_score([]) is None
    assert pipeline._profile_top_score(chunks) is None
    assert pipeline._profile_query_coverage(None, chunks) is None
    assert pipeline._profile_query_coverage("!!!", chunks) is None
