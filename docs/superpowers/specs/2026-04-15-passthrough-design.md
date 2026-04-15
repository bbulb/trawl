# Raw passthrough for JSON/XML responses

**Date**: 2026-04-15
**Status**: Design approved, pending implementation plan

## Problem

`fetch_page` currently routes every non-PDF URL through Playwright →
Trafilatura / BS fallback → chunking → retrieval. When the URL returns
structured data (JSON, XML, RSS, Atom), Chromium wraps the raw body in
a JSON/XML viewer DOM, and Trafilatura either returns empty or BS
extracts a mangled version of the payload. Agents calling known API
endpoints (e.g. a weather.com API used by a downstream bot) receive
garbled output instead of the original body.

We want a bypass: when the content is structured data, skip extraction
and return the raw body as-is.

## Scope

**In scope**

- Detect JSON / XML / RSS / Atom responses via URL hint or
  `Content-Type` header.
- Return the raw body as a single chunk, skipping extraction, chunking,
  embedding, and reranking.
- Enforce a byte cap to protect local-LLM context windows.

**Out of scope**

- Structured querying of JSON/XML (jq-style field selection).
- Parsing / reformatting / pretty-printing the body.
- CSV, YAML, plain text passthrough. Can be added later as the same
  mechanism (new content-type entries) but are not part of this work.
- Changes to the profile fast path.

## Design decisions (from brainstorm)

| Decision | Choice | Rationale |
|---|---|---|
| Trigger | URL suffix hint (httpx direct) + Playwright Content-Type post-check | Fast path for known API shapes; catches suffix-less endpoints (e.g. `/api/weather`) too |
| Response shape | Existing `PipelineResult` + `chunks[0].text = raw body` + new `content_type` field | Caller code unchanged; one meta field identifies the path |
| Byte cap | `TRAWL_PASSTHROUGH_MAX_BYTES = 262144` (256 KB) default | ~64K tokens; fits most local LLM contexts; covers weather-style APIs |
| Query behaviour on bypass | Ignored silently | `path="raw_passthrough"` + `content_type` sufficient to identify |
| Truncation | `truncated: bool` field, `ok=True` | Truncation is policy, not error |
| Tests | New `tests/test_passthrough.py` + MCP smoke update | Parity matrix preserved per CLAUDE.md |

## Architecture

New short-circuit branch in the full pipeline:

```
fetch_relevant(url, query)
  │
  ├─ (profile path) — unchanged
  │
  └─ _run_full_pipeline
      ├─ _is_pdf_url(url)?            → pdf.fetch (existing)
      │
      ├─ passthrough.matches(url)?    → passthrough.fetch (httpx)
      │     ├─ ok & CT allow-listed   → _build_passthrough_result
      │     └─ fail / CT mismatch     → fall through
      │
      ├─ _API_FETCHERS match          — unchanged
      │
      └─ playwright.fetch(url)
            ├─ content_type allow-listed?
            │     ├─ yes → fetch_raw_body(url) → _build_passthrough_result
            │     └─ no  → existing extraction → chunk → retrieve
```

Profile path is left alone — profiles are VLM outputs for HTML pages
and do not exist for JSON/XML URLs in practice. If one somehow exists,
drift detection will fall through to the transfer path, miss, and land
in the full pipeline, which routes correctly.

## Components

### `src/trawl/fetchers/passthrough.py` (new)

```python
PASSTHROUGH_CONTENT_TYPES: tuple[str, ...] = (
    "application/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
)
# Also match any `+json` or `+xml` suffix (e.g. application/vnd.api+json).

PASSTHROUGH_URL_SUFFIXES: tuple[str, ...] = (".json", ".xml", ".rss", ".atom")

PASSTHROUGH_MAX_BYTES: int = int(os.environ.get("TRAWL_PASSTHROUGH_MAX_BYTES", "262144"))

def matches(url: str) -> bool: ...
def is_passthrough_content_type(ct: str | None) -> bool: ...
def fetch(url: str, *, timeout_s: float = 15.0) -> FetchResult: ...
def fetch_raw_body(url: str, *, timeout_s: float = 15.0) -> tuple[bytes, str | None, bool]:
    """Return (body, content_type, truncated). Used when Playwright detected
    passthrough Content-Type after the fact and we need the original bytes."""
```

`fetch` uses `httpx.get(..., follow_redirects=True)` with streaming
(`iter_bytes`) and stops at `PASSTHROUGH_MAX_BYTES + 1` bytes, marking
`truncated=True`.

### `src/trawl/fetchers/playwright.py` (modified)

- Add `content_type: str | None = None` to `FetchResult` (dataclass
  default preserves backward compat).
- In `_open_context`, capture the `Response` returned by `page.goto`
  and read `response.headers.get("content-type")`. Yield it alongside
  the existing tuple.
- `fetch()` populates `FetchResult.content_type` from the captured
  header.

### `src/trawl/pipeline.py` (modified)

- Add to `PipelineResult`: `content_type: str | None = None`,
  `truncated: bool = False`.
- Import `PASSTHROUGH_MAX_BYTES` from `fetchers.passthrough` (defined there since both `passthrough.fetch` and `fetch_raw_body` need it; pipeline only reads it for logging/assertions).
- In `_run_full_pipeline`, insert the two-stage passthrough branch
  (diagram above).
