from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from trawl import pipeline, retrieval, telemetry
from trawl.chunking import Chunk
from trawl.fetchers.playwright import FetchResult


def _chunk(text: str, heading: str = "", *, index: int = 0) -> Chunk:
    return Chunk(
        text=text,
        heading_path=[heading] if heading else [],
        char_count=len(text),
        chunk_index=index,
        embed_text=text,
    )


def test_retrieve_uses_bm25_fallback_when_embedding_fails(monkeypatch):
    chunks = [
        _chunk("general discussion of task scheduling", index=0),
        _chunk("asyncio.gather awaits tasks concurrently", index=1),
        _chunk("unrelated installation notes", index=2),
    ]

    def fail_embed(_client, _base_url, _model, _texts):
        raise httpx.ConnectError("embedding down")

    monkeypatch.setattr(retrieval, "_embed_batch", fail_embed)

    result = retrieval.retrieve("asyncio.gather tasks", chunks, k=2)

    assert result.error is None
    assert result.warning
    assert "embedding unavailable" in result.warning
    assert result.retrieval_mode == "bm25_fallback"
    assert result.fusion_weights == {"bm25": 1.0}
    assert "asyncio.gather" in result.scored[0].chunk.text
    assert result.n_chunks_embedded == 0


@pytest.mark.parametrize(
    ("query", "texts", "expected"),
    [
        (
            "TRAWL_EMBED_CACHE_TTL repeated pages",
            [
                "Install Chromium with playwright install chromium.",
                "Set TRAWL_EMBED_CACHE_TTL=86400 for repeated queries over the same pages.",
                "The reranker endpoint can improve precision.",
            ],
            "TRAWL_EMBED_CACHE_TTL=86400",
        ),
        (
            "Yi Sun-sin Battle of Myeongnyang",
            [
                "Yi Sun-sin commanded Joseon forces at the Battle of Myeongnyang.",
                "The article includes a table of later naval campaigns.",
                "A biography section lists family members.",
            ],
            "Battle of Myeongnyang",
        ),
        (
            "asyncio.gather tasks",
            [
                "General discussion of task scheduling.",
                "asyncio.gather awaits tasks concurrently.",
                "Unrelated installation notes.",
            ],
            "asyncio.gather",
        ),
    ],
)
def test_bm25_fallback_recovers_representative_facts(monkeypatch, query, texts, expected):
    chunks = [_chunk(text, index=i) for i, text in enumerate(texts)]

    def fail_embed(_client, _base_url, _model, _texts):
        raise httpx.ConnectError("embedding down")

    monkeypatch.setattr(retrieval, "_embed_batch", fail_embed)

    result = retrieval.retrieve(query, chunks, k=1)

    assert result.error is None
    assert result.retrieval_mode == "bm25_fallback"
    assert expected in result.scored[0].chunk.text


def test_pipeline_returns_chunks_with_warning_when_embedding_fails(monkeypatch):
    monkeypatch.setenv("TRAWL_FETCH_CACHE_TTL", "0")

    fetched = FetchResult(
        url="https://example.test/asyncio",
        html="<html><title>Asyncio</title><body>asyncio.gather awaits tasks</body></html>",
        markdown="asyncio.gather awaits tasks concurrently\n\nother unrelated text",
        raw_html="",
        fetcher="test",
        elapsed_ms=1,
    )

    def fake_fetch_html(_url, query=None):
        return (
            fetched,
            pipeline.extraction.ExtractedContent(
                markdown=fetched.markdown,
                extractor="test",
            ),
            "test",
        )

    def fail_embed(_client, _base_url, _model, _texts):
        raise httpx.ConnectError("embedding down")

    monkeypatch.setattr(pipeline, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(retrieval, "_embed_batch", fail_embed)

    result = pipeline.fetch_relevant(
        "https://example.test/asyncio",
        "asyncio.gather tasks",
        use_rerank=False,
    )
    payload = pipeline.to_dict(result)

    assert payload["error"] is None
    assert payload["warnings"]
    assert "embedding unavailable" in payload["warnings"][0]
    assert payload["retrieval_diagnostics"]["mode"] == "bm25_fallback"
    assert payload["chunks"]
    assert "asyncio.gather" in payload["chunks"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_fetch_page_payload_keeps_degraded_warning(monkeypatch):
    from trawl_mcp import server as mcp_server

    warning = "embedding unavailable; using BM25 fallback: ConnectError: embedding down"

    def fake_fetch_relevant(_url, _query=None, *, k=None, use_hyde=False, use_rerank=True):
        return SimpleNamespace()

    def fake_to_dict(_result):
        return {
            "error": None,
            "warnings": [warning],
            "chunks": [{"text": "asyncio.gather awaits tasks"}],
            "hyde_text": "",
        }

    monkeypatch.setattr(mcp_server, "fetch_relevant", fake_fetch_relevant)
    monkeypatch.setattr(mcp_server, "to_dict", fake_to_dict)

    response = await mcp_server._call_fetch_page(
        {"url": "https://example.test/asyncio", "query": "asyncio.gather tasks"}
    )
    payload = json.loads(response[0].text)

    assert payload["ok"] is True
    assert payload["warnings"] == [warning]
    assert payload["n_chunks_returned"] == 1


def test_telemetry_includes_degraded_warnings():
    result = pipeline.PipelineResult(
        url="https://example.test/asyncio",
        query="asyncio.gather tasks",
        fetcher_used="test",
        fetch_ms=1,
        chunk_ms=1,
        retrieval_ms=1,
        total_ms=3,
        page_chars=100,
        n_chunks_total=1,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
        warnings=["embedding unavailable; using BM25 fallback: ConnectError: embedding down"],
    )

    event = telemetry._build_event(result)

    assert event["warnings"] == result.warnings
