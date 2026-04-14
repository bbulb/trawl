"""Entry point for trawl-mcp.

Default: stdio MCP transport (as spawned by Claude Desktop / Claude Code).
With --http: streamable-HTTP transport (for HTTP-only MCP clients).

Usage:
    python -m trawl_mcp                      # stdio
    python -m trawl_mcp --http               # HTTP on 127.0.0.1:8765
    python -m trawl_mcp --http 0.0.0.0:8765  # HTTP bound to all interfaces
"""

from __future__ import annotations

import sys


def _parse_http_arg(argv: list[str]) -> tuple[str, int] | None:
    if "--http" not in argv:
        return None
    i = argv.index("--http")
    host, port = "127.0.0.1", 8765
    if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
        spec = argv[i + 1]
        if ":" in spec:
            host_s, port_s = spec.rsplit(":", 1)
            host = host_s or host
            port = int(port_s)
        else:
            port = int(spec)
    return host, port


def main() -> None:
    http_bind = _parse_http_arg(sys.argv[1:])
    if http_bind is not None:
        from trawl_mcp.http import run as run_http

        run_http(host=http_bind[0], port=http_bind[1])
        return

    from trawl_mcp.server import _cli_entry

    _cli_entry()


if __name__ == "__main__":
    main()
