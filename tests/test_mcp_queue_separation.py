from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest


def _payload(url: str, *, error: str | None = None, path: str = "full_page_retrieval") -> dict:
    return {
        "url": url,
        "query": "q",
        "fetcher_used": "test",
        "fetch_ms": 1,
        "chunk_ms": 0,
        "retrieval_ms": 0,
        "total_ms": 1,
        "page_chars": 1,
        "n_chunks_total": 1,
        "structured_path": path == "raw_passthrough",
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [] if error else [{"text": "ok"}],
        "error": error,
        "path": path,
    }


@pytest.mark.asyncio
async def test_browser_free_fetch_page_calls_are_not_blocked_by_slow_browser_call(monkeypatch):
    from trawl_mcp import server as mcp_server

    slow_started = threading.Event()
    calls: list[tuple[str, bool, str]] = []
    calls_lock = threading.Lock()

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
        del query, k, use_hyde, use_rerank, record_telemetry
        with calls_lock:
            calls.append((url, allow_browser, threading.current_thread().name))
        if "slow-browser" in url:
            assert allow_browser is True
            slow_started.set()
            time.sleep(0.45)
            return SimpleNamespace(payload=_payload(url))

        assert allow_browser is False
        path = "raw_passthrough" if url.endswith(".json") else "full_page_retrieval"
        return SimpleNamespace(payload=_payload(url, path=path))

    monkeypatch.setattr(mcp_server, "fetch_relevant", fake_fetch_relevant)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setattr(mcp_server, "_profile_candidate_exists", lambda url: False, raising=False)

    slow_task = asyncio.create_task(
        mcp_server._call_fetch_page({"url": "https://example.test/slow-browser", "query": "q"})
    )
    try:
        for _ in range(100):
            if slow_started.is_set():
                break
            await asyncio.sleep(0.005)
        assert slow_started.is_set()

        fast_urls = [
            "https://api.example.test/data.json",
            "https://example.test/manual.pdf",
            "https://github.com/python/cpython/issues/1",
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
            "https://stackoverflow.com/questions/11828270/how-do-i-exit-vim",
        ]
        fast_tasks = [
            asyncio.create_task(mcp_server._call_fetch_page({"url": url, "query": "q"}))
            for url in fast_urls
        ]

        started = time.monotonic()
        responses = await asyncio.wait_for(asyncio.gather(*fast_tasks), timeout=0.25)
        elapsed = time.monotonic() - started

        assert elapsed < 0.25
        assert [json.loads(response[0].text)["ok"] for response in responses] == [True] * 5
    finally:
        await slow_task

    fast_calls = [call for call in calls if "slow-browser" not in call[0]]
    assert len(fast_calls) == 5
    assert {allow_browser for _, allow_browser, _ in fast_calls} == {False}
    assert all(thread_name.startswith("trawl-general") for _, _, thread_name in fast_calls)


@pytest.mark.asyncio
async def test_browser_required_general_result_retries_on_browser_executor(monkeypatch):
    from trawl_mcp import server as mcp_server

    calls: list[tuple[bool, bool, str]] = []

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
        del query, k, use_hyde, use_rerank
        calls.append((allow_browser, record_telemetry, threading.current_thread().name))
        if not allow_browser:
            return SimpleNamespace(
                payload=_payload(url, error="browser fallback required: GitHub API failed")
            )
        return SimpleNamespace(payload=_payload(url))

    monkeypatch.setattr(mcp_server, "fetch_relevant", fake_fetch_relevant)
    monkeypatch.setattr(mcp_server, "to_dict", lambda result: dict(result.payload))
    monkeypatch.setattr(mcp_server, "_profile_candidate_exists", lambda url: False, raising=False)

    response = await mcp_server._call_fetch_page(
        {"url": "https://github.com/python/cpython/issues/1", "query": "q"}
    )
    payload = json.loads(response[0].text)

    assert payload["ok"] is True
    assert len(calls) == 2
    assert calls[0][:2] == (False, False)
    assert calls[1][:2] == (True, True)
    assert calls[0][2].startswith("trawl-general")
    assert calls[1][2].startswith("trawl-browser")


def test_browser_disabled_pipeline_does_not_call_playwright_api_fallback(monkeypatch):
    import trawl.profiles as profiles
    from trawl import pipeline

    monkeypatch.setattr(profiles, "track_visit", lambda url: None)
    monkeypatch.setattr(profiles, "load_profile", lambda url: None)
    monkeypatch.setattr(profiles, "get_visit_count", lambda url: 0)

    def fake_github_fetch(url, *, allow_browser_fallback=True):
        assert allow_browser_fallback is False
        return pipeline.playwright.make_error_result(
            url,
            "github",
            time.monotonic(),
            "browser fallback required: GitHub API failed",
        )

    def fail_playwright_fetch(url):
        raise AssertionError(f"Playwright fetch ran on browser-disabled path: {url}")

    monkeypatch.setattr(pipeline.github, "matches", lambda url: True)
    monkeypatch.setattr(pipeline.github, "fetch", fake_github_fetch)
    monkeypatch.setattr(pipeline.playwright, "fetch", fail_playwright_fetch)

    result = pipeline.fetch_relevant(
        "https://github.com/python/cpython/issues/1",
        "q",
        allow_browser=False,
        record_telemetry=False,
    )

    assert result.error is not None
    assert result.error.startswith("browser fallback required:")
