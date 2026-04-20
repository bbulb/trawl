"""Unit tests for the defensive chunk-window cap in reranking.

Tests only the pure helper `_apply_caps` -- no HTTP. The cap guards
against payload sizes that exceed the reranker model's context
(bge-reranker-v2-m3, 8192 tokens; see the 2026-04-20 stability
diagnostic). Defaults are set so normal trawl workload never trips
the cap.
"""

from __future__ import annotations

import pytest

from trawl.chunking import Chunk
from trawl.reranking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_DOCS,
    MIN_PER_DOC_CHARS,
    _apply_caps,
)
from trawl.retrieval import ScoredChunk


def _scored(bodies: list[str]) -> list[ScoredChunk]:
    out = []
    for i, body in enumerate(bodies):
        out.append(
            ScoredChunk(
                chunk=Chunk(text=body, embed_text=body, chunk_index=i),
                score=1.0 - i * 0.01,
            )
        )
    return out


def test_defaults_do_not_cap_typical_workload(monkeypatch):
    # Simulate a realistic high-end: retrieve_k=24 (upper bound from
    # _adaptive_k * 2) with typical per-chunk length ~1500 chars body +
    # title/heading (MAX_EMBED_INPUT_CHARS is 1800 but pages rarely fill
    # it). Total ~= 36 000 chars < DEFAULT_MAX_CHARS (40 000).
    monkeypatch.delenv("TRAWL_RERANK_MAX_DOCS", raising=False)
    monkeypatch.delenv("TRAWL_RERANK_MAX_CHARS", raising=False)
    docs = [f"Title: P\n\n{'x' * 1400}" for _ in range(24)]
    scored = _scored(docs)
    r_scored, r_docs, tel = _apply_caps("a query", scored, docs)
    assert len(r_docs) == 24
    assert tel["pre_docs"] == tel["post_docs"] == 24
    assert tel["pre_chars"] == tel["post_chars"]
    assert all(len(d) == len(docs[0]) for d in r_docs)
    assert r_scored is scored or len(r_scored) == len(scored)


def test_doc_count_cap_drops_tail(monkeypatch):
    monkeypatch.setenv("TRAWL_RERANK_MAX_DOCS", "5")
    monkeypatch.delenv("TRAWL_RERANK_MAX_CHARS", raising=False)
    docs = [f"body {i}" for i in range(12)]
    scored = _scored(docs)
    r_scored, r_docs, tel = _apply_caps("q", scored, docs)
    assert len(r_docs) == 5
    assert r_docs == docs[:5]
    assert len(r_scored) == 5
    # Highest-ranked chunks are preserved (cosine-sorted input).
    assert [s.chunk.chunk_index for s in r_scored] == [0, 1, 2, 3, 4]
    assert tel["pre_docs"] == 12 and tel["post_docs"] == 5


def test_char_cap_truncates_each_doc(monkeypatch):
    monkeypatch.delenv("TRAWL_RERANK_MAX_DOCS", raising=False)
    monkeypatch.setenv("TRAWL_RERANK_MAX_CHARS", "5000")
    docs = ["x" * 1000 for _ in range(10)]  # 10 000 chars total
    r_scored, r_docs, tel = _apply_caps("q", _scored(docs), docs)
    assert len(r_docs) == 10  # doc count unchanged
    # (5000 - len("q")) // 10 == 499
    assert all(len(d) == 499 for d in r_docs)
    assert tel["pre_chars"] > tel["post_chars"]


def test_char_cap_respects_min_per_doc(monkeypatch):
    # Pathological: MAX_CHARS so tight that proportional truncation
    # would give < MIN_PER_DOC_CHARS. We keep MIN_PER_DOC_CHARS as the
    # floor so each doc still carries some signal (even if total
    # overshoots MAX_CHARS slightly -- server-side cap still protects
    # us, this is just a retry floor).
    monkeypatch.delenv("TRAWL_RERANK_MAX_DOCS", raising=False)
    monkeypatch.setenv("TRAWL_RERANK_MAX_CHARS", "500")
    docs = ["x" * 1000 for _ in range(30)]
    r_scored, r_docs, tel = _apply_caps("q", _scored(docs), docs)
    assert all(len(d) == MIN_PER_DOC_CHARS for d in r_docs)


def test_caps_stack_docs_then_chars(monkeypatch):
    monkeypatch.setenv("TRAWL_RERANK_MAX_DOCS", "10")
    monkeypatch.setenv("TRAWL_RERANK_MAX_CHARS", "2000")
    docs = ["x" * 500 for _ in range(50)]  # 25 000 chars total
    r_scored, r_docs, tel = _apply_caps("q", _scored(docs), docs)
    # First step drops to 10 docs. Then 2000 chars budget across 10
    # docs -> ~199 chars per doc.
    assert len(r_docs) == 10
    assert len(r_scored) == 10
    assert all(len(d) <= 200 for d in r_docs)


def test_zero_sentinel_disables_cap(monkeypatch):
    monkeypatch.setenv("TRAWL_RERANK_MAX_DOCS", "0")
    monkeypatch.setenv("TRAWL_RERANK_MAX_CHARS", "0")
    docs = ["x" * 5000 for _ in range(100)]
    r_scored, r_docs, tel = _apply_caps("q", _scored(docs), docs)
    assert len(r_docs) == 100
    assert tel["pre_chars"] == tel["post_chars"]


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRAWL_RERANK_MAX_DOCS", "not-a-number")
    monkeypatch.setenv("TRAWL_RERANK_MAX_CHARS", "also-bad")
    # Should not raise; uses DEFAULT_MAX_{DOCS,CHARS}.
    docs = ["x" * 100 for _ in range(5)]
    r_scored, r_docs, tel = _apply_caps("q", _scored(docs), docs)
    assert len(r_docs) == 5
    assert DEFAULT_MAX_DOCS > 0
    assert DEFAULT_MAX_CHARS > 0


def test_warning_emitted_when_cap_fires(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("TRAWL_RERANK_MAX_DOCS", "3")
    monkeypatch.delenv("TRAWL_RERANK_MAX_CHARS", raising=False)
    docs = [f"body {i}" for i in range(8)]
    with caplog.at_level(logging.WARNING, logger="trawl.reranking"):
        _apply_caps("q", _scored(docs), docs)
    assert any(
        "reranker input capped" in rec.message for rec in caplog.records
    ), f"expected WARNING; got {[r.message for r in caplog.records]}"


def test_no_warning_when_cap_does_not_fire(monkeypatch, caplog):
    import logging

    monkeypatch.delenv("TRAWL_RERANK_MAX_DOCS", raising=False)
    monkeypatch.delenv("TRAWL_RERANK_MAX_CHARS", raising=False)
    docs = ["body"] * 3
    with caplog.at_level(logging.WARNING, logger="trawl.reranking"):
        _apply_caps("q", _scored(docs), docs)
    assert not any(
        "reranker input capped" in rec.message for rec in caplog.records
    )


def test_empty_inputs_are_safe(monkeypatch):
    monkeypatch.delenv("TRAWL_RERANK_MAX_DOCS", raising=False)
    monkeypatch.delenv("TRAWL_RERANK_MAX_CHARS", raising=False)
    r_scored, r_docs, tel = _apply_caps("q", [], [])
    assert r_scored == []
    assert r_docs == []
    assert tel["pre_docs"] == tel["post_docs"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
