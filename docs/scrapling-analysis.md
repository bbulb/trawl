# Scrapling Project Analysis

Date: 2026-04-27

Source project: <https://github.com/D4Vinci/Scrapling>

## Executive Summary

Scrapling is an adaptive web scraping framework. Its center of gravity is fetch
and crawl capability: HTTP fetching with browser impersonation, Playwright-based
dynamic rendering, stealth browser sessions, proxy/session management, adaptive
selector relocation, a spider framework, a CLI, and an MCP server.

trawl is a selective reading and retrieval pipeline for AI agents. Its center of
gravity is post-fetch processing: extraction, chunking, local embedding search,
optional reranking, profile-assisted main-content scoping, and an MCP tool that
returns only query-relevant evidence.

The projects overlap in the fetcher, selector, and MCP surface area. They do not
solve the same core problem. Scrapling is a strong candidate as an optional
fetch/render backend for trawl, especially for protected or JavaScript-heavy
sites. It is not a direct replacement for trawl's query-aware retrieval layer.

Recommended first integration path: add Scrapling as an optional fallback fetcher
used only when the current Playwright fetcher fails, times out, or detects a
likely anti-bot response. Keep the existing trawl pipeline, chunking, retrieval,
reranking, and MCP output contract unchanged.

## Current Facts Checked

Scrapling repository and release state checked on 2026-04-27:

- Repository: <https://github.com/D4Vinci/Scrapling>
- Latest release observed: `v0.4.7`, published 2026-04-17.
- Package name: `scrapling`
- Python requirement: `>=3.10`
- License: BSD-3-Clause
- Main package layout observed on GitHub:
  - `scrapling/parser.py`
  - `scrapling/core/`
  - `scrapling/fetchers/`
  - `scrapling/engines/`
  - `scrapling/spiders/`
- Fetcher modules observed:
  - `scrapling/fetchers/requests.py`
  - `scrapling/fetchers/chrome.py`
  - `scrapling/fetchers/stealth_chrome.py`
- MCP implementation observed in `scrapling/core/ai.py`.

Scrapling `pyproject.toml` declares these relevant dependencies:

- Base install: `lxml`, `cssselect`, `orjson`, `tld`, `w3lib`,
  `typing_extensions`.
- `fetchers` extra: `click`, `curl_cffi`, `playwright==1.58.0`,
  `patchright==1.58.2`, `browserforge`, fingerprint data, `msgspec`, `anyio`,
  `protego`.
- `ai` extra: `mcp>=1.27.0`, `markdownify`, and `scrapling[fetchers]`.
- `shell` extra: IPython, `markdownify`, and fetchers.

Compatibility note: trawl currently pins `playwright==1.58.0` and requires
`mcp>=1.27`, so the headline versions align. The optional Scrapling fetcher
dependencies are still materially heavier than trawl's current default install.

## Scrapling Capabilities Relevant to trawl

### 1. Fetcher Classes

Scrapling exposes three main fetcher families:

- `Fetcher` / `AsyncFetcher` / `FetcherSession`
  - HTTP request path based on `curl_cffi`.
  - Browser fingerprint impersonation and HTTP-level scraping features.
  - Best fit for static pages where HTTP is enough.

- `DynamicFetcher` / `DynamicSession` / `AsyncDynamicSession`
  - Playwright-backed browser rendering.
  - Supports JavaScript pages, wait controls, page automation hooks,
    `wait_selector`, `network_idle`, XHR capture, resource blocking, proxy
    rotation, real Chrome, and persistent sessions.
  - Best fit for SPAs and dynamic documentation/product pages.

- `StealthyFetcher` / `StealthySession` / `AsyncStealthySession`
  - Browser path for harder anti-bot sites.
  - Uses stealth/fingerprint tooling and `patchright`.
  - Documentation positions it for Cloudflare and similar protections.
  - Supports persistent sessions and concurrent pages through a page pool.

trawl already has `src/trawl/fetchers/playwright.py`, which launches one shared
headless Chromium browser, applies `playwright-stealth`, waits for text
stability, unwraps a small allow-list of shadow DOM content, and returns rendered
HTML. Scrapling's value here is not basic Playwright rendering; it is the broader
stealth/session/proxy/fingerprint surface that trawl intentionally does not
currently own.

### 2. Adaptive Scraping

Scrapling's adaptive scraping stores unique element properties and later
relocates an element when the original CSS/XPath selector no longer matches.
The documented model is:

1. Enable adaptive mode.
2. Select an element with `auto_save=True`.
3. Later, if the selector stops matching, call the same selector with
   `adaptive=True`.
