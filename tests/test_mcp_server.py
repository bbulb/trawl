"""Smoke test for the trawl-mcp stdio server.

Spawns `python -m trawl_mcp` as a subprocess, speaks the MCP protocol to
it, and verifies:
    - the server initialises
    - `tools/list` returns `fetch_page`
    - `tools/call` on `fetch_page` with a trivial URL returns chunks

Invoke:
    python tests/test_mcp_server.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class _PassthroughHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        body = b'{"mcp": "passthrough"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_local_server() -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PassthroughHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server


async def run() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trawl_mcp"],
    )

    print("→ starting trawl-mcp subprocess…")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("→ initialising session")
            await session.initialize()

            print("→ listing tools")
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"   tools = {tool_names}")
            assert "fetch_page" in tool_names, f"fetch_page missing from {tool_names}"
            fetch_page_tool = next(t for t in tools_result.tools if t.name == "fetch_page")
            assert fetch_page_tool.inputSchema is not None, "fetch_page has no input schema"
            print(
                f"   input schema properties: {list(fetch_page_tool.inputSchema.get('properties', {}).keys())}"
            )

            print("→ calling fetch_page on example.com")
            call_result = await session.call_tool(
                "fetch_page",
                {
                    "url": "https://example.com/",
                    "query": "what is this domain for",
                },
            )
            assert call_result.content, "empty content returned"
            first = call_result.content[0]
            assert first.type == "text", f"expected text content, got {first.type}"
            payload = json.loads(first.text)
            print(f"   payload keys: {sorted(payload.keys())}")
            assert payload["ok"] is True, f"call failed: {payload.get('error')}"
            assert payload["n_chunks_returned"] >= 1, "no chunks returned"
            print(
                f"   n_chunks_returned={payload['n_chunks_returned']}  "
                f"total_ms={payload['total_ms']}  "
                f"fetcher={payload.get('fetcher_used', payload.get('fetcher'))}"
            )
            print(f"   first chunk text[:120]: {payload['chunks'][0]['text'][:120]!r}")

            base, server = _start_local_server()
            try:
                print("→ calling fetch_page on local JSON endpoint")
                call_result = await session.call_tool(
                    "fetch_page",
                    {"url": f"{base}/data.json"},
                )
                assert call_result.content, "empty content returned"
                payload = json.loads(call_result.content[0].text)
                print(f"   passthrough payload keys: {sorted(payload.keys())}")
                assert payload["ok"] is True, f"passthrough call failed: {payload.get('error')}"
                assert payload["path"] == "raw_passthrough", payload.get("path")
                assert payload["content_type"] == "application/json"
                assert payload["truncated"] is False
                assert payload["chunks"][0]["text"] == '{"mcp": "passthrough"}'
            finally:
                server.shutdown()
                server.server_close()

    print("\nOK: trawl-mcp stdio server smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
