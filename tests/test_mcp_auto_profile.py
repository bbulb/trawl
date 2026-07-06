from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

NO_PROFILE_ERROR = "no profile for URL; provide a query or call profile_page first"


@pytest.fixture(autouse=True)
def _reset_auto_profile_state(monkeypatch):
    from trawl_mcp import server as mcp_server

    monkeypatch.setattr(mcp_server, "_auto_profile_failed_hosts", set())
    monkeypatch.setattr(mcp_server, "_auto_profile_attempt_count", 0)
    monkeypatch.delenv("TRAWL_MCP_AUTO_PROFILE", raising=False)
    monkeypatch.delenv("TRAWL_MCP_AUTO_PROFILE_MAX", raising=False)


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


def _suggest_profile_payload(url: str, query: str) -> dict:
    return {
        "url": url,
        "query": query,
        "fetcher_used": "playwright+trafilatura",
        "fetch_ms": 100,
        "chunk_ms": 10,
        "retrieval_ms": 20,
        "total_ms": 140,
        "page_chars": 400,
        "n_chunks_total": 1,
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [{"text": "retrieved content", "chunk_index": 0}],
        "error": None,
        "profile_used": False,
        "profile_hash": None,
        "path": "full_pipeline",
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
            "lca_tag": "MAIN",
            "lca_path": ["HTML", "BODY", "MAIN"],
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
async def test_fetch_page_auto_profiles_suggest_profile_and_retries(monkeypatch):
    from trawl_mcp import server as mcp_server

    url = "https://suggest.test/page"
    query = "needle"
    fetch_calls: list[dict] = []
    profile_calls: list[dict] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, max_cache_age_s
        fetch_calls.append({"url": url, "query": query, "record_telemetry": record_telemetry})
        payload = (
            _suggest_profile_payload(url, query)
            if len(fetch_calls) == 1
            else _profile_payload(url)
        )
        return SimpleNamespace(payload=payload)

    async def fake_generate_profile(url, *, force_refresh=False):
        profile_calls.append({"url": url, "force_refresh": force_refresh})
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
            "lca_tag": "MAIN",
            "lca_path": ["HTML", "BODY", "MAIN"],
        }

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")
    monkeypatch.setenv("TRAWL_MCP_AUTO_PROFILE", "1")

    response = await mcp_server._call_fetch_page({"url": url, "query": query})
    payload = json.loads(response[0].text)

    assert payload["ok"] is True
    assert payload["path"] == "profile_direct"
    assert payload["profile_used"] is True
    assert payload["auto_profile_requested"] is True
    assert payload["profile_attempted"] is True
    assert profile_calls == [{"url": url, "force_refresh": False}]
    assert len(fetch_calls) == 2
    assert fetch_calls[0]["record_telemetry"] is True
    assert fetch_calls[1]["record_telemetry"] is True


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_accept_gate_keeps_acceptable_profile(monkeypatch):
    from trawl_mcp import server as mcp_server

    url = "https://quality-keep.test/page"
    query = "needle"
    fetch_calls: list[str] = []
    delete_calls: list[str] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        fetch_calls.append(url)
        payload = (
            _suggest_profile_payload(url, query)
            if len(fetch_calls) == 1
            else _profile_payload(url)
        )
        return SimpleNamespace(payload=payload)

    async def fake_generate_profile(url, *, force_refresh=False):
        del force_refresh
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
            "lca_tag": "DIV",
            "lca_path": ["HTML", "BODY", "DIV"],
        }

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "_delete_auto_profile", lambda url: delete_calls.append(url))
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page({"url": url, "query": query, "auto_profile": True})
    payload = json.loads(response[0].text)

    assert payload["path"] == "profile_direct"
    assert payload["profile_used"] is True
    assert "auto_profile_rejected" not in payload
    assert delete_calls == []
    assert mcp_server._auto_profile_failed_hosts == set()
    assert fetch_calls == [url, url]


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_accept_gate_rejects_shallow_div(monkeypatch):
    from trawl_mcp import server as mcp_server

    url = "https://quality-reject.test/page"
    query = "needle"
    fetch_calls: list[str] = []
    delete_calls: list[str] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        fetch_calls.append(url)
        return SimpleNamespace(payload=_suggest_profile_payload(url, query))

    async def fake_generate_profile(url, *, force_refresh=False):
        del force_refresh
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
            "lca_tag": "DIV",
            "lca_path": ["HTML", "DIV"],
        }

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "_delete_auto_profile", lambda url: delete_calls.append(url))
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page({"url": url, "query": query, "auto_profile": True})
    payload = json.loads(response[0].text)

    assert payload["path"] == "full_pipeline"
    assert payload["profile_used"] is False
    assert payload["profile_attempted"] is True
    assert payload["auto_profile_rejected"] == "quality:DIV/2"
    assert payload["profile_page"]["ok"] is True
    assert delete_calls == [url]
    assert mcp_server._auto_profile_failed_hosts == {"quality-reject.test"}
    assert fetch_calls == [url]


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_does_not_fire_when_off(monkeypatch):
    from trawl_mcp import server as mcp_server

    profile_calls: list[str] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        return SimpleNamespace(payload=_suggest_profile_payload(url, query))

    async def fake_generate_profile(url, *, force_refresh=False):
        del force_refresh
        profile_calls.append(url)
        return {"ok": True, "main_selector": "main.content"}

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page(
        {"url": "https://off.test/page", "query": "needle"}
    )
    payload = json.loads(response[0].text)

    assert payload["profile_used"] is False
    assert "auto_profile_requested" not in payload
    assert profile_calls == []

    monkeypatch.setenv("TRAWL_MCP_AUTO_PROFILE", "1")
    response = await mcp_server._call_fetch_page(
        {"url": "https://override.test/page", "query": "needle", "auto_profile": False}
    )
    payload = json.loads(response[0].text)

    assert payload["profile_used"] is False
    assert "auto_profile_requested" not in payload
    assert profile_calls == []


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_failed_host_cooldown(monkeypatch):
    from trawl_mcp import server as mcp_server

    profile_calls: list[str] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        return SimpleNamespace(payload=_suggest_profile_payload(url, query))

    async def fake_generate_profile(url, *, force_refresh=False):
        del force_refresh
        profile_calls.append(url)
        return {"ok": False, "stage": "vlm", "error": "invalid JSON", "notes": []}

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")

    response = await mcp_server._call_fetch_page(
        {"url": "https://cooldown.test/one", "query": "needle", "auto_profile": True}
    )
    first = json.loads(response[0].text)

    response = await mcp_server._call_fetch_page(
        {"url": "https://cooldown.test/two", "query": "needle", "auto_profile": True}
    )
    second = json.loads(response[0].text)

    assert first["profile_attempted"] is True
    assert first["profile_error"] == "invalid JSON"
    assert second["profile_attempted"] is False
    assert profile_calls == ["https://cooldown.test/one"]