4. Scrapling compares candidate elements using stored features such as tag name,
   text, attributes, sibling tag names, path tag names, parent tag attributes,
   and parent text.

This overlaps with trawl's profile system only partially.

trawl profiles are page-reader oriented. A vision model proposes a main-content
CSS selector plus verification anchors. The selector scopes future fetches to the
main content region before extraction and retrieval.

Scrapling adaptive scraping is scraper-maintenance oriented. It helps a known
field or element survive markup drift after it has been saved once.

Potential use inside trawl:

- Useful for maintaining a known main-content selector after a profile exists.
- Less useful for discovering the main content region from scratch.
- Could reduce profile drift failures if trawl stored the profiled subtree with
  Scrapling's adaptive storage.

Risk:

- trawl currently treats the profile selector and verification anchors as a
  conservative gate. Scrapling relocation may return a "best similar" element,
  which needs independent validation before using it as evidence.

### 3. MCP Server

Scrapling's MCP server exposes scraping operations directly:

- `get`
- `bulk_get`
- `fetch`
- `bulk_fetch`
- `stealthy_fetch`
- `bulk_stealthy_fetch`
- `open_session`
- `close_session`
- `list_sessions`
- `screenshot`

It supports CSS selector narrowing, extraction type selection, main-content-only
handling, persistent browser sessions, and screenshots as MCP image content.

trawl's MCP server exposes a narrower reading contract:

- `fetch_page`: fetch one page or PDF and return chunks relevant to a natural
  language query.
- `profile_page`: generate and cache a VLM-based selector profile when
  `TRAWL_VLM_URL` is configured.

The MCP products are complementary:

- Scrapling MCP is an operator tool: browse, scrape, inspect, screenshot, keep
  sessions open.
- trawl MCP is a reader/retrieval tool: return compact evidence for a query.

For trawl, it is better to depend on Scrapling as a Python library inside the
pipeline than to call Scrapling's MCP server from trawl's MCP server. MCP-to-MCP
composition would add process orchestration, serialization, and error handling
without improving the trawl API.

### 4. Spider Framework

Scrapling includes a Scrapy-like spider system:

- async `Spider` API with `start_urls` and callbacks;
- scheduler and priority queue;
- concurrency control;
- per-domain throttling and download delays;
- session manager;
- blocked response detection and retry;
- robots.txt support;
- checkpoint-based pause/resume;
- response cache;
- streaming output;
- export helpers.

trawl is currently single-page oriented. It can follow links indirectly through
`outbound_links` and `chain_hints`, but it does not own crawl scheduling,
checkpointing, or a persistent crawl corpus.

Potential use inside trawl:

- Not needed for the current `fetch_relevant(url, query)` contract.
- Relevant if trawl grows a "crawl this docs site and build/reuse a local
  retrieval corpus" feature.
- Scrapling's spider layer could provide crawling mechanics while trawl provides
  extraction, chunking, and ranking for each fetched page.

## Current trawl Architecture Touchpoints

### Pipeline Contract

The main contract is `fetch_relevant(url, query)` in `src/trawl/pipeline.py`.
Its documented flow is:

1. Track visits and possibly suggest profiling.
2. Use a cached profile if available.
3. Otherwise route PDFs, structured passthrough, API-first fetchers, or
   Playwright fallback.
4. Extract HTML to markdown.
5. Chunk markdown.
6. Optionally use HyDE.
7. Retrieve top-k chunks with local bge-m3 embeddings.
8. Optionally rerank with a cross-encoder.

The `PipelineResult` output shape includes:

- URL and query;
- fetcher used;
- timing fields;
- total page characters;
- total and embedded chunk counts;
- returned chunks;
- profile metadata;
- rerank metadata;
- content type/truncation;
- enrichment fields such as excerpts, outbound links, entities, and chain hints;
- retrieval diagnostics.

Any Scrapling integration should preserve this output shape.

### Existing Fetcher Routing

trawl currently has API-first fetchers for:

- YouTube
- GitHub
- Stack Exchange
- Wikipedia

It also has:

- PDF handling;
- raw passthrough for JSON/XML/RSS/Atom;
- Playwright fallback for general HTML;
- per-fetch cache;
- profile transfer and profile fast paths.

Scrapling should not replace API-first fetchers. Those fetchers produce cleaner,
cheaper, more deterministic content for known hosts. Scrapling is most valuable
after those routes fail or do not match.

### Existing Playwright Fetcher

`src/trawl/fetchers/playwright.py` has local behavior that is specific to
trawl's retrieval quality:

- one shared browser holder;
- a single lock around sync Playwright usage;
- short `networkidle` budget with fallback to `domcontentloaded`;
- content-ready wait based on stable visible text length;
- optional profile selector readiness check;
- Korean locale;
- shadow DOM unwrap allow-list for documentation code examples;
- per-host wait ceiling via `host_stats`.

