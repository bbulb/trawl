"""trawl-mcp — MCP server exposing trawl's fetch_page tool over stdio.

Entry point: `python -m trawl_mcp` starts a stdio MCP server that any
MCP client (Claude Code, Claude Desktop, any mcp-gateway style
client, …) can connect to.

Exposed tools:
    fetch_page(url: str, query: str, k?: int)
        Fetch a web page or PDF, return the top-k chunks most relevant
        to `query`. See src/trawl/pipeline.py for the pipeline details.
"""
