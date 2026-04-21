"""Unit tests for src/trawl/telemetry.py. No external servers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from trawl import telemetry
from trawl.pipeline import PipelineResult


def hashlib_sha1_prefix(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


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
    assert event["rerank_capped"] is False
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


def test_build_event_propagates_rerank_capped_true():
    """rerank_capped=True on the result must surface in the JSONL event."""
    r = _sample_result()
    r.rerank_capped = True
    event = telemetry._build_event(r)
    assert event["rerank_capped"] is True


def test_record_appends_jsonl(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result(url="https://a.example.com/x", query="q1"))
    telemetry.record(_sample_result(url="https://b.example.com/y", query="q2"))
    telemetry.record(_sample_result(url="https://c.example.com/z", query="q3"))

    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        event = json.loads(line)
        assert event["schema"] == 1
        assert event["host"].endswith("example.com")


def test_record_creates_directory(tmp_path: Path, monkeypatch):
    target = tmp_path / "nested" / "dir" / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    telemetry.record(_sample_result())

    assert target.exists()
    assert target.parent.is_dir()


def test_rotation_when_exceeds_max_bytes(tmp_path: Path, monkeypatch):
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))
    # One event is ~500 bytes; 300 bytes forces rotation after the first write.
    monkeypatch.setenv("TRAWL_TELEMETRY_MAX_BYTES", "300")

    telemetry.record(_sample_result(query="first"))
    telemetry.record(_sample_result(query="second"))
    telemetry.record(_sample_result(query="third"))

    rotated = target.with_suffix(target.suffix + ".1")
    assert rotated.exists(), "rotated .1 file should be created"
    assert target.exists(), "new current file should be created"

    # Current file must contain only the most recent event(s).
    current_lines = target.read_text(encoding="utf-8").splitlines()
    assert len(current_lines) >= 1
    last_event = json.loads(current_lines[-1])
    # sha1("third")[:16]
    assert last_event["query_sha1"] == hashlib_sha1_prefix("third")


def test_record_swallows_io_errors(tmp_path: Path, monkeypatch, caplog):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("cannot test permission denial as root")
    # Point at a path whose parent cannot be created.
    target = tmp_path / "ro" / "t.jsonl"
    tmp_path.chmod(0o500)  # make tmp_path read+execute only
    try:
        monkeypatch.setenv("TRAWL_TELEMETRY", "1")
        monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

        with caplog.at_level("WARNING", logger="trawl.telemetry"):
            telemetry.record(_sample_result())  # must not raise

        assert any("telemetry record failed" in r.message for r in caplog.records)
    finally:
        tmp_path.chmod(0o700)  # restore so pytest can clean up


def test_fetch_relevant_records_telemetry(tmp_path: Path, monkeypatch):
    """fetch_relevant() must call telemetry.record exactly once, regardless
    of which internal return path was taken (error path is fine)."""
    target = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRAWL_TELEMETRY", "1")
    monkeypatch.setenv("TRAWL_TELEMETRY_PATH", str(target))

    from trawl import pipeline

    # query=None with no profile triggers the fast error-return path —
    # does not require any network or embedding server.
    result = pipeline.fetch_relevant("https://never.example.com/x", "")
    assert result.error is not None  # sanity

    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["url"] == "https://never.example.com/x"
    assert event["error"] is not None
