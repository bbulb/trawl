# trawl

Selective web content extraction. URL + natural-language query → the few
chunks most relevant to the query, ranked by dense embedding similarity.
Exposed as a Python library and a stdio MCP server so agents
(Claude Code, Claude Desktop, anything MCP-aware) can read only the
parts of a page they actually need.

This file is loaded automatically by Claude Code when working in the
trawl directory. Humans should read `README.md` first, then
`CONTRIBUTING.md` for dev setup.

## Current status

- **Version**: 0.1.0 + cross-encoder reranking, env var unification,
  slot pinning, VLM profile prompt v2.
- **Parity matrix**: 12/12 cases pass (see `tests/test_cases.yaml`).
- **Profile eval**: 36-site evaluation — 92% success rate, 16/36 IDEAL
  selectors.
- **Benchmark vs Jina Reader**: ~23x fewer tokens on average across 12
  cases; profile-cached mode ~30x.

### What a new session should do first

1. Read `README.md` (5 min) to understand what trawl does.
2. Read `ARCHITECTURE.md` if you need to modify the pipeline — it has
   the "why each library was chosen" reasoning you'll need.
3. Activate the dev env: `mamba activate trawl` (create with
   `mamba env create -f environment.yml` if missing).
4. Run `python tests/test_pipeline.py` as a smoke test. Requires a
   running bge-m3 embedding server at `TRAWL_EMBED_URL` (default
   `localhost:8081`).

### What NOT to do on a fresh session

- Don't re-tune `chunking.py` / `retrieval.py` parameters
  "to see what happens". The "Things NOT to change" table below
  exists for a reason.
- Don't add crawling, search, or page-rewriting features. See the
  "In / out of scope" section below.

## Critical Rules

> **MUST follow these rules. No exceptions.**

- **Use the `trawl` mamba env.** Every command: run inside
  `mamba activate trawl` or prefix with `mamba run -n trawl`. `trawl`
  is `pip install -e .`-installed into this env; other envs won't
  have the editable install.
- **Run the parity matrix before committing any change to `src/trawl/`.**
  `python tests/test_pipeline.py` must stay 12/12. If a tuning change
  breaks one case, it's almost certainly breaking something else too —
  diagnose, don't just tighten ground truth.
- **Run the MCP smoke test before touching `src/trawl_mcp/`.**
  `python tests/test_mcp_server.py` proves the stdio protocol still
  works end-to-end.
- **Do not commit `tests/results/`.** Already gitignored, but watch
  for timestamp directories sneaking in.
- **Do not change `tests/test_cases.yaml` ground truth to make a
  failing test pass** without re-running the matrix to confirm the
  change is principled, not a fudge.
- **llama-server endpoint map** (reference setup; override with env
  vars, see `.env.example`):
  - `:8081` — bge-m3 embeddings, **mandatory** (without it retrieval
    fails).
  - `:8082` — utility LLM (e.g. Gemma 4 E4B). HyDE only. Small context
    (4K), auxiliary tasks.
  - `:8083` — bge-reranker-v2-m3 cross-encoder. Default on; falls back
    gracefully to cosine-only if absent. Run with
    `--reranking --pooling rank`.
  - `:8080` — vision-enabled main LLM. Used **only** for explicit
    `profile_page` invocations (bounded, manual-trigger workload).
    If slot contention shows up, set `TRAWL_VLM_URL` to a dedicated
    vision server; no code changes needed.
  - **Slot pinning** — `TRAWL_VLM_SLOT=<N>` / `TRAWL_HYDE_SLOT=<N>`
    pin requests to a specific llama-server slot (via `id_slot`) to
    avoid evicting other consumers' KV cache on shared servers with
    prompt caching.

## Quick Reference

All commands assume you're inside the `trawl` mamba env
(`mamba activate trawl`) or prefixed with `mamba run -n trawl`.

