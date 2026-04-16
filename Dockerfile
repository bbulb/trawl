# trawl MCP server — HTTP transport, for HTTP-only MCP clients.
# Base image ships chromium + runtime libs pre-installed for Playwright.
# The tag version MUST match the `playwright==` pin in pyproject.toml —
# the base image's /ms-playwright/ browsers only work with the matching
# Python package revision. Bump both together.
FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Install Python deps first so source changes do not invalidate the dep layer.
# Stub packages let `pip install -e .` resolve deps before real source is copied.
COPY pyproject.toml README.md ./
RUN mkdir -p src/trawl src/trawl_mcp && \
    touch src/trawl/__init__.py src/trawl_mcp/__init__.py && \
    pip install --no-cache-dir -e .

# Real source — only this layer rebuilds on code changes.
COPY src ./src

# Chromium + runtime libs are already in the base image at /ms-playwright.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# trawl runtime config — inject via `docker run -e ...`, compose
# `environment:`, or `--env-file .env`. Not baked into the image so the
# same image works across local-dev, LAN llama-servers, and remote hosts.
# See .env.example for the full list.
#
# Required:
#   TRAWL_EMBED_URL    e.g. http://host.docker.internal:8081/v1
#   TRAWL_EMBED_MODEL  e.g. bge-m3
#
# Optional (feature degrades or is unused when absent):
#   TRAWL_RERANK_URL / TRAWL_RERANK_MODEL   — cross-encoder reranker;
#                                             falls back to cosine-only
#   TRAWL_HYDE_URL   / TRAWL_HYDE_MODEL     — HyDE query expansion (off by default)
#   TRAWL_VLM_URL    / TRAWL_VLM_MODEL      — required for profile_page;
#                                             unset = tool hidden from MCP list
#   TRAWL_PASSTHROUGH_MAX_BYTES             — default 262144 (256 KB)
#   TRAWL_HYDE_SLOT / TRAWL_VLM_SLOT        — llama-server slot pinning
#
# Profile/visit cache is persisted at /root/.cache/trawl via VOLUME below.
# Mount from host to retain state across container lifecycle:
#   docker run -v ~/.cache/trawl:/root/.cache/trawl ...

EXPOSE 8765

VOLUME ["/root/.cache/trawl"]

CMD ["python", "-m", "trawl_mcp", "--http", "0.0.0.0:8765"]
