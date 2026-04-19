# Changelog

All notable changes to trawl will be recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/). trawl does
not yet follow semver strictly — expect breaking changes before
`1.0.0`.

## [Unreleased]

### Added

- **C16 — Compositional payload enrichment.** New module
  `src/trawl/enrichment.py` derives four lightweight metadata fields
  from existing extraction output (no LLM, no network) so agents can
  chain follow-up fetches without re-parsing the markdown payload:
    * `excerpts` — top-3 chunks' first-sentence summary, char-capped
      at 120 (handles ko/ja/zh sentence terminators, strips markdown
      markup).
    * `outbound_links` — markdown `[text](url)` references from the
      emitted chunks, dedup'd, hard-capped at 50 entries / 10 KB.
      Image refs excluded.
    * `page_entities` — noun-phrase candidates pulled from `page_title`
      + chunk `heading_path` (English Capitalised n-grams + Korean
      Hangul runs), 20-entry cap.
    * `chain_hints` — per-host follow-up dict for arxiv / github /
      wikipedia (en/ko/ja) / youtube / stackoverflow. Empty for
      unknown hosts.
  Backward-compatible: all four `PipelineResult` fields default to
  empty containers; legacy callers see no behaviour change. MCP
  responses include the new fields automatically (via `to_dict`).
  Spec: `docs/superpowers/specs/2026-04-19-c16-compositional-payload-design.md`.
- **C7 — PDF Content-Type HEAD probe.** `fetchers/pdf.probe(url)`
  performs a small HEAD request before launching Playwright when the
  URL does not match the existing `.pdf` / `/pdf/` suffix heuristic.
  When the response Content-Type is `application/pdf`, the pipeline
  routes to `pdf.fetch()` instead of rendering the PDF viewer chrome
  through Chromium. New `fetcher_used` value `pdf-probed` distinguishes
  this from the suffix-hit path. Probe failure (HEAD 405, timeout,
  network error) silently falls through to the existing HTML path —
  C7 must never make trawl slower than before. Closes the
  ARCHITECTURE.md "Future work #4" item. Mirrors the existing
  `passthrough.probe` pattern.
- **Repeating-record chunking (`src/trawl/records.py`).** Scans
  rendered HTML for runs of sibling elements with the same
  `(tag, sorted_class_list)` signature (≥3 members, excluding
  nav/sidebar/tab noise) and injects ASCII sentinel lines around
  each record before extraction. The chunker honours the sentinels
  and emits one atomic chunk per record regardless of `max_chars`.
  Covers job listings, news cards, product rows, bestseller grids —
  anywhere retrieval should rank individual records instead of
  fragmenting mid-record. Three new parity cases
  (`wanted_jobs`, `hada_news`, `aladin_bestsellers`) bring the
  matrix to **15/15**. Toggle via `TRAWL_RECORDS` (default on).
- **Raw passthrough for structured-data URLs
  (`src/trawl/fetchers/passthrough.py`).** URLs with `.json`,
  `.xml`, `.rss`, or `.atom` suffixes skip Playwright entirely and
  return raw bytes via httpx, capped at
  `TRAWL_PASSTHROUGH_MAX_BYTES` (default 256 KB). Suffix-less
  endpoints are detected by a HEAD pre-probe. A post-detection path
  covers the case where Chromium wraps a JSON response in a viewer
  DOM: the rendered Content-Type triggers a re-fetch of raw bytes.
  Passthrough URLs don't require a query.
- **Opt-in telemetry (`src/trawl/telemetry.py`).** Activated with
  `TRAWL_TELEMETRY=1`; appends one JSON line per `fetch_relevant()`
  call to `~/.cache/trawl/telemetry.jsonl` (override via
  `TRAWL_TELEMETRY_PATH`). Single-generation size rotation at 64 MB.
  Captures host, URL, query SHA-1 prefix (never plaintext query),
  fetcher path, profile hit/miss, rerank/HyDE flags, and latency
  breakdown. Purpose: feed the C4 decision in `notes/RESEARCH.md`.
- **Profile host-transfer path.** When no exact-URL profile exists
  for a URL but the host has other profiles, trawl tries each
  existing selector, verifies the matched subtree's char count is
  within `[TRAWL_PROFILE_TRANSFER_MIN_RATIO,
  TRAWL_PROFILE_TRANSFER_MAX_RATIO]` of the recorded size, and on
  success persists a copy of the profile under the new URL's hash
  for future exact-match hits. Extends the VLM-profile ROI to sibling
  URLs without a second VLM call.
- **Content-ready wait detector for Playwright fetches.** Replaces
  the previous fixed `wait_for_ms` delay with an async predicate
  that polls DOM state (text length, stable ticks, absence of
  placeholder patterns) up to the `wait_for_ms` ceiling. Measured:
  avg fetch_ms 67% shorter across the parity matrix; Discourse/chat
  SPAs that held websockets open (e.g. NVIDIA forum: 17s → 4.4s) no
  longer wait for `networkidle`. Guarded by a `NETWORKIDLE_BUDGET_MS`
  short-fuse so suffering SPAs drop to `domcontentloaded`.