```bash
# First time (creates the env + installs deps)
mamba env create -f environment.yml
mamba run -n trawl playwright install chromium
mamba activate trawl

# Parity matrix: 12 cases, non-zero exit on regression
python tests/test_pipeline.py

# Single case, verbose
python tests/test_pipeline.py --only kbo_schedule --verbose

# With HyDE enabled (adds ~15-20s, rarely useful)
python tests/test_pipeline.py --hyde

# MCP server smoke test
python tests/test_mcp_server.py

# Benchmark vs Jina Reader (requires .env with JINA_API_KEY)
python benchmarks/run_benchmark.py
python benchmarks/run_benchmark.py --no-profile

# Profile eval: 36-site VLM prompt quality check (requires :8080 VLM)
python benchmarks/profile_eval.py
python benchmarks/profile_eval.py --category docs

# Start MCP server (stdio)
python -m trawl_mcp

# Library usage check
python -c "
from trawl import fetch_relevant
r = fetch_relevant('https://example.com/', 'what is this')
print(r.chunks)
"
```

## Architecture pointer

See `ARCHITECTURE.md` for:
- Full pipeline diagram
- Why each component was chosen
- Tuning decisions (adaptive k, max_chars, waitFor, stealth) and their
  measured effect
- Known limitations and workarounds

The `README.md` is for users. `ARCHITECTURE.md` is the file to read
when you need to understand *why* something is the way it is.

## Code layout

```
src/trawl/                       library — the pipeline
  pipeline.py                    fetch_relevant() entry point
  chunking.py                    heading + table preservation + sentence
                                 fallback + markdown markup stripping
  retrieval.py                   bge-m3 cosine top-k with adaptive k
  reranking.py                   bge-reranker-v2-m3 cross-encoder rerank
  extraction.py                  Trafilatura (precise+recall) + BS fallback
  hyde.py                        optional query expansion (off by default)
  profiles/                      VLM-based page profiling
    prompts.py                   VLM prompt (v2: anti-sidebar anchor guidance)
    mapper.py                    anchor→DOM→LCA→CSS selector (noise filter)
    vlm.py                       llama-server VLM client
    profile.py                   profile load/save/cache
  fetchers/
    playwright.py                sync_playwright + stealth, shared browser
    pdf.py                       httpx + pymupdf
    youtube.py                   youtube_transcript_api + playwright fallback
    github.py                    GitHub REST API + playwright fallback
    stackexchange.py             Stack Exchange API v2.3 + playwright fallback
    wikipedia.py                 MediaWiki parse API + playwright fallback

src/trawl_mcp/                   MCP stdio server wrapper
  server.py                      list_tools / call_tool handlers
  __main__.py                    `python -m trawl_mcp` entry

tests/
  test_cases.yaml                12 golden cases
  test_pipeline.py               parity runner — compares against ground truth
  test_mcp_server.py             stdio protocol smoke test
  results/                       gitignored test outputs

benchmarks/
  benchmark_cases.yaml           12 cases for trawl vs Jina comparison
  run_benchmark.py               trawl (base/profile/cached) vs Jina runner
  profile_eval_cases.yaml        36 cases for VLM profile eval
  profile_eval.py                profile generation quality evaluator
  results/                       gitignored benchmark outputs

examples/
  claude_code_config.json        MCP server entry for Claude Code
  mcp_gateway_config.yaml        mcp-gateway style snippet
```

## Conventions

- Python 3.10+, typed where it helps readability. Not a mypy-strict
  codebase yet.
- **No emoji in source or test files.** CLAUDE.md and README.md may
  have them sparingly when the user explicitly asks, but the default
  is no emoji.
- Docstrings on public functions; a one-line comment only when the
  *why* is non-obvious.
- Commits: conventional commit prefixes (`feat`, `fix`, `docs`,
  `test`, `refactor`, `chore`). Short subject, longer body if
  the change is non-trivial or has tuning rationale.
