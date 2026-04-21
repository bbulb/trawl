# Changelog

All notable changes to trawl will be recorded here. Format loosely
follows [Keep a Changelog](https://keepachangelog.com/). trawl does
not yet follow semver strictly — expect breaking changes before
`1.0.0`.

## [Unreleased]

### Added

- **Defensive chunk-window cap on the reranker request** (PR #TBD).
  `src/trawl/reranking.py`'s `rerank()` now clamps its outbound
  payload to `TRAWL_RERANK_MAX_DOCS` (default `30`) documents and
  `TRAWL_RERANK_MAX_CHARS` (default `40000`) total characters
  (`query + docs`) before posting to `:8083`. If the doc-count cap
  fires, lower-cosine-rank tail chunks are dropped; if the char cap
  fires, each remaining doc is proportionally truncated (floor
  `200` chars to keep some signal). A single `WARNING` is logged
  when either cap activates. `<= 0` on either env var disables the
  cap (sentinel for the measurement sanity path). Rationale: the
  2026-04-20 stability diagnostic (PR #36) confirmed that
  `bge-reranker-v2-m3`'s server-side validator fast-rejects requests
  beyond its 8 192-token context with HTTP 500. Follow-up spike
  bracketed the empirical threshold between 40 000 chars (passes)
  and 50 000 chars (fails); 50 synthetic large-burst requests went
  from 100 % fast-reject without the cap to 0 % failure with it,
  while the 15-case parity matrix stayed 15/15 and the 16-pattern
  `code_heavy_query` slice remained unchanged (1 pre-existing,
  external flake unrelated to this change — `curl_options` budget).
  New `--via-trawl` mode in `benchmarks/reranker_stability_diag.py`
  routes burst requests through `trawl.reranking.rerank()` so the
  cap is exercised; the original direct-HTTP mode is retained for
  threshold bracketing. See
  `docs/superpowers/specs/2026-04-20-reranking-chunk-window-cap-design.md`.

## [0.4.0] — 2026-04-20

Fourth tagged release. Closes the C6 (hybrid retrieval) follow-up
chain surfaced in 0.3.0's "retrieval still struggles on
`code_heavy_query`" note. Headline: Playwright shadow-DOM unwrap for
code-block custom elements — MDN's post-2024 redesign wraps every
code example in `<mdn-code-example>` backed by Shadow DOM, which
Playwright's `page.content()` does not traverse. Inlining the shadow
`<pre><code>` content before extraction flips
`claude_code_mdn_fetch_api` to PASS and brings the 16-pattern
`code_heavy_query` slice to 16/16.

Also includes two Stack Exchange URL corrections (PR #30) and four
research spikes (PR #29/#31/#32/#33) whose conclusions and runners
are kept as reusable artefacts. Three of those spikes rejected their
hypothesis at the pre-registered gate — the measurement discipline
ended up being as valuable as the one hypothesis that stuck, because
each rejection narrowed the search to the actual bottleneck (Shadow
DOM).

### Added

- **Shadow-DOM unwrap for code-block custom elements (default on).**
  `src/trawl/fetchers/playwright.py` introduces
  `SHADOW_DOM_UNWRAP_TAGS` (initial allow-list: `mdn-code-example`)
  and `_unwrap_shadow_dom()`, called between the content-ready wait
  and `page.content()`. For each matching element, pulls
  `shadowRoot.querySelector('pre > code').textContent`, HTML-escapes
  it, and inlines `<pre><code>{text}</code></pre>` into the light
  DOM so `html_to_markdown` / Trafilatura sees a proper code block.
  Using `textContent` (rather than the full shadow `innerHTML`)
  avoids the syntax-highlight `<span>` scaffolding that would
  otherwise split identifiers like `JSON.stringify` across tag
  boundaries during markdown conversion. Falls back to the full
  `shadowRoot.innerHTML` when no `pre > code` exists. Idempotent;
  JS eval exceptions are swallowed so extraction never fails on
  account of unwrap.
    * New env var: `TRAWL_SHADOW_DOM_UNWRAP` (default `"1"`; set to
      `"0"` to disable).
    * New module-level constant: `SHADOW_DOM_UNWRAP_TAGS` in
      `fetchers/playwright.py`. Additions must go through the same
      measurement gate (fix a specific pattern and not regress the
      other 15).
    * Measurement runner: `benchmarks/shadow_dom_sweep.py` (2 modes
      × 16 patterns × 2 iter + 15-case parity per mode).
    * Design doc:
      `docs/superpowers/specs/2026-04-20-playwright-shadow-dom-design.md`.
    * Measurement: `shadow_dom_off` 15/16 → `shadow_dom_on` 16/16;
      `flipped_to_pass = [claude_code_mdn_fetch_api]`;
      `flipped_to_fail = []`; `top1_identity_changed = 1/16` (MDN
      only, `n_chunks_total` 22 → 24); parity 15/15 both modes;
      retrieval_ms regression within noise. Raw at
      `benchmarks/results/shadow-dom-sweep/2026-04-20T10-26-17Z/`
      (gitignored).

### Fixed

- **Two Stack Exchange `code_heavy_query` URLs resolved to
  unrelated questions.** `claude_code_serverfault_nginx_reverse_proxy`
  pointed at `serverfault.com/questions/378860` (resolves to an
  apache-vhosts / cookie question, not the nginx reverse-proxy Host
  header question). `claude_code_stackoverflow_python_async_subprocess`
  pointed at SO #44488350, which is an *answer* ID whose parent
  question is about CSV escaping. Stack Exchange resolves by ID
  alone, ignoring the slug, so both patterns had been failing
  against content unrelated to their query since the coding shard
  was introduced. Replaced with the canonical questions (SF #87056
  and SO #42639984); both flip to PASS. Not an extraction defect —
  `benchmarks/stackexchange_extraction_diag.py` confirmed trawl's
  extraction was intact. Also removes a duplicate argparse flag
  registration in `tests/test_agent_patterns.py` left behind by a
  stack-merge union resolver.

### Research (no code change, shipped as reusable runners + design docs)

- **C6 RRF-k tuning spike** (PR #29). Measured
  `TRAWL_HYBRID_RRF_K ∈ {10, 30, 60, 100}` on the 16
  `code_heavy_query` patterns with hybrid retrieval on. All four k
  values produced identical assertion pass rate and identical top-1
  reshuffles across three patterns — the reranker stabilises the
  pre-rerank ordering, so RRF k is effectively invisible
  downstream. Gate (b): retain `k=60`. Runner:
  `benchmarks/c6_rrf_k_sweep.py`; design doc:
  `docs/superpowers/specs/2026-04-20-c6-rrf-k-tuning-design.md`.
- **Identifier-aware BM25 tokenizer spike** (PR #31). Hypothesised
  that emitting compound tokens for dotted (`asyncio.gather`) /
  hyphenated (`Content-Type`) identifiers would let the sparse
  ranker boost code-heavy chunks. Measurement (3 modes × 16
  patterns): `net_assertion_delta = 0`, `top1_identity_changed =
  0/16`. Corpus-side compound emission alone is insufficient when
  queries don't contain the compound identifier (the MDN query
  describes intent — "send a POST request" — not symbols). Gate
  (b). Runner: `benchmarks/bm25_id_aware_sweep.py`; design doc:
  `docs/superpowers/specs/2026-04-20-bm25-id-aware-tokenizer-design.md`.
- **HyDE → BM25 query spike** (PR #32). Hypothesised that the
  HyDE hypothetical answer (which does emit compound identifiers
  under the current Gemma prompt) could feed the sparse query if
  routed into BM25 in addition to the dense path. Measurement (3
  modes × 16 patterns): `net_delta = 0`. HyDE produced the right
  identifiers, but the MDN failure survived because — as the next
  spike proved — the underlying chunks didn't contain those
  identifiers in the first place (they were in Shadow DOM). Gate
  (b). Runner: `benchmarks/hyde_compound_id_sweep.py`; design doc:
  `docs/superpowers/specs/2026-04-20-hyde-compound-identifier-design.md`.
- **MDN reranker diagnostic** (PR #33). One-shot diagnostic to
  locate the MDN assertion-keyword chunk's rank across raw /
  reranked / HyDE modes. Found the keyword chunk at rank 14 even
  in `raw` mode (no reranker) — reranker was not the bottleneck.
  Direct inspection of the HTML returned by Playwright showed 23
  `<mdn-code-example>` tags with `innerHTML`-empty light DOM; the
  real code lived in Shadow DOM. Decision hint `D1`, which set up
  PR #34. Runner: `benchmarks/mdn_reranker_diag.py`; design doc:
  `docs/superpowers/specs/2026-04-20-mdn-reranker-diagnostic-design.md`.

### Known caveats

- **Reranker `:8083` intermittently returns HTTP 500** during
  sweeps (observed across PR #31/#32/#33/#34 measurements). The
  client falls back to cosine-only scoring per the existing
  `reranker unavailable, falling back to cosine: ...` log line, so
  assertions still pass on the 16-pattern slice and on the 15-case
  parity matrix. Flagged here but not treated as a 0.4.0 gate
  failure; a separate reliability investigation is queued.
- **`SHADOW_DOM_UNWRAP_TAGS` allow-list is narrow.** Only
  `mdn-code-example` ships. Other docs sites that use similar
  Shadow-DOM wrappers (Docusaurus / GitBook variants) are not yet
  covered; each addition will come with its own measurement PR.

## [0.3.0] — 2026-04-20

Third tagged release. Packs up the six C-series follow-ups and the
longform retrieval cost spike that landed between 2026-04-15 (the
previous `v0.2.0` tag) and 2026-04-20.

(Note: `v0.2.0` was cut on 2026-04-15 with a narrower scope — raw
passthrough, Docker cleanup, WCXB benchmark. All subsequent work
ships here as `0.3.0`.)

Headline work: opt-in longform chunk budget (C5 follow-up, ~69%
retrieval p95 reduction on wiki / arxiv pages), opt-in BM25 hybrid
retrieval (C6), per-fetch result cache (C8, default on), per-host
adaptive content-ready ceiling (C9, default on), C7 PDF HEAD probe,
C16 compositional payload enrichment, agent-patterns harness shard
groundwork, and assorted lint / stack-merge cleanup.

Per-feature detail below.

### Added

- **Longform retrieval cost — chunk budget + BM25 prefilter (opt-in).**
  New optional `chunk_budget` kwarg on
  `src/trawl/retrieval.py::retrieve()` and matching env var
  `TRAWL_CHUNK_BUDGET` (default `0` = disabled). When set and a page
  produces more chunks than the budget, the surplus is dropped *before*
  the embedding loop using the C6 BM25 scorer, so only the top-N
  chunks reach bge-m3. Reuses the C6 tokenizer; no new deps. Caps
  embedding cost on longform pages (Wikipedia, arXiv PDFs).
  Measurement at `TRAWL_CHUNK_BUDGET=100` (4 longform cases × 2 modes
  × 3 iterations): `retrieval_ms.p95` drops 6,002 ms → 1,890 ms (69%
  reduction), 4/4 cases keep the same post-reranker rank-1 chunk,
  parity matrix unchanged at the pre-existing 14/15 (the lone fail,
  `kbo_schedule`, already fails on clean `develop`). New
  `PipelineResult.n_chunks_embedded` field + `telemetry.jsonl`
  schema v1 entry reports the post-prefilter count so hit rate is
  measurable without a rerun. Spec:
  `docs/superpowers/specs/2026-04-20-longform-retrieval-cost-design.md`.
  Follows up on the C5 premise spike conclusion
  (`docs/superpowers/specs/2026-04-20-c5-hierarchical-fetch-conclusion.md`).
    * New env var: `TRAWL_CHUNK_BUDGET` (default `0`).
    * 8 unit tests in `tests/test_retrieval_chunk_budget.py`
      (monkey-patched embeddings).
    * Measurement script: `benchmarks/longform_retrieval_cost_measure.py`
      (takes `--budget`, `--iterations`, writes JSON summary + md
      report to `benchmarks/results/longform-retrieval-cost/<ts>/`).
- **C6 — BM25 hybrid retrieval (opt-in).** New module
  `src/trawl/bm25.py` exposes a rule-based multilingual tokenizer
  (Latin word-level / Hangul character bigrams / kana & CJK-unified
  single characters), a thin wrapper around `rank_bm25.BM25Okapi`,
  and a Reciprocal Rank Fusion helper. When
  `TRAWL_HYBRID_RETRIEVAL=1` is set, `src/trawl/retrieval.py::retrieve()`
  scores BM25 alongside dense cosine and fuses both rankings via RRF
  (`k=60`, override with `TRAWL_HYBRID_RRF_K`). `ScoredChunk.score`
  still carries the raw dense cosine so the reranker and telemetry
  see the same numbers as before — only the pre-rerank ordering
  changes. Dense-only behaviour (default off) is bit-for-bit
  unchanged. Parity matrix stays 15/15 in both modes. Measurement:
  `notes/c6-hybrid-measurement.md`. Spec:
  `docs/superpowers/specs/2026-04-19-c6-hybrid-retrieval-design.md`.
    * New dep: `rank_bm25>=0.2.2` (pure-Python BM25Okapi + numpy).
    * New env vars: `TRAWL_HYBRID_RETRIEVAL` (default `0`),
      `TRAWL_HYBRID_RRF_K` (default `60`).
    * 25 unit tests in `tests/test_bm25.py` + 7 integration tests in
      `tests/test_retrieval_hybrid.py` (monkey-patched embeddings,
      no live infra required).
- **Agent-patterns assertion DSL — `cache_hit` key.** Extends the
  `tests/agent_patterns/` whitelist with a `cache_hit: bool` key that
  mirrors `PipelineResult.cache_hit`. Pattern authors can now assert
  that a repeat-visit step actually served from the C8 per-fetch
  cache. Applied to the three `workflows.yaml::repeat_visits` patterns
  so `step 1+` require `cache_hit: true` on non-profile fetches and
  the final profile-fast-path step requires `cache_hit: false`
  (profile path bypasses `fetch_cache` by design).
- **C9 — Per-host adaptive content-ready ceiling.** New module
  `src/trawl/host_stats.py` tracks the last 50 Playwright fetch
  durations per hostname. `fetchers/playwright.fetch()` and
  `render_session()` now consult `host_stats.ceiling_ms(url,
  default=wait_for_ms)` before opening a context; after 5
  observations the wait ceiling switches from the static 5000 ms
  default to `p95 × 1.5`, clamped to `[1500, 15000] ms`. Observations
  below `MIN_OBSERVATIONS` fall back to the caller-provided default,
  so new installs behave identically until a host warms up.
    * On-disk JSON at `~/.cache/trawl/host_stats.json` (atomic
      rewrite, corrupt/schema-mismatch recovery).
    * Observations above `MAX_CEILING_MS × 2` or below zero are
      discarded as sanity checks.
    * New env vars `TRAWL_HOST_STATS` (default `1`, set `0` to
      disable recording and fall back to the static ceiling) and
      `TRAWL_HOST_STATS_PATH` (default
      `~/.cache/trawl/host_stats.json`).
    * `render_session()` consults ceilings but doesn't record —
      caller holds the session arbitrarily long.
    * 21 unit tests in `tests/test_host_stats.py` cover warm-up
      threshold, bounds, rolling-window eviction, sanity filter,
      corrupt-file recovery, and env disable.
  Spec: `docs/superpowers/specs/2026-04-20-c9-per-host-ceiling-design.md`.
- **C8 — Per-fetch result cache.** New module `src/trawl/fetch_cache.py`
  caches successful HTML/PDF fetches to
  `~/.cache/trawl/fetches/<sha256>.json` keyed by URL. Subsequent fetches
  within the TTL skip Playwright + Trafilatura entirely; chunking,
  embedding, retrieval, and enrichment still run fresh (query-specific).
  Expected savings: ~2-5 s per warm repeat visit (60-70% of total
  latency on repeat_visits workflows).
    * `PipelineResult.cache_hit: bool` exposes reuse for telemetry /
      assertion authors (new field, default `False`, no MCP API change).
    * New env vars `TRAWL_FETCH_CACHE_TTL` (default 300, set `0` to
      disable), `TRAWL_FETCH_CACHE_PATH` (default
      `~/.cache/trawl/fetches`), `TRAWL_FETCH_CACHE_MAX_MB` (default
      100, soft cap with mtime-based LRU trim).
    * Profile fast/transfer paths, passthrough branches, and error
      results are never cached — each has a distinct invalidation
      mode that MVP scope excludes.
    * Atomic file writes (`tempfile` + `os.replace`). Corrupt or
      schema-mismatched entries are skipped and deleted on the next
      read.
    * 21 unit tests in `tests/test_fetch_cache.py` + 7 pipeline
      integration tests in `tests/test_pipeline_cache.py`. No live
      infra required for either.
  Spec: `docs/superpowers/specs/2026-04-20-c8-per-fetch-cache-design.md`.
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
- **Agent-patterns assertion DSL — C16 enrichment keys.** Extends the
  `tests/agent_patterns/` assertion whitelist with four keys that let
  pattern authors verify the compositional payload directly:
    * `excerpts_min_count` — `int` or `">= N"`, measured against the
      `excerpts` field length.
    * `outbound_links_contain_any` — `list[str]` of substrings
      matched against each link's `url` or `anchor_text`.
    * `page_entities_contain_any` — `list[str]` of substrings matched
      against the emitted `page_entities`.
    * `chain_hints_has_key` — `str`, the expected top-level key in the
      `chain_hints` dict.
  Applied to the two `workflows.yaml` compositional patterns so arXiv
  / GitHub host-specific `chain_hints` (`pdf_template`, `raw_template`)
  and minimum excerpt counts are now live assertions.
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
