# trawl MCP server — HTTP transport, for HTTP-only MCP clients.
# Base image ships chromium + runtime libs pre-installed for Playwright.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

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

# trawl expects these at runtime. Compose overrides these at service
# definition time to point at the host llama-servers.
ENV TRAWL_EMBED_URL=http://host.docker.internal:8081/v1
ENV TRAWL_EMBED_MODEL=bge-m3-Q8_0.gguf
ENV TRAWL_RERANK_URL=http://host.docker.internal:8083/v1
ENV TRAWL_RERANK_MODEL=bge-reranker-v2-m3
ENV TRAWL_HYDE_URL=http://host.docker.internal:8082/v1
ENV TRAWL_HYDE_MODEL=gemma-4-E4B-it-Q8_0.gguf
ENV TRAWL_VLM_URL=http://host.docker.internal:8080/v1
ENV TRAWL_VLM_MODEL=gemma

EXPOSE 8765

VOLUME ["/root/.cache/trawl"]

CMD ["python", "-m", "trawl_mcp", "--http", "0.0.0.0:8765"]
