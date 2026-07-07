from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_call_fetch_page_transcribe_images_null_defers_to_env(monkeypatch):
    from trawl_mcp import server as mcp_server

    seen: dict[str, bool | None] = {}

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
        transcribe_images=None,
    ):
        del url, query, k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        seen["transcribe_images"] = transcribe_images
        return SimpleNamespace(payload={"error": None, "chunks": [], "hyde_text": ""})

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))

    response = await mcp_server._call_fetch_page(
        {
            "url": "https://example.test/page",
            "query": "q",
            "auto_profile": False,
            "transcribe_images": None,
        }
    )
    payload = json.loads(response[0].text)

    assert seen["transcribe_images"] is None
    assert payload["ok"] is True
