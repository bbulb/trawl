"""In-process smoke test for trawl-mcp's streamable HTTP transport.

Starts uvicorn on a random localhost port in a background thread, then
speaks streamable-HTTP MCP (JSON-RPC + SSE, Mcp-Session-Id header) with
httpx the same way a streamable-HTTP MCP client does. fetch_relevant
is monkey-patched to avoid hitting llama-server or the network.

Invoke:
    python tests/test_mcp_http.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_fetch_relevant(url: str, query: str, k=None, use_hyde: bool = False):
    return SimpleNamespace(
        url=url,
        query=query,
        fetcher_used="fake",
        error=None,
        page_chars=120,
        output_chars=80,
        compression_ratio=0.67,
        n_chunks_total=1,
        total_ms=5,
        chunks=[{"heading": "fake", "text": "fake chunk for " + query, "score": 0.99}],
    )


def _fake_to_dict(result):
    return {
        "url": result.url,
        "query": result.query,
        "fetcher_used": result.fetcher_used,
        "error": result.error,
        "page_chars": result.page_chars,
        "output_chars": result.output_chars,
        "compression_ratio": result.compression_ratio,
        "n_chunks_total": result.n_chunks_total,
        "total_ms": result.total_ms,
        "chunks": result.chunks,
    }


def _install_fakes() -> None:
    import trawl_mcp.server as srv

    srv.fetch_relevant = _fake_fetch_relevant  # type: ignore[attr-defined]
    srv.to_dict = _fake_to_dict  # type: ignore[attr-defined]


async def _run() -> int:
    _install_fakes()

    import uvicorn

    from trawl_mcp.http import build_app  # noqa: WPS433 — imported after fakes

    port = _free_port()
    app = build_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server readiness
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn failed to start within 5s"

    url = f"http://127.0.0.1:{port}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    def parse(resp: httpx.Response) -> dict:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            data_parts: list[str] = []
            for line in resp.text.strip().split("\n"):
                if line.startswith("data: "):
                    data_parts.append(line[6:])
                elif line.startswith("data:"):
                    data_parts.append(line[5:])
            return json.loads("\n".join(data_parts))
        return resp.json()

    async with httpx.AsyncClient(timeout=10.0) as client:
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.0.1"},
            },
        }
        r = await client.post(url, json=init_body, headers=headers)
        r.raise_for_status()
        session_id = r.headers.get("mcp-session-id")
        assert session_id, "server did not return Mcp-Session-Id on initialize"
        init_result = parse(r)
        assert "result" in init_result, f"initialize failed: {init_result}"

        notify_body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        r = await client.post(
            url,
            json=notify_body,
            headers={**headers, "Mcp-Session-Id": session_id},
        )
        assert r.status_code in (200, 202), f"initialized notify got {r.status_code}"

        list_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        r = await client.post(
            url,
            json=list_body,
            headers={**headers, "Mcp-Session-Id": session_id},
        )
        r.raise_for_status()
        list_result = parse(r)
        tool_names = [t["name"] for t in list_result["result"]["tools"]]
        assert "fetch_page" in tool_names, f"fetch_page missing from {tool_names}"

        call_body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "fetch_page",
                "arguments": {"url": "https://example.com/", "query": "what is this"},
            },
        }
        r = await client.post(
            url,
            json=call_body,
            headers={**headers, "Mcp-Session-Id": session_id},
        )
        r.raise_for_status()
        call_result = parse(r)
        content = call_result["result"]["content"]
        payload = json.loads(content[0]["text"])
        assert payload["ok"] is True, f"call failed: {payload}"
        assert payload["n_chunks_returned"] == 1
        assert "fake chunk for what is this" in payload["chunks"][0]["text"]

    server.should_exit = True
    thread.join(timeout=5)

    print("OK: trawl-mcp HTTP transport smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
