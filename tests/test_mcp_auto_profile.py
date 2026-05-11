from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

NO_PROFILE_ERROR = "no profile for URL; provide a query or call profile_page first"


def _missing_profile_payload(url: str) -> dict:
    return {
        "url": url,
        "query": "",
        "fetcher_used": None,
        "fetch_ms": 0,
        "chunk_ms": 0,
        "retrieval_ms": 0,
        "total_ms": 1,
        "page_chars": 0,
        "n_chunks_total": 0,
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [],
        "error": NO_PROFILE_ERROR,
        "profile_used": False,
        "profile_hash": None,
        "path": "error",
        "suggest_profile": True,
        "suggest_profile_reason": "visited 3 times",
    }


def _profile_payload(url: str) -> dict:
    return {
        "url": url,
        "query": "",
        "fetcher_used": "profile+trafilatura",
        "fetch_ms": 100,
        "chunk_ms": 10,
        "retrieval_ms": 0,
        "total_ms": 120,
        "page_chars": 400,
        "n_chunks_total": 1,
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [{"text": "main content", "chunk_index": 0}],
        "error": None,
        "profile_used": True,
        "profile_hash": "abc123def456",
        "path": "profile_direct",
        "suggest_profile": False,
        "suggest_profile_reason": None,
    }


@pytest.mark.asyncio
async def test_fetch_page_auto_profiles_missing_profile_and_retries(monkeypatch):
    import trawl.profiles as profiles
    from trawl_mcp import server as mcp_server

    url = "https://example.test/page"
    fetch_calls: list[dict] = []
    profile_calls: list[dict] = []

    def fake_fetch_relevant(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        allow_browser=True,
        record_telemetry=True,
    ):
        fetch_calls.append(
            {
                "url": url,
                "query": query,
                "allow_browser": allow_browser,
                "record_telemetry": record_telemetry,
            }
        )
        payload = _missing_profile_payload(url) if len(fetch_calls) == 1 else _profile_payload(url)
        return SimpleNamespace(payload=payload)

    def fake_generate_profile(url, *, force_refresh=False):
        profile_calls.append({"url": url, "force_refresh": force_refresh})
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
        }

    monkeypatch.setattr(mcp_server, "fetch_relevant", fake_fetch_relevant)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setattr(mcp_server, "_profile_candidate_exists", lambda url: False, raising=False)
    monkeypatch.setattr(profiles, "generate_profile", fake_generate_profile)
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page({"url": url, "auto_profile": True})
    payload = json.loads(response[0].text)

    assert payload["ok"] is True
    assert payload["path"] == "profile_direct"
    assert payload["profile_used"] is True
    assert payload["auto_profile_requested"] is True
    assert payload["profile_attempted"] is True
    assert payload["profile_page"]["ok"] is True
    assert profile_calls == [{"url": url, "force_refresh": False}]
    assert len(fetch_calls) == 2
    assert fetch_calls[0]["record_telemetry"] is False
    assert fetch_calls[1]["record_telemetry"] is True


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_reports_profile_failure(monkeypatch):
    import trawl.profiles as profiles
    from trawl_mcp import server as mcp_server

    url = "https://example.test/page"

    def fake_fetch_relevant(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        allow_browser=True,
        record_telemetry=True,
    ):
        del query, k, use_hyde, use_rerank, allow_browser, record_telemetry
        return SimpleNamespace(payload=_missing_profile_payload(url))

    def fake_generate_profile(url, *, force_refresh=False):
        del url, force_refresh
        return {"ok": False, "stage": "vlm", "error": "invalid JSON", "notes": []}

    monkeypatch.setattr(mcp_server, "fetch_relevant", fake_fetch_relevant)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setattr(mcp_server, "_profile_candidate_exists", lambda url: False, raising=False)
    monkeypatch.setattr(profiles, "generate_profile", fake_generate_profile)
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page({"url": url, "auto_profile": True})
    payload = json.loads(response[0].text)

    assert payload["ok"] is False
    assert payload["error"] == NO_PROFILE_ERROR
    assert payload["auto_profile_requested"] is True
    assert payload["profile_attempted"] is True
    assert payload["profile_error"] == "invalid JSON"
    assert payload["profile_page"]["ok"] is False
    assert payload["profile_page"]["stage"] == "vlm"


@pytest.mark.asyncio
async def test_fetch_page_schema_exposes_auto_profile(monkeypatch):
    from trawl_mcp import server as mcp_server

    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    tools = await mcp_server.list_tools()
    fetch_tool = next(tool for tool in tools if tool.name == "fetch_page")

    auto_profile = fetch_tool.inputSchema["properties"]["auto_profile"]
    assert auto_profile["type"] == "boolean"
    assert auto_profile["default"] is False
