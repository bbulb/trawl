"""MCP-layer smoke test for the profile feature.

Monkeypatches trawl.profiles.generate_profile and
trawl.fetch_relevant so the test doesn't need a real VLM, network, or
Playwright. Verifies that list_tools reports both tools, that
fetch_page works with and without a query, and that profile_page
dispatches correctly.

Invoke:
    python tests/test_mcp_profile.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp.types import TextContent


def _fake_pipeline_result_with_profile():
    """Shape of a PipelineResult.to_dict() that came from the profile fast path."""
    return {
        "url": "https://example.com/",
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
        "chunks": [
            {
                "text": "main content",
                "heading": "",
                "char_count": 12,
                "chunk_index": 0,
                "score": None,
            }
        ],
        "error": None,
        "profile_used": True,
        "profile_hash": "abc123def456",
        "path": "profile_direct",
        "suggest_profile": False,
        "suggest_profile_reason": None,
        "output_chars": 12,
        "compression_ratio": 33.3,
    }


def _fake_pipeline_result_with_query():
    """Shape when a query was provided and no profile existed."""
    return {
        "url": "https://example.com/",
        "query": "what is this",
        "fetcher_used": "playwright+trafilatura",
        "fetch_ms": 2000,
        "chunk_ms": 10,
        "retrieval_ms": 200,
        "total_ms": 2300,
        "page_chars": 1000,
        "n_chunks_total": 3,
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [
            {
                "text": "result chunk",
                "heading": "",
                "char_count": 12,
                "chunk_index": 0,
                "score": 0.85,
            }
        ],
        "error": None,
        "profile_used": False,
        "profile_hash": None,
        "path": "full_page_retrieval",
        "suggest_profile": False,
        "suggest_profile_reason": None,
        "output_chars": 12,
        "compression_ratio": 83.3,
    }


def _install_fakes() -> None:
    """Replace fetch_relevant/to_dict and generate_profile before
    trawl_mcp.server is imported so the handlers bind to the fakes.
    """
    import trawl as trawl_mod
    import trawl.profiles as profiles_mod

    def _fake_fetch_relevant(url, query=None, *, k=None, use_hyde=False):
        # Return a minimal object with the attributes the existing
        # to_dict expects. Use a SimpleNamespace-like shim.
        from types import SimpleNamespace

        if query:
            data = _fake_pipeline_result_with_query()
        else:
            data = _fake_pipeline_result_with_profile()
        return SimpleNamespace(
            **{
                **data,
                "output_chars": data["output_chars"],
                "compression_ratio": data["compression_ratio"],
            }
        )

    def _fake_to_dict(result):
        return {k: v for k, v in result.__dict__.items()}

    def _fake_generate_profile(url, *, force_refresh=False):
        return {
            "ok": True,
            "url": url,
            "url_hash": "abc123def456",
            "cached": False,
            "main_selector": "main.content",
            "lca_tag": "MAIN",
            "subtree_char_count": 400,
            "verification_anchors": ["anchor1", "anchor2", "anchor3"],
            "page_type": "other",
            "structure_description": "fake profile for testing",
        }

    trawl_mod.fetch_relevant = _fake_fetch_relevant
    trawl_mod.to_dict = _fake_to_dict
    profiles_mod.generate_profile = _fake_generate_profile


async def _run_list_tools():
    from trawl_mcp.server import list_tools

    return await list_tools()


async def _run_call_tool(name: str, arguments: dict):
    from trawl_mcp.server import call_tool

    return await call_tool(name, arguments)


def main() -> int:
    _install_fakes()

    # Re-import server.py AFTER fakes are installed so its `from trawl
    # import fetch_relevant, to_dict` picks up the fake symbols.
    import importlib

    if "trawl_mcp.server" in sys.modules:
        importlib.reload(sys.modules["trawl_mcp.server"])

    # 1. list_tools includes fetch_page and profile_page.
    tools = asyncio.run(_run_list_tools())
    tool_names = [t.name for t in tools]
    print(f"tools = {tool_names}")
    assert "fetch_page" in tool_names, tool_names
    assert "profile_page" in tool_names, tool_names

    fetch_tool = next(t for t in tools if t.name == "fetch_page")
    assert fetch_tool.inputSchema["required"] == ["url"], fetch_tool.inputSchema["required"]

    # 2. fetch_page without query returns the profile-fast-path payload.
    result = asyncio.run(_run_call_tool("fetch_page", {"url": "https://example.com/"}))
    assert len(result) == 1 and isinstance(result[0], TextContent)
    payload = json.loads(result[0].text)
    print(
        f"fetch_page (no query) path={payload.get('path')} profile_used={payload.get('profile_used')}"
    )
    assert payload["profile_used"] is True, payload
    assert payload["path"] == "profile_direct", payload
    assert payload["ok"] is True, payload

    # 3. fetch_page with query returns the full-page payload.
    result = asyncio.run(
        _run_call_tool(
            "fetch_page",
            {
                "url": "https://example.com/",
                "query": "what is this",
            },
        )
    )
    payload = json.loads(result[0].text)
    print(
        f"fetch_page (with query) path={payload.get('path')} profile_used={payload.get('profile_used')}"
    )
    assert payload["profile_used"] is False, payload
    assert payload["path"] == "full_page_retrieval", payload

    # 4. profile_page returns a profile summary.
    result = asyncio.run(_run_call_tool("profile_page", {"url": "https://example.com/"}))
    payload = json.loads(result[0].text)
    print(f"profile_page ok={payload.get('ok')} selector={payload.get('main_selector')}")
    assert payload["ok"] is True, payload
    assert payload["main_selector"] == "main.content", payload
    assert payload["verification_anchors"] == ["anchor1", "anchor2", "anchor3"], payload

    print()
    print("OK: trawl-mcp profile tool tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