- New helper `_build_passthrough_result(url, query, body, content_type,
  fetcher_name, t_start, fetch_ms, truncated)`:
  - Decode bytes using the charset from `Content-Type` if present,
    else UTF-8 with `errors="replace"`.
  - Build a single chunk dict with `text=body`, `heading=None`,
    `char_count=len(body)`, `chunk_index=0`, `score=None`.
  - Return `PipelineResult(path="raw_passthrough", structured_path=False,
    hyde_used=False, n_chunks_total=1, page_chars=len(body),
    content_type=content_type, truncated=truncated, chunks=[chunk], ...)`.

### `src/trawl_mcp/server.py`

No changes. `content_type` and `truncated` flow through `to_dict`
automatically.

## Data flow details

**httpx path (URL hint matched)**

1. `passthrough.matches(url)` → True.
2. `passthrough.fetch(url)` → streamed GET, cap at byte limit, verify
   `Content-Type` is allow-listed.
3. On success, `_build_passthrough_result`. On mismatch / error,
   return `ok=False` so pipeline falls through to Playwright.

**Playwright post-detection path (URL hint missed)**

1. Playwright `fetch()` returns with `content_type` set.
2. Pipeline calls `is_passthrough_content_type`. If True, discard the
   rendered HTML (Chromium already injected JSON-viewer DOM) and call
   `fetch_raw_body(url)` for the original bytes via httpx.
3. `_build_passthrough_result`.
4. If `fetch_raw_body` fails, return an error result with
   `error="passthrough raw body fetch failed: {reason}"`. Do not
   fall back to Trafilatura — the rendered HTML is not trustworthy
   for structured data.

## Error handling

| Condition | Behaviour |
|---|---|
| httpx timeout / connection error in `passthrough.fetch` | `ok=False, error=...`, fall through to Playwright |
| HTTP 4xx/5xx | `ok=False, error="HTTP {code}"`, fall through |
| URL suffix matched but `Content-Type` not allow-listed | `ok=False, error="content-type mismatch: {ct}"`, fall through |
| Playwright detected passthrough but `fetch_raw_body` fails | Terminal `error="passthrough raw body fetch failed: {reason}"`, `chunks=[]`, `path="raw_passthrough"` |
| Empty body (0 bytes) | `error="empty body"`, `ok=False`, `chunks=[]` |
| Body exceeds `PASSTHROUGH_MAX_BYTES` | `truncated=True`, `ok=True`, `chunks=[{text: <truncated bytes>}]`, no `error` |
| Binary MIME (`octet-stream`, `image/*`, `application/pdf`) | Not passthrough; allow-list is strict prefix match + `+json`/`+xml` suffix test |

Encoding: honour `charset=` from `Content-Type` when present, else
UTF-8 with `errors="replace"`. Don't parse XML declarations (YAGNI).

## Testing

### `tests/test_passthrough.py` (new)

Local `http.server.ThreadingHTTPServer` fixture on a random port — no
network dependencies.

- `test_matches_url_suffix` — positive/negative URL cases.
- `test_is_passthrough_content_type` — including `application/vnd.api+json`,
  excluding `text/html`, `application/pdf`, `image/png`, `None`.
- `test_fetch_json_via_httpx` — end-to-end through `fetch_relevant`:
  `path="raw_passthrough"`, `content_type="application/json"`,
  `chunks[0].text == '{"a":1}'`, `truncated=False`.
- `test_fetch_xml_via_httpx` — same for XML.
- `test_url_suffix_mismatch_fallback` — `.json` URL returning
  `text/html` falls through.
- `test_content_type_post_detection` — suffix-less URL; simulated by
  injecting a `FetchResult(content_type="application/json")` stub into
  a pipeline unit test that verifies `fetch_raw_body` is called and
  the passthrough branch is taken.
- `test_truncation` — body at `MAX+1KB` bytes → `truncated=True`,
  `len(chunks[0].text) == MAX`, `ok=True`.
- `test_empty_body` — `error="empty body"`, `ok=False`.
- `test_binary_mime_not_passthrough` — `application/octet-stream`
  does not trigger passthrough.
- `test_max_bytes_env_override` — `TRAWL_PASSTHROUGH_MAX_BYTES=1024`
  via monkeypatch → 1 KB truncation.

### `tests/test_mcp_server.py` (updated)

Add one case: stdio call with local JSON URL → payload contains
`path="raw_passthrough"`, `content_type`, `truncated` keys.

### `tests/test_pipeline.py` (unchanged)

Parity matrix stays at 12/12. This change does not touch any case
and the new branch is gated on URL/Content-Type signals none of the
existing cases produce.

## Environment variables

| Name | Default | Purpose |
|---|---|---|
| `TRAWL_PASSTHROUGH_MAX_BYTES` | `262144` (256 KB) | Hard cap on passthrough body size |

Documented in `.env.example` alongside the existing trawl settings.

## Files changed

- `src/trawl/fetchers/passthrough.py` — new
- `src/trawl/fetchers/playwright.py` — `FetchResult.content_type`,
  capture `Content-Type` from `page.goto` response
- `src/trawl/pipeline.py` — passthrough branch, `PipelineResult.content_type`,
  `PipelineResult.truncated`, `_build_passthrough_result`
- `tests/test_passthrough.py` — new
- `tests/test_mcp_server.py` — add passthrough case
- `.env.example` — document new env var
- `CLAUDE.md` — add `TRAWL_PASSTHROUGH_MAX_BYTES` to the quick
  reference / tuning table
