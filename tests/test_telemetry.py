"""Unit tests for src/trawl/telemetry.py. No external servers."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from trawl import telemetry
from trawl.pipeline import PipelineResult


def _sample_result(url: str = "https://example.com/a", query: str = "what") -> PipelineResult:
    return PipelineResult(
        url=url,
        query=query,
        fetcher_used="playwright",
        fetch_ms=100,
        chunk_ms=10,
        retrieval_ms=20,
        total_ms=140,
        page_chars=1234,
        n_chunks_total=7,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
        path="full_page_retrieval",
    )


def test_record_noop_when_disabled(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.delenv("TRAWL_TELEMETRY", raising=False)
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result())

    assert not target.exists()
