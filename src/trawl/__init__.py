"""trawl — selective web content extraction.

The core function of this package is `fetch_relevant(url, query)`, which
fetches a web page (HTML or PDF), extracts the main content, chunks it by
semantic boundaries, and returns only the chunks most relevant to `query`
as ranked by dense embedding similarity.

Typical usage as a Python library:

    >>> from trawl import fetch_relevant
    >>> result = fetch_relevant("https://example.com/", "what is this page about")
    >>> for chunk in result.chunks:
    ...     print(chunk["heading"], chunk["text"])

The same function is exposed as an MCP tool by `trawl_mcp`:

    $ python -m trawl_mcp  # starts the stdio MCP server

See README.md for configuration (embedding server URL, timeouts) and the
list of supported page types.
"""

from .pipeline import PipelineResult, fetch_relevant, to_dict

__all__ = ["fetch_relevant", "PipelineResult", "to_dict"]
__version__ = "0.1.0"
