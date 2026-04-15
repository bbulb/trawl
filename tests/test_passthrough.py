"""Tests for raw-passthrough handling of JSON/XML responses."""
from __future__ import annotations

from trawl.pipeline import PipelineResult, to_dict


def test_pipeline_result_has_passthrough_fields():
    r = PipelineResult(
        url="https://x/y.json",
        query="",
        fetcher_used="passthrough",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
    )
    assert r.content_type is None
    assert r.truncated is False
    d = to_dict(r)
    assert "content_type" in d
    assert "truncated" in d
