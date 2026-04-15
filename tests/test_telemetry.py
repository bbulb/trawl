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


def test_build_event_fields():
    r = _sample_result(url="https://www.example.com/a/b?x=1", query="hello")
    event = telemetry._build_event(r)

    assert event["schema"] == 1
    assert event["host"] == "www.example.com"
    assert event["url"] == "https://www.example.com/a/b?x=1"
    # sha1("hello") = aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d → first 16
    assert event["query_sha1"] == "aaf4c61ddcc5e8a2"
    assert event["fetcher_used"] == "playwright"
    assert event["path"] == "full_page_retrieval"
    assert event["profile_used"] is False
    assert event["profile_hash"] is None
    assert event["rerank_used"] is False
    assert event["hyde_used"] is False
    assert event["fetch_ms"] == 100
    assert event["total_ms"] == 140
    assert event["n_chunks_total"] == 7
    assert event["error"] is None
    assert "ts" in event and event["ts"].endswith("Z")
    # Must NOT contain raw query, chunks, or hyde_text
    assert "query" not in event
    assert "chunks" not in event
    assert "hyde_text" not in event
