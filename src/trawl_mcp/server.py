"""Stdio/HTTP MCP server exposing trawl's fetch_page and profile_page tools.

The pipeline uses sync_playwright internally, which can't run inside an
asyncio event loop on its own. We run each pipeline invocation in a worker
thread via `asyncio.to_thread`, keeping the MCP server responsive.
"""

from __future__ import annotations

import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from trawl import fetch_relevant, to_dict

logger = logging.getLogger("trawl_mcp")

server: Server = Server("trawl")


FETCH_PAGE_DESCRIPTION = (
    "Fetch a web page or PDF and return the content most relevant to a "
    "natural-language query, or — if a cached extraction profile exists for "
    "the URL — the main content subtree directly without embedding. The "
    "profile fast path skips the bge-m3 retrieval step entirely when the "
    "subtree is small (<=20 chunks by default), which makes 'what's on this "
    "page' style queries work without a specific search term. When no profile "
    "exists, behaves like the original retrieval-only pipeline and requires "
    "a query. After 3+ visits to a URL without a profile, the response "
    "includes suggest_profile=true as a hint that calling profile_page on "
    "this URL would speed up future calls. Handles PDFs automatically (URL "
    "ending in .pdf or /pdf/). Cloudflare-protected sites work via "
    "playwright-stealth but may take an extra 10-20s."
)

PROFILE_PAGE_DESCRIPTION = (
    "Generate a reusable extraction profile for a URL via visual LLM "
    "analysis. The profile identifies the page's main content region as a "
    "CSS selector plus verification anchors and caches it to disk. "
    "Subsequent fetch_page calls on the same URL will take the profile fast "
    "path and skip embedding entirely for small-to-medium pages. Call this "
    "when fetch_page returns suggest_profile=true, or when you expect to "
    "revisit a URL multiple times. Profile generation takes ~10-20 seconds "
    "and uses the local vision LLM (default: http://localhost:8080)."
)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="fetch_page",
            description=FETCH_PAGE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch. HTTPS recommended. "
                        "URLs ending in .pdf or containing /pdf/ "
                        "are routed through the PDF path.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional. If a profile exists for this "
                        "URL, the profiled main-content subtree "
                        "is returned regardless of query. If no "
                        "profile exists, query is required and "
                        "drives embedding-based top-k retrieval "
                        "over the whole page. Any language "
                        "supported by bge-m3 (100+ including "
                        "Korean, Japanese, English).",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Top-k override for retrieval. Default: "
                        "adaptive by chunk count.",
                    },
                    "use_hyde": {
                        "type": "boolean",
                        "description": "Enable HyDE query expansion "
                        "(off by default; rarely needed).",
                    },
                    "use_rerank": {
                        "type": "boolean",
                        "description": "Enable cross-encoder reranking "
                        "(on by default). Improves precision "
                        "at ~0.5-2s extra latency.",
                    },
                },
            },
        ),
        Tool(
            name="profile_page",
            description=PROFILE_PAGE_DESCRIPTION,
            inputSchema={
                "type": "object",
                "required": ["url"],
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to profile.",
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "default": False,
                        "description": "Regenerate even if a cached profile exists.",
                    },
                },
            },
        ),
    ]


def _error_response(message: str) -> list[TextContent]:
    """Build the MCP TextContent response for an error case."""
    return [
        TextContent(
            type="text",
            text=json.dumps({"ok": False, "error": message}, ensure_ascii=False),
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "fetch_page":
        return await _call_fetch_page(arguments)
    if name == "profile_page":
        return await _call_profile_page(arguments)
    return _error_response(f"unknown tool: {name}")


async def _call_fetch_page(arguments: dict) -> list[TextContent]:
    url = arguments.get("url")
    query = arguments.get("query")  # may be None
    k = arguments.get("k")
    use_hyde = bool(arguments.get("use_hyde", False))
    use_rerank = bool(arguments.get("use_rerank", True))
    if not url:
        return _error_response("url is required")

    logger.info(
        "fetch_page url=%s query=%r k=%s hyde=%s rerank=%s",
        url,
        query,
        k,
        use_hyde,
        use_rerank,
    )
    result = await asyncio.to_thread(
        fetch_relevant,
        url,
        query,
        k=k,
        use_hyde=use_hyde,
        use_rerank=use_rerank,
    )
    payload = to_dict(result)
    payload["ok"] = not bool(payload.get("error"))
    # Derived counts for agent convenience.
    payload["n_chunks_returned"] = len(payload.get("chunks") or [])
    # Drop oversized fields that aren't useful to agents.
    payload.pop("hyde_text", None)
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def _call_profile_page(arguments: dict) -> list[TextContent]:
    url = arguments.get("url")
    force_refresh = bool(arguments.get("force_refresh", False))
    if not url:
        return _error_response("url is required")

    logger.info("profile_page url=%s force_refresh=%s", url, force_refresh)

    # Lazy import so profile_page only loads the VLM/mapper code path
    # when the tool is actually called.
    from trawl.profiles import generate_profile

    payload = await asyncio.to_thread(
        generate_profile,
        url,
        force_refresh=force_refresh,
    )
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("trawl-mcp starting (stdio transport)")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _cli_entry() -> None:
    """Sync entry point used by the `trawl-mcp` console script."""
    asyncio.run(main())
