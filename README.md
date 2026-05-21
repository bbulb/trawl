# trawl

[![CI](https://github.com/bbulb/trawl/actions/workflows/ci.yml/badge.svg)](https://github.com/bbulb/trawl/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Selective web content extraction for AI agents.** Give trawl a URL
and a natural-language query; it fetches the page, extracts the main
content, chunks it, embeds the chunks with a local bge-m3 model, and
returns only the handful most relevant to the query.

The point is to let an agent "read a web page" by reading only the
~1,000 tokens that matter, instead of dumping 50k+ tokens of page
content into its context.

```python
from trawl import fetch_relevant

r = fetch_relevant("https://en.wikipedia.org/wiki/Yi_Sun-sin",
                   "who did Yi Sun-sin defeat at Myeongnyang")
for c in r.chunks:
    print(f"[{c['score']:.2f}] {c['heading']}\n    {c['text'][:120]}")
```

## Why trawl?

Most "read this page" tools fall into two camps:

1. **Full-page dumpers** (Jina Reader, Firecrawl markdown) — faithful
   but dump the entire page into your context window. A 50k-token
   documentation page becomes 50k tokens of input regardless of what
   you actually wanted to know.
2. **LLM-driven extractors** (Firecrawl `/extract`) — ask an LLM to
   pull structured fields, which needs a strong model, is slow, and
   still ships the full page to the model internally.

trawl takes a different angle: **query-aware dense retrieval over the
extracted markdown**. The heavy lifting is a small, fast local
embedding model (bge-m3), not an LLM. You get back the 5-12 chunks
that matter for your query, at ~1k tokens of output.

### Benchmark vs Jina Reader (12 cases)

| Mode | Avg tokens returned | vs Jina | Ground-truth pass |
|---|---|---|---|
| trawl-base | **1,177** | 23× fewer | 11/12 |
| trawl-cached (with profile) | **1,004** | 30× fewer | 10/11 |
| Jina Reader | 27,506 | (baseline) | 12/12 |

trawl wins on every token-efficiency axis and runs entirely on your
own infrastructure. In exchange you pay a real cost elsewhere:

### External: WCXB dev (1,497 pages)

Beyond the internal 15-case parity matrix, trawl's extraction stage is
cross-validated against the [WCXB](https://github.com/Murrough-Foley/web-content-extraction-benchmark)
public benchmark (CC-BY-4.0, 1,497 dev pages across 7 page types).

| Extractor                         |   F1   |
|-----------------------------------|--------|
| trawl (`html_to_markdown`)        |  0.777 |
| Trafilatura (same environment)    |  0.750 |

Per-page-type breakdown and error counts: see
[`benchmarks/wcxb/README.md`](benchmarks/wcxb/README.md) and run the
benchmark locally to regenerate.

### When *not* to use trawl

- **You want the whole page verbatim.** Selective retrieval is the
  point; if your downstream task needs faithful full-page markdown
  (archival, translation, full-text search indexing), Jina Reader or
  Firecrawl's markdown mode is the right tool.
- **Low-friction setup matters more than token efficiency.** Jina is
  `curl https://r.jina.ai/<url>` — one HTTP call, no local state.
  trawl needs a Python environment, Chromium via Playwright, and a
  running bge-m3 embedding server you host yourself.
- **Latency-sensitive first-visit calls.** Jina's CDN ~3s vs trawl's
  ~9s on the first fetch (Playwright + stealth + embedding). With a
  cached profile trawl's subsequent fetches to the same host drop,
  but the first visit is always slower.
- **Sites behind active anti-bot** (Cloudflare Turnstile with
  proof-of-work, DataDome). trawl's local playwright-stealth defeats
  passive JS challenges only; commercial services that pay for
  anti-bot infrastructure will get those pages where trawl can't.
- **No query, just "read this".** trawl requires a query to rank
  against (unless a cached profile exists). For "summarise whatever
  this page is about", a full-page dumper is a better fit.

## What's in the box

- **Adaptive fetcher routing** — API-first fetchers for YouTube,
  Wikipedia, Stack Exchange, GitHub, and arXiv PDFs; Playwright +
  playwright-stealth fallback for everything else.
- **Three-way extraction** — Trafilatura (precise + recall) and
  BeautifulSoup heuristics race; the longest result wins. This covers
  articles, pricing pages, and lists without per-site rules.
- **Heading-aware chunker** — preserves heading context on every
  chunk and keeps tables intact. Falls back to sentence-level
  chunking for PDF-style single-blob inputs.
- **Repeating-record chunking** — when the rendered DOM contains a
  run of sibling elements with the same structural signature (job
  listings, news cards, product rows), each record becomes its own
  atomic chunk so retrieval ranks them individually instead of
  fragmenting mid-record.
- **Raw passthrough for JSON / XML / RSS / Atom** — URLs with those
  suffixes (or endpoints that answer `Content-Type: application/json`
  on a HEAD probe) are returned byte-for-byte up to
  `TRAWL_PASSTHROUGH_MAX_BYTES` (default 256 KB). No embedding, no
  query required.
- **bge-m3 dense retrieval** with an OpenAI-compatible embedding
  endpoint. Adaptive top-k based on page size. If the embedding
  endpoint is unavailable, trawl falls back to BM25 lexical ranking
  and returns a warning in the result payload instead of failing the
  entire fetch. This degraded mode is meant for operational
  continuity; quality is best with the bge-m3 embedding service
  running.
- **Cross-encoder reranking** (bge-reranker-v2-m3) on the top 2×
  candidates. Falls back gracefully to cosine-only if the reranker
  server is down.
- **Chunk budget for longform pages** (default on). When a page
  produces more chunks than `TRAWL_CHUNK_BUDGET` (default 100), a
  BM25 prefilter keeps the top-N and drops the rest before
  embedding. Cuts retrieval cost on Wikipedia / arXiv / long manpage
  scale pages (~69% `retrieval_ms.p95` reduction on longform
  fixtures; rank-1 identity preserved). Opt out via
  `TRAWL_CHUNK_BUDGET=0`.
- **Optional HyDE query expansion** for queries where the literal
  words don't match the page vocabulary. Off by default.
- **VLM page profiling** (optional) — when the same site is visited
  repeatedly, trawl can ask a vision LLM to propose a CSS selector
  that scopes future fetches to the article region. Cached per host.
- **stdio MCP server** exposing `fetch_page` and `profile_page` tools
  for Claude Code, Claude Desktop, and any MCP-compatible client.

## Project layout

```
src/trawl/                  pipeline library
  pipeline.py               fetch_relevant() entry point
  chunking.py               heading + table preserving chunker
  records.py                repeating-sibling record detection + sentinels
  retrieval.py              bge-m3 cosine retrieval, adaptive k
  reranking.py              bge-reranker-v2-m3 cross-encoder
  extraction.py             Trafilatura + BeautifulSoup three-way
  hyde.py                   optional query expansion
  telemetry.py              opt-in JSONL telemetry
  profiles/                 VLM-based page profiling (optional)
  fetchers/                 per-site API-first adapters
    playwright.py, pdf.py, passthrough.py, youtube.py,
    wikipedia.py, github.py, stackexchange.py

src/trawl_mcp/              MCP server (stdio default, --http opt-in)
tests/                      unit tests + 15-case parity matrix
benchmarks/                 trawl vs Jina, VLM profile eval
examples/                   MCP client config snippets
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design rationale
behind every component, per-case performance, and known limitations.

## Requirements

- Python 3.10+
- Chromium (installed via Playwright)
- A running **bge-m3** embedding server with an OpenAI-compatible
  `/v1/embeddings` endpoint. The reference setup is [`llama-server`](https://github.com/ggerganov/llama.cpp/tree/master/tools/server)
  loaded with a bge-m3 GGUF, listening on `http://localhost:8081`.
  Any OpenAI-compatible embedding endpoint works if you override
  `TRAWL_EMBED_URL`.

Optional:

- bge-reranker-v2-m3 on `:8083` for cross-encoder reranking
  (graceful fallback if absent)
- A small utility LLM on `:8082` for HyDE (off by default)
- A vision LLM on `:8080` for `profile_page` (only needed if you use
  the profiling feature)

### Reference `llama-server` commands

The reference local setup runs four `llama-server` processes. These are
the canonical flag sets validated against the parity matrix and the
coding `agent_patterns` shard. Adjust `-ngl`, `--ctx-size`, and
`--parallel` to your hardware.

```bash
# :8081 — bge-m3 embeddings (REQUIRED for retrieval)
llama-server -m ~/models/bge-m3-Q8_0.gguf \
  --embeddings --pooling cls \
  --port 8081 -ngl 99 --ctx-size 8192 -ub 2048 -b 2048

# :8083 — bge-reranker-v2-m3 cross-encoder (optional, graceful fallback)
llama-server -m ~/models/bge-reranker-v2-m3-Q8_0.gguf \
  --reranking --pooling rank \
  --port 8083 -ngl 99 --ctx-size 65536 \
  --parallel 4 -ub 2048 -b 2048

# :8082 — small utility LLM for HyDE (optional, off by default)
llama-server -m ~/models/gemma-3-4b-it-Q4_K_M.gguf \
  --port 8082 -ngl 99 --ctx-size 4096

# :8080 — vision LLM for profile_page (optional, manual-trigger only)
llama-server -m ~/models/<vision-model>.gguf --mmproj ~/models/<mmproj>.gguf \
  --port 8080 -ngl 99 --ctx-size 8192
```

## Install

The reference setup uses a dedicated conda/mamba environment
(`environment.yml` creates it):

```bash
mamba env create -f environment.yml    # creates `trawl` env with deps
mamba run -n trawl playwright install chromium
```

Copy `.env.example` → `.env` if you need to override any default
endpoints; every variable is optional.

All commands below assume the `trawl` mamba environment: either activate
it with `mamba activate trawl` or prefix commands with
`mamba run -n trawl`.

### Runtime health check

Use `trawl-doctor` to check the local runtime before wiring trawl into
an MCP client:

```bash
trawl-doctor
# or
python -m trawl.diagnostics --json
```

The doctor checks Python, Playwright Chromium, cache-path writability,
the embedding endpoint, the optional reranker endpoint, and optional
VLM profile configuration. Embedding is required for dense retrieval;
reranker and VLM are optional.

## Usage

### As a Python library

```python
from trawl import fetch_relevant

result = fetch_relevant(
    "https://ko.wikipedia.org/wiki/이순신",
    "이순신 직업 생년월일 주요 업적",
)

print(f"fetcher={result.fetcher_used}  latency={result.total_ms}ms")
print(f"compression={result.compression_ratio}x")
for chunk in result.chunks:
    print(f"[{chunk['score']:.3f}] {chunk['heading']}")
    print(f"    {chunk['text'][:200]}")
```

`fetch_relevant` never raises. On failure it returns a `PipelineResult`
with an empty `chunks` list and a non-empty `error` — check
`result.error` before consuming `result.chunks`.

### As an MCP server (stdio)

```bash
python -m trawl_mcp
# or, if the console script is on PATH:
trawl-mcp
```

The server exposes two tools:

**`fetch_page`** — retrieval.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | string | yes | — | Target URL. `.pdf` URLs or URLs containing `/pdf/` route through the PDF path |
| `query` | string | no | — | The user's question/topic. Required when no cached profile exists, unless `auto_profile=true` is used |
| `k` | integer | no | adaptive | Override top-k. Default is adaptive (5–12) by chunk count |
| `use_hyde` | boolean | no | `false` | Expand the query via a hypothetical answer before embedding. Rarely helpful; costs ~15–20s |
| `use_rerank` | boolean | no | `true` | Cross-encoder reranking via bge-reranker-v2-m3. ~0.5–2s extra latency |
| `auto_profile` | boolean | no | `false` | For queryless HTML fetches with no usable profile, generate a VLM profile first and retry via the profile fast path. Requires `TRAWL_VLM_URL`; adds profile-page latency |

Returns a JSON blob as `TextContent`:

```json
{
  "url": "...",
  "query": "...",
  "fetcher": "playwright+trafilatura",
  "ok": true,
  "error": null,
  "warnings": [],
  "page_chars": 55423,
  "output_chars": 3453,
  "compression_ratio": 16.1,
  "n_chunks_total": 175,
  "n_chunks_returned": 10,
  "total_ms": 10612,
  "chunks": [
    {"heading": "…", "text": "…", "score": 0.78}
  ]
}
```

MCP `fetch_page` separates browser-free work from browser work. URLs
that can use raw passthrough, direct PDF fetching, or a native API
fetcher start on a small general thread pool. Playwright-rendered pages,
cached-profile fetches, host-profile transfer, and `profile_page` stay
on one browser thread to avoid sync-Playwright greenlet thread switches.
If a native API fetcher discovers it needs a browser fallback, the MCP
handler retries that call on the browser thread.

**`profile_page`** — VLM-driven page profiling. Takes a screenshot,
asks a vision LLM to identify the main-content region, and caches the
resulting CSS selector keyed by host. Subsequent `fetch_page` calls on
the same host scope extraction to that region, which further reduces
token output on structured pages (finance, news feeds, schedules).

### Wiring into a client

Ready-to-use config snippets in [`examples/`](examples/):

- [`examples/claude_code_config.json`](examples/claude_code_config.json)
  — drop into Claude Code's `mcp_servers.json`
- [`examples/mcp_gateway_config.yaml`](examples/mcp_gateway_config.yaml)
  — example entry for an mcp-gateway style HTTP config

## Configuration

All environment variables are optional. Defaults target a reference
llama-server layout with specific GGUF filenames — **override
`TRAWL_*_MODEL` to match whatever you actually loaded** (llama.cpp
expects the filename you passed to `-m`). Complete list in
[`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `TRAWL_EMBED_URL` | `http://localhost:8081/v1` | bge-m3 embedding endpoint |
| `TRAWL_EMBED_MODEL` | `bge-m3-Q8_0.gguf` | Embedding model name |
| `TRAWL_EMBED_CACHE_TTL` | `3600` | Document embedding cache TTL in seconds. `0` disables the cache. |
| `TRAWL_EMBED_CACHE_PATH` | `~/.cache/trawl/embeddings` | Directory for cached document embedding vectors. |
| `TRAWL_EMBED_CACHE_MAX_MB` | `512` | Soft size cap for the embedding cache; old entries are trimmed by mtime. |
| `TRAWL_RERANK_URL` | `http://localhost:8083/v1` | bge-reranker-v2-m3 endpoint |
| `TRAWL_RERANK_MODEL` | `bge-reranker-v2-m3` | Reranker model name |
| `TRAWL_HYDE_URL` | `http://localhost:8082/v1` | Small utility LLM for HyDE |
| `TRAWL_HYDE_MODEL` | `gemma-4-E4B-it-Q8_0.gguf` | HyDE model name |
| `TRAWL_HYDE_SLOT` | *(unset)* | Pin HyDE to a llama-server slot for KV-cache reuse |
| `TRAWL_CONTEXTUAL_RETRIEVAL` | `0` | `0` disables contextual retrieval, `1` forces deterministic page/section context for dense and BM25 retrieval inputs, and `auto` enables it for identifier/code-heavy queries, large pages, and repeated-record pages. Output chunks are unchanged. |
| `TRAWL_CONTEXT_PREFIX_MAX_CHARS` | `320` | Maximum characters of contextual prefix per chunk before the chunk body is appended. |
| `TRAWL_CONTEXT_PREFIX_VERSION` | `deterministic-v1` | Prefix version string used for contextual embedding cache invalidation. |
| `TRAWL_MCP_GENERAL_WORKERS` | `4` | Worker count for MCP browser-free `fetch_page` routes. Browser/profile routes remain pinned to one Playwright-safe worker. |
| `TRAWL_FETCH_CACHE_TTL` | `300` | Per-URL fetch cache TTL in seconds. `0` disables the fetch cache. |
| `TRAWL_FETCH_CACHE_PATH` | `~/.cache/trawl/fetches` | Directory for cached successful HTML/PDF/API fetch output. |
| `TRAWL_FETCH_CACHE_MAX_MB` | `100` | Soft size cap for the fetch cache; old entries are trimmed by mtime. |
| `TRAWL_FETCH_CACHE_REVALIDATE_TIMEOUT` | `10` | Conditional revalidation timeout for stale records with `ETag`/`Last-Modified`. |
| `TRAWL_SCRAPLING_FALLBACK` | `0` | Enable optional Scrapling fallback after Playwright fails or returns unusable/anti-bot content. Requires `.[scrapling]`. |
| `TRAWL_SCRAPLING_MODE` | `auto` | Scrapling mode: `auto`, `dynamic`, or `stealthy`. `auto` uses stealthy only for anti-bot-looking failures. |
| `TRAWL_SCRAPLING_TIMEOUT_MS` | `30000` | Scrapling fallback timeout in milliseconds. |
| `TRAWL_VLM_URL` | `http://localhost:8080/v1` | Vision LLM for page profiling |
| `TRAWL_VLM_MODEL` | `gemma` | Vision model name |
| `TRAWL_VLM_TIMEOUT` | `120` | VLM request timeout (seconds) |
| `TRAWL_VLM_MAX_TOKENS` | `2048` | VLM max output tokens |
| `TRAWL_VLM_SLOT` | *(unset)* | Pin VLM to a llama-server slot |

> **Why HyDE targets `:8082` instead of `:8080`:** on shared
> llama-servers the main endpoint is often servicing another
> consumer (e.g. a chat agent with long tool loops). Pointing HyDE
> at a dedicated small-utility endpoint avoids slot contention.
> See [`ARCHITECTURE.md#why-is-hyde-off-by-default`](ARCHITECTURE.md#why-is-hyde-off-by-default).
>
> **Slot pinning**: on shared servers with prompt caching enabled,
> set `TRAWL_VLM_SLOT` / `TRAWL_HYDE_SLOT` to a slot ID integer to
> avoid evicting other consumers' KV cache.

The document embedding cache is on by default with a 1-hour TTL.
Disk usage is capped by `TRAWL_EMBED_CACHE_MAX_MB` (default 512 MB)
with LRU trimming. To extend the window across a longer agent
session:

```bash
export TRAWL_EMBED_CACHE_TTL=86400
```

To disable entirely:

```bash
export TRAWL_EMBED_CACHE_TTL=0
```

The cache key includes model, endpoint, contextual-retrieval
mode/version, and a hash of the text, so content changes naturally
miss the cache.

The per-fetch cache stores successful fetch output plus optional
`ETag`, `Last-Modified`, and content-hash metadata. When a cached record
is stale and has validators, trawl sends a conditional request: `304`
refreshes the cache timestamp and reuses the stored markdown, while
`200` or missing validators falls through to a normal fresh fetch.

For harder protected or JavaScript-heavy pages, Scrapling can be enabled
as a recovery-only HTML supplier:

```bash
pip install -e '.[scrapling]'
export TRAWL_SCRAPLING_FALLBACK=1
```

This does not change the default install. trawl still uses its existing
API/PDF/passthrough/Playwright routes first, and then runs its own
extraction, chunking, retrieval, and reranking on Scrapling-supplied HTML.

To measure cold versus warm repeated retrieval with the reader-comparison
benchmark:

```bash
mamba run -n trawl python benchmarks/reader_comparison.py \
  --provider trawl \
  --repeat 2 \
  --warm-repeat-embed-cache-ttl 86400
```

The generated report and CSV include cold/warm phase, retrieval latency,
fetch cache hit, chunk budget count, and document embedding cache hit/miss
counters for `trawl` rows. For a strict cold first pass, point
`TRAWL_EMBED_CACHE_PATH` at an empty directory before running.

To re-measure retrieval modes under the same cache setting:

```bash
mamba run -n trawl python benchmarks/reader_comparison.py \
  --provider trawl \
  --repeat 2 \
  --warm-repeat-embed-cache-ttl 86400 \
  --retrieval-mode dense \
  --retrieval-mode hybrid \
  --retrieval-mode contextual-auto \
  --retrieval-mode contextual-forced
```

Retrieval-mode runs set `TRAWL_HYBRID_RETRIEVAL` and
`TRAWL_CONTEXTUAL_RETRIEVAL` only around each `trawl` benchmark call. The
report compares each mode to dense by case/repeat, including
query type, flipped-to-fail count, first-fact rank movement, retrieval
p50/p95, output tokens, contextual-use flags, and the embedding-cache TTL
used for the run.
Default retrieval settings stay unchanged unless a measured run has zero
flipped-to-fail rows and retrieval p95 grows by no more than 20%.

Optional reader-comparison providers:

- Firecrawl: set `FIRECRAWL_API_KEY`; unavailable credentials produce
  skipped rows.
- Crawl4AI: install the optional package in your environment; unavailable
  imports produce skipped rows.

## Testing

```bash
# Offline unit tests (CI runs these)
pytest tests/test_profiles.py tests/test_profile_transfer.py

# Parity matrix: 12 end-to-end cases, requires live bge-m3 endpoint
python tests/test_pipeline.py
python tests/test_pipeline.py --only kbo_schedule --verbose

# MCP stdio smoke test
python tests/test_mcp_server.py
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full dev workflow.

## Known limitations

- **Active anti-bot** (Cloudflare Turnstile with proof-of-work,
  DataDome) defeats trawl. Passive JS challenges (Stack Overflow
  tier) work via playwright-stealth at a ~10–20s latency cost.
- **Serial fetching** — a module-level browser lock. Multi-tenant
  deployments need a browser pool.
- **PDF OCR** is not supported; scanned-only PDFs return empty chunks.
- **Auth / paywall pages** return the login page, not the content.

See [`ARCHITECTURE.md#known-limitations`](ARCHITECTURE.md#known-limitations)
for details and workarounds.

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — design rationale, measured
  performance, per-component trade-offs
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — dev setup, test workflow,
  how to add a fetcher
- **[CHANGELOG.md](CHANGELOG.md)** — version history
- **[CLAUDE.md](CLAUDE.md)** — project rules for Claude Code sessions
  working in this directory

## License

MIT. See [`LICENSE`](LICENSE).