This code is tuned for "return stable HTML for extraction" rather than general
browser automation. A Scrapling backend should either preserve these semantics or
be introduced as a separate fetcher name with measured behavior.

### Extraction and Retrieval

trawl extraction is currently independent of the fetcher backend:

- Trafilatura recall;
- Trafilatura precision;
- BeautifulSoup fallback;
- optional Readability;
- repeating-record annotation;
- score-based candidate selection using query coverage, structure preservation,
  link density, and boilerplate markers.

This means a Scrapling fetcher only needs to supply HTML or markdown plus
provenance. The downstream extraction/ranking path can remain unchanged.

## Integration Options

### Option A: Optional Scrapling Fallback Fetcher

Add `src/trawl/fetchers/scrapling.py` and route to it only when:

- current Playwright fetch returns an error;
- current Playwright fetch returns suspiciously short content;
- current extraction returns no chunks;
- status/body indicates anti-bot interstitial;
- an explicit env var asks for Scrapling, for example
  `TRAWL_SCRAPLING_MODE=auto|off|dynamic|stealthy`.

Suggested behavior:

- Default remains `off` or `auto` depending on release appetite.
- Use `DynamicFetcher` for general JS fallback.
- Use `StealthyFetcher` only when anti-bot is suspected or explicitly enabled.
- Return a trawl `FetchResult` with `fetcher="scrapling-dynamic"` or
  `fetcher="scrapling-stealthy"`.
- Keep Scrapling as an optional dependency, for example
  `pip install -e '.[scrapling]'`.

Pros:

- Smallest change to trawl.
- Preserves current successful paths.
- Directly addresses trawl's current anti-bot limitation.
- Easy to benchmark against existing reader cases.

Cons:

- Adds heavyweight optional dependencies.
- Browser lifecycle may conflict with trawl's existing sync Playwright model if
  used in the same process without care.
- Needs clear timeout and resource limits.

Recommended as the first integration.

### Option B: Replace trawl Playwright Fetcher with Scrapling DynamicFetcher

This would make Scrapling the default general HTML renderer.

Pros:

- Less trawl-owned browser code.
- Gains Scrapling session/proxy/fingerprint knobs.

Cons:

- Higher regression risk.
- trawl's content-ready wait, shadow DOM unwrap, host wait ceilings, and
  profile-selector wait behavior would need to be reimplemented or mapped.
- Scrapling fetcher output is optimized for scraping, not necessarily for
  trawl's extraction scoring.

Not recommended before an A/B benchmark.

### Option C: Use Scrapling Adaptive Storage for trawl Profiles

Store the profiled main-content element through Scrapling's adaptive mechanism,
then ask Scrapling to relocate it when the CSS selector drifts.

Pros:

- Could reduce profile drift when websites change layout.
- Fits Scrapling's differentiating feature.

Cons:

- Requires mapping trawl's profile model to Scrapling storage identifiers.
- Similarity relocation needs trawl verification anchors before trust.
- Adds state and migration complexity.

Worth a later spike after Option A if profile drift is a measured problem.

### Option D: Use Scrapling Spider for Multi-Page trawl

Use Scrapling spiders to crawl a documentation site or domain, then feed each
response through trawl extraction/chunking/retrieval indexing.

Pros:

- Avoids building crawler infrastructure in trawl.
- Gives pause/resume, throttling, robots support, and streaming.

Cons:

- This is a new product mode, not a simple backend swap.
- Requires storage/indexing decisions outside the current single-page pipeline.

Not needed for current trawl. Good candidate if a site-level corpus feature is
planned.

## Recommended First Design

Implement a narrow optional fallback:

1. Add optional dependency group:

   ```toml
   [project.optional-dependencies]
   scrapling = ["scrapling[fetchers]>=0.4.7,<0.5"]
   ```

2. Add `src/trawl/fetchers/scrapling.py`:

   - Import Scrapling lazily.
   - Expose `fetch(url, mode="dynamic" | "stealthy", ...) -> FetchResult`.
   - Convert Scrapling response fields to trawl's `FetchResult`.
   - Catch import errors and return a normal error result.
   - Enforce a trawl timeout independent of Scrapling defaults.

3. Add pipeline env controls:

   - `TRAWL_SCRAPLING_FETCHER=0|1`
   - `TRAWL_SCRAPLING_MODE=dynamic|stealthy|auto`
   - `TRAWL_SCRAPLING_ON_FAILURE=1`

4. Route only after existing fetchers:

   - Keep passthrough/PDF/API-first routes before Scrapling.
   - Try current Playwright first.
   - If Playwright fails or yields unusable extraction, try Scrapling when
     enabled.