@pytest.mark.asyncio
async def test_fetch_page_auto_profile_cap_stops_triggering(monkeypatch):
    from trawl_mcp import server as mcp_server

    fetch_counts: dict[str, int] = {}
    profile_calls: list[str] = []

    async def fake_fetch_page_routed(
        url,
        query=None,
        *,
        k=None,
        use_hyde=False,
        use_rerank=True,
        record_telemetry=True,
        max_cache_age_s=None,
    ):
        del k, use_hyde, use_rerank, record_telemetry, max_cache_age_s
        fetch_counts[url] = fetch_counts.get(url, 0) + 1
        payload = (
            _suggest_profile_payload(url, query)
            if fetch_counts[url] == 1
            else _profile_payload(url)
        )
        return SimpleNamespace(payload=payload)

    async def fake_generate_profile(url, *, force_refresh=False):
        del force_refresh
        profile_calls.append(url)
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
            "lca_tag": "MAIN",
            "lca_path": ["HTML", "BODY", "MAIN"],
        }

    monkeypatch.setattr(mcp_server, "_run_fetch_page_routed", fake_fetch_page_routed)
    monkeypatch.setattr(mcp_server, "_run_generate_profile", fake_generate_profile)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.test/v1")
    monkeypatch.setenv("TRAWL_MCP_AUTO_PROFILE_MAX", "1")

    await mcp_server._call_fetch_page(
        {"url": "https://cap-one.test/page", "query": "needle", "auto_profile": True}
    )
    response = await mcp_server._call_fetch_page(
        {"url": "https://cap-two.test/page", "query": "needle", "auto_profile": True}
    )
    payload = json.loads(response[0].text)

    assert payload["profile_attempted"] is False
    assert payload["auto_profile_skipped"] == "cap_reached"
    assert profile_calls == ["https://cap-one.test/page"]
    assert fetch_counts == {
        "https://cap-one.test/page": 2,
        "https://cap-two.test/page": 1,
    }


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
    assert "TRAWL_MCP_AUTO_PROFILE" in auto_profile["description"]

    monkeypatch.setenv("TRAWL_MCP_AUTO_PROFILE", "1")
    tools = await mcp_server.list_tools()
    fetch_tool = next(tool for tool in tools if tool.name == "fetch_page")
    auto_profile = fetch_tool.inputSchema["properties"]["auto_profile"]
    assert auto_profile["default"] is True
