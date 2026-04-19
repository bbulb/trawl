# Architecture

trawl is a thin pipeline that turns a URL and a query into a small
bundle of highly relevant text chunks. The whole flow is under 600
lines of library code; everything interesting is in the choices
about which library does what.

This document explains what those choices are, why they are what
they are, and what it cost to figure them out. If you want a
how-to-use, read `README.md`. If you want rules for modifying the
code, read `CLAUDE.md`. This file is the "why".

## Pipeline

```
fetch_relevant(url, query, k=?, use_hyde=?, use_rerank=?)
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 1. Fetch                                        │
│   URL ends in .pdf / contains /pdf/ ?           │
│      yes → httpx.get(url) + pymupdf.open()      │
│            → plain text (one string per page)   │
│   YouTube video URL?                            │
│      yes → youtube_transcript_api.list+fetch()  │
│            → transcript text (joined segments)  │
│            fallback: playwright if no transcript│
│   GitHub URL?                                   │
│      yes → GitHub REST API (httpx)              │
│            → README / issue / PR / file content │
│            fallback: playwright if API fails    │
│   Stack Exchange URL?                           │
│      yes → SE API v2.3 (httpx)                  │
│            → question + answers as markdown     │
│            fallback: playwright if API fails    │
│   Wikipedia URL?                                │
│      yes → MediaWiki parse API (httpx)          │
│            → article HTML → html_to_markdown()  │
│            fallback: playwright if API fails    │
│      no  → sync_playwright().chromium.launch()  │
│            (wrapped by playwright-stealth)      │
│            → goto(url, wait_until=networkidle)  │
│            → page.content() → HTML string       │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 2. Extract (HTML → markdown)                    │
│   Runs three extractors in parallel mentally:   │
│     a. Trafilatura favor_precision=True         │
│     b. Trafilatura favor_recall=True            │
│     c. BeautifulSoup body.get_text() after      │
│        stripping <script> <style> <nav> etc.    │
│   Returns the LONGEST of the three.             │
│   Reasoning: each extractor excels on a         │
│   different page type; the longest output is    │
│   empirically the best proxy for "captured the  │
│   relevant content" across our 11-case matrix.  │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 3. Chunk                                        │
│   Adaptive max_chars:                           │
│     page < 20k chars → max_chars=900            │
│     otherwise        → max_chars=450            │
│   Split on markdown headings first; each        │
│   section may be further split if > max_chars.  │
│   Tables and lists preserved intact. Long       │
│   single-line inputs (common in PDFs) fall      │
│   through to sentence → word → char splitting.  │
│   Each chunk stores:                            │
│     .text          the original markdown        │
│     .embed_text    plain_text() without markup  │
│     .heading_path  ['Section', 'Subsection']    │
│   Chunks with .embed_text < 20 chars are        │
│   dropped (pure nav/link boilerplate).          │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 4. (Optional) HyDE expansion                    │
│   If use_hyde=True: ask the local LLM for a     │
│   2-3 sentence hypothetical answer to `query`.  │
│   Feed that answer's embedding as an extra      │
│   query vector, averaged with the real query.   │
│   OFF BY DEFAULT — the spike found the baseline │
│   already passes the full matrix and HyDE is    │
│   non-deterministic on Gemma 4's reasoning-mode │
│   response format.                              │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 5. Retrieve                                     │
│   Embed query (+ HyDE text if any) and all      │
│   chunks via bge-m3 on a local llama-server     │
│   (OpenAI-compatible /v1/embeddings, default    │
│   http://localhost:8081/v1).                    │
│   When reranking is enabled (default), retrieves │
│   top-2k candidates instead of top-k.            │
│   Adaptive k (chunks ranked by cosine):          │
│     < 30  chunks → k = min(8, n/2 + 2)          │
│     < 100 chunks → k = 8                        │
│     < 200 chunks → k = 10                       │
│     ≥ 200 chunks → k = 12                       │
│   retrieve_k = min(k * 2, n_chunks) when         │
│   reranking; otherwise retrieve_k = k.           │
│   Chunks are embedded using heading + embed_text │
│   so section headers contribute to the vector.  │
│   Each input is truncated to 1800 chars as a    │
│   safety net for llama-server's ubatch limit.   │
└─────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 6. Rerank (default on)                          │
│   POST top-2k candidates to bge-reranker-v2-m3  │
│   on localhost:8083/v1/rerank. Cross-encoder     │
│   rescores each (query, chunk) pair. Return      │
│   top-k by cross-encoder relevance score.        │
│   On server failure: log warning, fall back to   │
│   cosine-ranked top-k from step 5.               │
│   Adds ~0.5-2s latency per query.                │
└─────────────────────────────────────────────────┘
  │
  ▼
PipelineResult(chunks=[{heading, text, score}, …],
               fetch_ms, chunk_ms, retrieval_ms,
               total_ms, page_chars, n_chunks_total,
               compression_ratio, error=None)
```