5. Add tests with monkeypatched fake Scrapling classes:

   - dependency missing returns graceful error;
   - successful DynamicFetcher response maps to `FetchResult`;
   - pipeline falls back to Scrapling after Playwright failure;
   - disabled env var leaves behavior unchanged.

6. Add benchmarks:

   - existing reader comparison cases;
   - at least one JS-heavy page;
   - at least one known anti-bot/interstitial fixture if an offline fixture can
     be captured.

## Risk Analysis

### Dependency and Install Size

Scrapling fetchers bring `patchright`, browser fingerprint tooling, and browser
install expectations. This is a meaningful increase in setup complexity. Keep it
optional.

### Browser Runtime Conflicts

trawl deliberately runs sync Playwright in a single dedicated worker thread for
MCP calls. Scrapling may manage its own Playwright/Patchright lifecycle. Mixing
both in one process needs testing under `trawl-mcp`, not just direct Python
calls.

Mitigation:

- Keep trawl's MCP single-thread executor.
- Use Scrapling one-shot calls first, not long-lived Scrapling sessions.
- Add integration tests that call through the MCP worker path if possible.

### Output Quality Regression

Scrapling may return a parsed `Response` object and useful selector APIs, but
trawl's retrieval quality depends on the rendered HTML passed into
`trawl.extraction.extract_html`.

Mitigation:

- Prefer raw/rendered HTML from Scrapling response when available.
- Reuse trawl extraction instead of using Scrapling's extracted text as the
  primary path.
- Track `fetcher_used` so benchmark regressions can be attributed.

### Anti-Bot Claims

Scrapling documents Cloudflare-oriented features, but protected sites change
quickly and legal/ethical limits apply. trawl should not promise universal
bypass.

Mitigation:

- Word documentation as "optional stronger protected-site fetcher".
- Keep robots/terms/rate-limit cautions.
- Disable stealth mode by default unless explicitly configured.

### Profile Semantics

Scrapling adaptive scraping can return a best-similarity match. trawl profiles
currently require verification anchors. Do not treat relocated elements as
trusted without verifying anchors or content ratio.

## What Not To Change Initially

- Do not replace bge-m3 retrieval with Scrapling. Scrapling does not provide
  trawl's query-aware dense retrieval/reranking pipeline.
- Do not remove API-first fetchers. They are cheaper and cleaner for known
  domains.
- Do not make Scrapling a required dependency.
- Do not route all pages through `StealthyFetcher`; it is more expensive and
  increases operational risk.
- Do not call Scrapling's MCP server from trawl's MCP server for the first
  integration. Use the Python library if integrating.

## Suggested Evaluation Matrix

Use these criteria before enabling Scrapling by default:

| Area | Metric | Baseline | Target |
|---|---|---:|---:|
| Existing pages | Reader comparison pass count | current benchmark | no regression |
| Token efficiency | returned chars/tokens | current benchmark | no regression |
| Latency | p50/p95 total_ms | current benchmark | acceptable increase only on fallback |
| Failure recovery | pages recovered after Playwright failure | 0 | measurable improvement |
| Install complexity | default install size | current | unchanged |
| MCP stability | repeated `fetch_page` calls | current | no greenlet/thread regressions |

## Source References

External:

- Scrapling GitHub repository: <https://github.com/D4Vinci/Scrapling>
- Scrapling documentation index: <https://scrapling.readthedocs.io/en/latest/>
- Fetchers basics: <https://scrapling.readthedocs.io/en/latest/fetching/choosing.html>
- Dynamic websites: <https://scrapling.readthedocs.io/en/latest/fetching/dynamic.html>
- Dynamic websites with hard protections:
  <https://scrapling.readthedocs.io/en/latest/fetching/stealthy.html>
- Adaptive scraping:
  <https://scrapling.readthedocs.io/en/latest/parsing/adaptive.html>
- MCP server guide:
  <https://scrapling.readthedocs.io/en/latest/ai/mcp-server.html>
- Latest release API checked:
  <https://api.github.com/repos/D4Vinci/Scrapling/releases/latest>
- Scrapling `pyproject.toml`:
  <https://raw.githubusercontent.com/D4Vinci/Scrapling/main/pyproject.toml>
- Scrapling MCP server metadata:
  <https://raw.githubusercontent.com/D4Vinci/Scrapling/main/server.json>

Local trawl files:

- `README.md`
- `pyproject.toml`
- `src/trawl/pipeline.py`
- `src/trawl/fetchers/playwright.py`
- `src/trawl/extraction.py`
- `src/trawl_mcp/server.py`