- Test artefacts land in `tests/results/<timestamp>/` and are
  gitignored. Don't bypass the gitignore.

## Things NOT to change without re-running the full test matrix

These values were tuned empirically and a change to any one of them
can regress 1-3 cases in the parity matrix. If you have a reason to
change them, run `tests/test_pipeline.py` before AND after.

| File | Value | Why it's load-bearing |
|---|---|---|
| `pipeline._adaptive_k` | `5/7/8/10/12` thresholds | Smaller pages need larger k for rank noise; bigger pages would be slow |
| `chunking.chunk_markdown` | `max_chars=450` | Larger chunks hurt recall (diffuses fact density) |
| `chunking.MIN_PLAIN_CHARS` | `20` | Smaller → keeps noise; larger → drops useful short chunks |
| `retrieval.EMBEDDING_BATCH` | `64` | Requires llama-server `--ubatch-size ≥ 2048` |
| `retrieval.MAX_EMBED_INPUT_CHARS` | `1800` | Safety net for the same ubatch ceiling |
| `fetchers/playwright.py wait_for_ms` | `5000` | Naver Sports SPA needs this much |
| `extraction.py` three-way max (precise, recall, bs) | order matters | Pricing pages need BS; articles need precise |
| `hyde.py DEFAULT_LLAMA_URL` | `:8082` | Utility LLM, not main LLM — slot contention risk on :8080 |
| `hyde.py chat_template_kwargs.enable_thinking` | `False` | Without it Gemma 4 burns all tokens on reasoning and returns empty content |
| `profiles/vlm.py chat_template_kwargs.enable_thinking` | `False` | Same Gemma 4 quirk as hyde.py |
| `pipeline.PROFILE_TRANSFER_MIN_RATIO` | `0.3` | Lower bound of acceptable subtree size ratio for host-transfer. Empirically validated on Google Finance (actual ratios 1.5-1.6x) |
| `pipeline.PROFILE_TRANSFER_MAX_RATIO` | `3.0` | Upper bound. Raising admits accidental `<body>`-level selector climbs |
| `reranking.py HTTP_TIMEOUT_S` | `30.0` | Reranker timeout; 20 pairs should complete well within this |
| `pipeline.py retrieve_k multiplier` | `2` | Retrieves 2x candidates for reranking; fewer reduces rerank benefit, more adds latency |
| `profiles/mapper.py DEFAULT_MAX_CANDIDATES_PER_ANCHOR` | `5` | Enough headroom to find non-noise candidates after sidebar/nav filtering |
| `profiles/mapper.py NOISE_CLS_RE` | `nav\|sidebar\|toc\|...` | Noise region detection for anchor filtering; too broad catches content, too narrow misses sidebars |

## In / out of scope

**In scope**: fetching one page at a time, extracting its relevant
parts for a given query, returning structured chunks. Targeting
MCP-compatible agents as the primary consumers.

**Out of scope**:
- Crawling (following links). trawl fetches one URL, that's it.
- Search (query → URL list). Use a separate web search tool.
- Commercial anti-bot bypass (DataDome, Cloudflare Turnstile with
  proof-of-work). Passive challenges work via stealth; active ones
  need a paid service.
- Content rewriting, summarisation, translation. Those belong in the
  downstream agent, not in trawl.

If someone asks to add crawling or search to trawl, push back. Those
are different tools with different failure modes.

## Getting unblocked

If a change breaks the parity matrix and you don't know why:
1. Run the failing case with `--verbose` to see the returned chunks.
2. Compare the fetched markdown to what the same URL produced before
   your change — the fetcher, extraction, and chunker each have
   isolated smoke tests you can run ad-hoc via `python -c "..."`.
3. If the failure involves specific facts missing from top-k, look
   at where those facts rank in the full retrieval (not just top-k).
   Often the fix is k, not the extraction.
4. If the embedding server has changed (new model, different
   quantisation, different context size), most tuning assumptions
   in this file need re-verification.