## Design decisions

### Why not Firecrawl?

An early spike tried to use Firecrawl's self-hosted `/extract`
endpoint to do the whole thing: fetch + extract + LLM-driven
structured output. It failed across all three test cases with a
consistent pattern: a small instruction-tuned model (Gemma 4) would
not produce the Zod-validated JSON Firecrawl's internal schema
analyser required. The failure mode is generic to small
instruction-tuned models trying to satisfy strict field-name
constraints; a stronger model might pass, but the underlying
whole-page-dump architecture is fundamentally incompatible with
minimising tokens — you are shipping the entire page to the model
either way.

A follow-up spike built the current selective-extraction approach.
It passed 4/4 initial cases on a baseline and 11/11 after edge
cases were added.

A final A/B spike tested whether we needed Firecrawl at all if we
only wanted the markdown. We replaced Firecrawl's Playwright service
with direct Playwright (+ stealth) and Trafilatura + BeautifulSoup
for extraction, reached the same 11/11 recall, and eliminated six
containers of infrastructure. trawl is the packaged form of that
pipeline.

### Why three extractors?

Trafilatura is the published best-in-class main-content extractor
(F1 ~0.958 on the ScrapingHub benchmark vs. readability-lxml's
~0.922). It's excellent on articles. On lists and pricing cards it
silently drops 80%+ of the content because the heuristic decides
the page "isn't an article".

Running it in both `favor_precision=True` and `favor_recall=True`
gives two different threshold settings on the same heuristic. On
pricing_page_ko the precision mode returned 2,737 chars and the
recall mode returned 6,392 chars — a real improvement but still
missing the actual `₩14,000` price numbers.

Adding a BeautifulSoup fallback that strips only `<script>`,
`<style>`, `<nav>`, `<header>`, `<footer>`, `<aside>`, `<form>`,
`<iframe>`, `<svg>`, `<noscript>`, `<menu>`, `<dialog>`, `<template>`
and returns the rest as plain text recovered pricing cards (11,534
chars, 6 occurrences of `₩`). The downstream chunker + embedding
top-k filters the extra noise naturally — so the BS fallback only
costs us a bit of CPU, never recall.