- **Streamable-HTTP MCP transport.** `python -m trawl_mcp --http
  [HOST:PORT]` starts the same tool set over streamable HTTP (default
  `127.0.0.1:8765`) in addition to the default stdio transport. Lets
  HTTP-only MCP clients integrate trawl without a stdio wrapper.
- **Reranker title-injection (C3 spike conclusion).** With
  `TRAWL_RERANK_INCLUDE_TITLE=1` (default on), reranker inputs are
  formatted as `Title: <page_title>\nSection: <heading>\nbody`.
  Average +0.27 top-1 relevance score vs bare body on the parity
  matrix; 0 regressions; max improvement +2.92
  (`pricing_page_ko`). Fine-tune half of the original C3 proposal
  remains deferred.
- **WCXB external benchmark integration (`benchmarks/wcxb/`).**
  One-shot runner against the Murrough-Foley WCXB dev split (1,497
  pages / 7 page types / 1,613 domains, CC-BY-4.0). Latest numbers:
  trawl `html_to_markdown` F1 = 0.777 vs in-environment Trafilatura
  baseline 0.750. Pinned SHA-256 manifest for reproducibility.
- **VLM profile prompt v2 and mapper noise filter.** The VLM prompt
  now instructs the model to pick mid-paragraph text instead of section
  headings (which duplicate in sidebar TOCs) and explicitly warns about
  sidebar/nav text duplication. The mapper adds a noise-region filter
  that deprioritises anchor matches inside `<nav>`, `<aside>`, elements
  with sidebar/toc/breadcrumb classes, or ARIA navigation roles. This
  prevents LCA collapse to `<body>` when sidebar entries match heading
  text. Profile eval results on 36 diverse sites: success rate 89% to
  92%, IDEAL selectors 10 to 16 (+60%), docs category 67% to 100%.
- **Benchmark suite (`benchmarks/`).** trawl vs Jina Reader (r.jina.ai)
  comparison framework with 12 test cases across docs, wiki, news,
  product, QA, finance, and blog categories. Measures latency, token
  count, and ground truth accuracy across three trawl modes (base,
  profile-gen, cached-profile) vs Jina. Key finding: trawl produces
  23x fewer tokens than Jina on average with comparable accuracy
  (11/12 vs 12/12 GT). Profile adds further compression on structured
  pages (e.g. Google Finance -88%, MDN -77%, BBC News -71%).
- **Profile evaluation suite (`benchmarks/profile_eval.py`).** 36-case
  evaluator recording VLM response, anchor matching, LCA path, and
  selector quality for prompt tuning iteration.
- **Slot pinning for shared llama-servers.** New env vars
  `TRAWL_VLM_SLOT` and `TRAWL_HYDE_SLOT` inject `id_slot`
  into request payloads, letting trawl pin requests to a dedicated
  llama-server slot. This prevents KV-cache eviction of other
  consumers on shared servers with prompt caching enabled. Unset by
  default (server assigns slots).
- **Cross-encoder reranking via bge-reranker-v2-m3** on
  `localhost:8083`. On by default; retrieves 2x candidates then
  rescores with the cross-encoder. Falls back gracefully to
  cosine-only if the server is unavailable. Adds ~0.5-2s latency.
  New env vars: `TRAWL_RERANK_URL`, `TRAWL_RERANK_MODEL`.
  New MCP parameter: `use_rerank` (default `true`).
- **Unified env var naming.** All environment variables now follow
  the `TRAWL_{COMPONENT}_{PROPERTY}` convention. Renames:
  `EMBEDDING_BASE_URL` → `TRAWL_EMBED_URL`,
  `EMBEDDING_MODEL` → `TRAWL_EMBED_MODEL`,
  `RERANKER_BASE_URL` → `TRAWL_RERANK_URL`,
  `RERANKER_MODEL` → `TRAWL_RERANK_MODEL`,
  `LLAMA_SERVER_URL` → `TRAWL_HYDE_URL`,
  `HYDE_MODEL` → `TRAWL_HYDE_MODEL`,
  `TRAWL_PROFILE_VLM_*` → `TRAWL_VLM_*`.

### Changed

- **HyDE default endpoint moved from `:8080` to `:8082`.** The original
  spike pointed HyDE at a main large-model llama-server, but on
  typical shared setups that endpoint's slots are reserved for
  another consumer (e.g. a chat agent). A HyDE call there would
  contend for a slot and, on models with known KV-cache-reuse
  issues, evict active chat caches. The fix: point HyDE at `:8082`,
  a small utility llama-server dedicated to auxiliary tasks.
