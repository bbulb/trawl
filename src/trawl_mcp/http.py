"""Streamable-HTTP MCP transport for trawl.

Mounts the existing trawl `server` instance (from trawl_mcp.server) on a
minimal Starlette app under /mcp using StreamableHTTPSessionManager from
the official MCP Python SDK. This is the streamable-HTTP transport
used by many HTTP-only MCP clients (e.g. the Notion MCP server uses
the same one).

Entry point:
    python -m trawl_mcp --http [HOST:PORT]

Default bind: 127.0.0.1:8765. Pass 0.0.0.0:8765 in Docker.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from trawl_mcp.server import server

logger = logging.getLogger("trawl_mcp.http")


class _NormalizeMcpPath:
    """Rewrite bare /mcp to /mcp/ before routing.

    Starlette's Mount("/mcp") redirects /mcp (no trailing slash) to /mcp/
    with a 307.  The MCP SDK client (httpx) does not follow redirects, so
    POST /mcp would fail.  This middleware normalises the path so both /mcp
    and /mcp/ reach the session manager without a redirect round-trip.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def build_app() -> Starlette:
    """Build the Starlette ASGI app hosting the MCP streamable-HTTP endpoint."""
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,
        json_response=False,
        stateless=False,
        session_idle_timeout=1800,
    )

    async def handle(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            logger.info("trawl-mcp HTTP session manager started")
            yield
            logger.info("trawl-mcp HTTP session manager shutting down")

    return Starlette(
        debug=False,
        routes=[Mount("/mcp", app=handle)],
        middleware=[Middleware(_NormalizeMcpPath)],
        lifespan=lifespan,
    )


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start uvicorn serving the MCP HTTP app. Blocks until SIGINT/SIGTERM."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info("trawl-mcp starting (HTTP transport) on %s:%d", host, port)
    uvicorn.run(build_app(), host=host, port=port, log_level="info")