The final `html_to_markdown()` returns `max([precise, recall, bs],
key=len)`. It's an unusual ensemble but it empirically wins on every
page type in the matrix. If a future case breaks this rule (BS wins
but it's actually noise), the right fix is a smarter selector, not
abandoning the three-way.

### Why Playwright directly instead of Firecrawl's service?

The single capability that made Firecrawl attractive was "Playwright
microservice that bypasses Cloudflare challenges on Stack Overflow".
Direct Playwright with default settings fails on the same sites.
`playwright-stealth` (~60 lines of patches wrapping `sync_playwright`)
handles the passive JS challenges that Cloudflare uses on ~95% of
protected pages. Stack Overflow now works; Reddit's new UI works;
Discord blog works. Hard-enforced challenges (Cloudflare Turnstile,
DataDome) still fail — those are out of scope.

Latency cost of stealth: ~10-20 seconds on Cloudflare-protected pages
while the challenge is solved. For Stack Overflow specifically we
measured 24.0s vs. 11.2s on the Firecrawl-backed pipeline; the
Firecrawl service appears to do something faster (possibly the
challenge is cached between their multiple Playwright processes).

### Why adaptive k?

A fixed k would have to be the maximum of (what small pages need) and
(what large pages need). Small pages like the Playwright docs (~20
chunks) have rank noise: code snippets don't embed as cleanly as prose
for a natural-language query, so the "right" chunk often sits at rank
6 or 7. Large pages like the Korean Wikipedia 이순신 article (~300+
chunks) have facets scattered across distant sections; k=5 misses
chunks that k=12 reliably includes.

Measured failure modes without adaptive k:
- k=5 everywhere: Wikipedia fails, Stack Overflow fails, Playwright
  docs fails
- k=12 everywhere: everything passes but small-page token budgets
  blow up unnecessarily

The current thresholds were chosen by running the matrix at k=5,
k=7, k=8, k=10, k=12 per case and picking the smallest k that passes
for each size class, then smoothing into a curve.

### Why a separate PDF path?

PyMuPDF is fast, memory-safe, and produces cleaner text than running
a PDF through Playwright (which would render the PDF viewer's UI, not
the document content). The cost is a separate code path for
recognising PDFs — currently a URL-suffix + `/pdf/` heuristic. A more
robust approach would be to HEAD the URL and inspect `Content-Type`;
that's a planned improvement.

One sharp edge: PyMuPDF's default text extraction can produce
single-line output on PDFs with unusual layouts (no explicit
newlines). Our chunker's `_split_long_line()` sentence fallback
handles this — before it was added, the arXiv test case "passed"
with 1 chunk of 110k characters (see `CHANGELOG.md` for the fix).

### Why is HyDE off by default?

It was added in Spike 2 as a safety valve for the vocabulary-mismatch
case (user asks "오늘 야구 일정", answer chunks contain team names
only). The baseline retrieval turned out to handle that case fine
without HyDE, so turning it on by default would just add latency for
no measurable recall gain on the 11-case matrix.

HyDE stays in the codebase as a callable function. If a future query
class regresses, it's the first thing to try. Two improvements from
the initial spike form:

1. **Endpoint moved from :8080 to :8082.** The original spike pointed
   HyDE at a main large-model llama-server on :8080, which on shared
   setups is typically servicing another consumer (e.g. a chat agent
   with long tool loops) across a limited number of llama-server
   slots. A trawl HyDE call would compete for a slot and, on models
   with known KV-cache-reuse issues, potentially evict an active
   chat's cache. The fix: point HyDE at :8082, a small utility
   llama-server dedicated to auxiliary tasks — no slot contention.
2. **`chat_template_kwargs.enable_thinking=False` is passed.** Without
   this, Gemma 4's reasoning-mode response burns the token budget on
   internal reasoning and returns empty `content`. With it, the E4B
   utility model answers directly in ~1-2 seconds, and the response
   shape matches what the retrieval layer expects.

The `reasoning_content` fallback in `hyde.expand()` stays as a safety
net for servers where `enable_thinking=False` isn't honoured (older
llama.cpp, different base model). Override via env vars:
`TRAWL_HYDE_URL`, `TRAWL_HYDE_MODEL`.

## Measured performance

Baseline on the 11-case matrix (llama-server with bge-m3 on :8081,
M-series Mac, first run after browser warm-up):