- **HyDE now passes `chat_template_kwargs.enable_thinking=False`.**
  Without this, Gemma 4's reasoning-mode response burns the token
  budget on internal reasoning and returns empty `content`, forcing
  us to fall back to `reasoning_content` which sometimes contains
  meta-reasoning bullets instead of a clean answer. With the kwarg,
  the E4B utility model answers directly in ~1-2 seconds. The
  `reasoning_content` fallback stays as a safety net.
- **`HYDE_MAX_TOKENS` lowered from 800 to 300.** The reasoning budget
  is no longer needed.
- Documentation updates in `CLAUDE.md`, `ARCHITECTURE.md`, `README.md`,
  and `examples/{claude_code_config.json, mcp_gateway_config.yaml}` to
  reflect the new endpoint and explain the slot-contention reasoning.

### Defaults

| Env var | Old default | New default |
|---|---|---|
| `TRAWL_HYDE_URL` (was `LLAMA_SERVER_URL`) | `http://localhost:8080/v1` | `http://localhost:8082/v1` |
| `TRAWL_HYDE_MODEL` (was `HYDE_MODEL`) | `gemma-4-26B-A4B-it-Q8_0.gguf` | `gemma-4-E4B-it-Q8_0.gguf` |

No code change to the pipeline or fetchers; the parity matrix still
passes (HyDE is off by default, so the changed defaults only matter
when a caller explicitly sets `use_hyde=True`).


## [0.1.0] — 2026-04-10

Initial release. Packaged form of the selective-extraction spike
plus the A/B Firecrawl-replacement spike that followed it.

### Added

- `src/trawl/` — the pipeline library
  - `fetch_relevant(url, query, k=?, use_hyde=?)` as the single entry point
  - Playwright fetcher with playwright-stealth wrapper for Cloudflare
    passive challenges
  - PDF fetcher via httpx + PyMuPDF
  - Three-way extraction (Trafilatura precision + recall + BeautifulSoup
    fallback, longest wins)
  - Heading-scoped chunker with sentence-level fallback for single-line
    inputs (e.g. PDFs)
  - bge-m3 cosine retrieval with adaptive top-k (7/8/10/12 by chunk count)
  - Optional HyDE query expansion (off by default)
- `src/trawl_mcp/` — stdio MCP server exposing a single `fetch_page` tool
  - Runs the sync pipeline in a worker thread so `sync_playwright`
    doesn't collide with the asyncio event loop
  - Returns a JSON payload with chunks, timings, compression ratio,
    and error state
- `tests/test_pipeline.py` — parity matrix, 11 cases, exits non-zero
  on any regression
- `tests/test_mcp_server.py` — spawns `python -m trawl_mcp` as a
  subprocess and walks `initialize → list_tools → call_tool`
- `tests/test_cases.yaml` — 11 golden cases carried over from the
  extraction spikes (KBO schedule, Wikipedia ko/ja, Naver news
  ranking, Notion pricing, Playwright docs, Paul Graham essay,
  GitHub README, arXiv PDF, Stack Overflow, example.com)
- `examples/claude_code_config.json` — drop-in MCP server entry for
  Claude Code's `mcp_servers.json`
- `examples/mcp_gateway_config.yaml` — mcp-gateway style config snippet
- `README.md` — user-facing quick start, usage, testing
- `ARCHITECTURE.md` — design rationale, measured performance, spike
  provenance
- `CLAUDE.md` — project rules for Claude Code sessions working in
  this directory
- `LICENSE` — MIT
- `pyproject.toml` — package metadata, `trawl-mcp` console script

### Known limitations at 0.1.0

- Cloudflare-hardened sites with active challenges (Turnstile,
  DataDome) are not handled; passive JS challenges work via stealth
  at a ~10-20s latency cost per fetch
- Fetcher serialises on a module-level lock — fine for single-user
  setups, will need a browser pool for multi-tenant deployments
- No PDF OCR; scanned documents return empty chunks
- Auth/paywall pages are fetched but return the login page, not the
  content
- URL-suffix heuristic for PDF detection (`.pdf` or `/pdf/`); a
  `Content-Type` HEAD lookup would be more robust

See `ARCHITECTURE.md` for the full list and workarounds.

### Test matrix at 0.1.0

11/11 cases pass. Measured with bge-m3 on llama-server :8081 locally
on an M-series Mac:

| Case | Tokens | Latency |
|---|---:|---:|
| kbo_schedule | 335 | ~6.8s |
| korean_wiki_person | 1151 | ~10.6s |
| korean_news_ranking | 1158 | ~8.3s |
| pricing_page_ko | 2110 | ~8.9s |
| english_tech_docs | 966 | ~6.1s |
| japanese_wiki | 883 | ~10.1s |
| blog_post_no_heading | 2010 | ~6.8s |
| github_readme | 1466 | ~7.8s |
| arxiv_pdf | 1644 | ~5.2s |
| stackoverflow_question | 1138 | ~24.0s |
| very_short_page | 49 | ~6.1s |

Average ~1174 output tokens / ~9.2s. Stack Overflow's Cloudflare
challenge dominates the latency average; excluding it, ~7.7s.