| Case | Tokens out | Chunks | Compression | Latency | Fetcher |
|------|-----------:|-------:|------------:|--------:|---|
| kbo_schedule | 335 | 2 | 1.0× | ~6.8s | playwright |
| korean_wiki_person | 1036 | 10 | 19.7× | ~4.9s | wikipedia |
| korean_news_ranking | 1158 | 8 | 6.0× | ~8.3s | playwright |
| pricing_page_ko | 2110 | 8 | 1.8× | ~8.9s | playwright |
| english_tech_docs | 966 | 7 | 1.5× | ~6.1s | playwright |
| japanese_wiki | 1012 | 10 | 16.2× | ~4.0s | wikipedia |
| blog_post_no_heading | 2010 | 7 | 1.5× | ~6.8s | playwright |
| github_readme | 734 | 6 | 1.3× | ~0.4s | github |
| arxiv_pdf | 1644 | 12 | 22.2× | ~5.2s | pdf |
| stackoverflow_question | 1138 | 10 | 12.9× | ~3.0s | stackexchange |
| very_short_page | 49 | 1 | 1.1× | ~6.1s | playwright |
| youtube_transcript | 695 | 3 | 1.0× | ~1.4s | youtube |

**Averages**: ~1070 output tokens, ~5.3s latency. API-based fetchers
(GitHub, Stack Exchange, Wikipedia, YouTube) account for the fastest
cases; the remaining Playwright-based cases average ~7.3s.

"Tokens out" is `output_chars / 3` as a rough estimate; the real
tokenisation depends on the downstream consumer's tokeniser. For
bge-m3 embeddings the character ratio is close to 1:1 on Korean and
~0.25:1 on English.

## Known limitations

### Cloudflare-hardened sites

Passive JS challenges (the Stack Overflow tier) are solved by
playwright-stealth at a ~10-20s latency cost. Actively enforced
challenges (Cloudflare Turnstile, DataDome, PerimeterX) are not.
Symptom: the returned chunks are the challenge page itself
("보안 확인 수행 중…", "Just a moment…"). Downstream agents should
learn to recognise these and treat the fetch as a miss.

### Serial fetching

The Playwright fetcher holds a module-level lock so only one fetch
runs at a time in a given process. Fine for small numbers of
concurrent users; a multi-tenant deployment needs a browser pool or
a queue.

### Auth / paywalls

trawl sends no cookies, headers, or credentials. Login-gated and
paywalled pages return the login/paywall page, not the content you
wanted. The pipeline doesn't crash — it returns low-signal chunks
and the agent should recognise this.

### PDF OCR

PyMuPDF extracts the embedded text layer. Scanned documents without
an OCR layer produce empty or near-empty output. Adding Tesseract or
equivalent is a planned improvement.

### Embedding rank noise on code-heavy pages

Natural-language queries against code-heavy docs (shell commands,
function signatures) embed less cleanly than against prose. We
mitigate with adaptive k and adaptive max_chars but the underlying
weakness of pure dense retrieval on code is a real thing. For a
production deployment the fix is BM25 hybrid retrieval; see the
"Future work" section below.

## Future work

Ordered by expected value-per-hour:

1. **Real-usage feedback loop**. Collect a week of actual queries
   from downstream integrations; identify regression cases the
   12-case matrix misses.
2. **Per-domain adaptive timeout** on top of the content-ready detector.
   `wait_for_ms=5000` is now the ceiling for content-ready stability
   detection (not a fixed wait — see `_wait_for_content_ready` in
   `fetchers/playwright.py`). A per-domain override could push the
   ceiling lower for known-fast hosts and higher for known-slow ones.
3. **BM25 hybrid retrieval** for code/technical queries. Would need
   a separate index and a way to detect when the query is
   code-shaped; probably more effort than it's worth unless we
   see measurable rank failures from real usage.
~~4. **Content-Type detection** for PDFs via HEAD requests~~ — **Done
   (C7, 2026-04-19).** `fetchers/pdf.probe(url)` HEAD-pre-probes
   suffix-less URLs and short-circuits to `pdf.fetch()` when the
   origin answers `application/pdf`. New `fetcher_used=pdf-probed`
   value distinguishes the probed path from the suffix-hit path.
5. **Browser pool** for concurrent fetches. Deferred until there's
   a concrete multi-user deployment that needs it.
~~6. **Per-fetch caching**~~ — **Done (C8, 2026-04-20).** Successful
   HTML/PDF fetches land in `~/.cache/trawl/fetches/<sha256>.json`
   (300 s default TTL, 100 MB soft cap, mtime-based LRU trim). Repeat
   visits within TTL skip Playwright + Trafilatura. Chunking /
   embedding / retrieval still run fresh because they're
   query-dependent. `PipelineResult.cache_hit` flags the reuse.
   Disable with `TRAWL_FETCH_CACHE_TTL=0`.

~~7. **Reranker pass**~~ — **Done.** Cross-encoder reranking via
bge-reranker-v2-m3 on `:8083` is implemented and on by default.
Retrieves 2x candidates then rescores with the cross-encoder.
Falls back gracefully to cosine-only if the server is unavailable.

8. **VLM profile prompt iteration** (ongoing). The profile prompt is
   at v2 (anti-sidebar anchor guidance + mapper noise filter). 36-site
   eval: 92% success, 16 IDEAL. Remaining failures: VLM hallucination
   on Korean text (Gemma 4B limit), anti-bot pages (not addressable).
   Next step would be a bigger VLM or language-specific prompting.

### Benchmark vs Jina Reader (2026-04-13)

12-site comparison (docs, wiki, news, product, QA, finance, blog):

| Mode | Avg tokens | vs Jina | GT pass |
|---|---|---|---|
| trawl-base | 1,177 | 23x fewer | 11/12 |
| trawl-cached (profile) | 1,004 | 30x fewer | 10/11 |
| Jina Reader | 27,506 | (baseline) | 12/12 |

trawl's advantage is token efficiency (query-aware selective extraction
vs full-page markdown). Jina's advantage is latency (CDN, avg 3.2s vs
trawl 9.3s). Profile adds further compression on structured pages
(Google Finance -88%, MDN -77%, BBC News -71%) but can increase tokens
on pages where the selector is too wide (pricing +216%, blog +29%).

Profile is most valuable for **repeat-visit structured pages** (finance,
news feeds, schedules). The current "suggest profile after 3 visits"
heuristic aligns with this data.

Full results: `benchmarks/results/`.

Anything on this list that's not justified by real-usage data is
speculation. Don't implement speculatively.

## Telemetry (optional)

Opt-in JSONL collector for `fetch_relevant()` calls. Off by default.
Activated with `TRAWL_TELEMETRY=1`; writes to `~/.cache/trawl/telemetry.jsonl`
(override with `TRAWL_TELEMETRY_PATH`). Single-generation size rotation
at `TRAWL_TELEMETRY_MAX_BYTES` (default 64 MB) — older data moves to
`telemetry.jsonl.1`.

Each line captures host, URL (plaintext), query SHA-1 prefix (query
plaintext is never stored), fetcher path, profile hit/miss, rerank and
HyDE flags, and latency/size breakdown. Full schema: see
`docs/superpowers/specs/2026-04-15-c4-telemetry-design.md`.

Purpose: feed the C4 (`notes/RESEARCH.md`) decision on whether
index-based extraction as a profile fallback has a problem to solve.

## Provenance

trawl is the packaged form of work that lived across three spikes:

1. A Firecrawl `/extract` spike that proved LLM-driven whole-page
   extraction doesn't work reliably with small local models and
   isn't token-efficient regardless.
2. A selective-extraction spike that built the pipeline in this
   repo against Firecrawl's markdown fetcher.
3. An A/B spike that replaced the Firecrawl stack with direct
   Playwright + Trafilatura + BS, reached parity, and removed six
   containers of infrastructure.

The 12 golden test cases in `tests/test_cases.yaml` originated in
those spikes and are preserved verbatim.
