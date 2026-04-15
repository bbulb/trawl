# Raw Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bypass extraction/chunking/retrieval for JSON/XML/RSS/Atom responses; return the raw body as a single chunk.

**Architecture:** Two-stage detection. URL-suffix hint routes to a new httpx-based fetcher; Playwright path captures the response `Content-Type` and, on a passthrough hit, re-fetches the raw body via httpx. `PipelineResult` grows two optional metadata fields (`content_type`, `truncated`) so agents can identify passthrough responses without a schema break.

**Tech Stack:** Python 3.10+, httpx (already a dep), playwright (already a dep), pytest.

All commands run inside `mamba activate trawl` or via `mamba run -n trawl ...`.

**Spec:** `docs/superpowers/specs/2026-04-15-passthrough-design.md`

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/trawl/fetchers/passthrough.py` | **new** | URL/CT detection, httpx GET with byte cap, truncation |
| `src/trawl/fetchers/playwright.py` | modify | Capture `Content-Type` from `page.goto` response, expose on `FetchResult` |
| `src/trawl/pipeline.py` | modify | New `PipelineResult` fields, passthrough branch, `_build_passthrough_result` helper |
| `tests/test_passthrough.py` | **new** | Unit + integration tests against a local `http.server` fixture |
| `tests/test_mcp_server.py` | modify | Add one passthrough call case |
| `.env.example` | modify | Document `TRAWL_PASSTHROUGH_MAX_BYTES` |
| `CLAUDE.md` | modify | Add env var to quick reference |

---

## Task 1: Add `content_type` and `truncated` fields to `PipelineResult`

**Files:**
- Modify: `src/trawl/pipeline.py` (`PipelineResult` dataclass, `to_dict`)
- Test: `tests/test_passthrough.py` (new)

- [ ] **Step 1: Create `tests/test_passthrough.py` with a single failing test**

```python
"""Tests for raw-passthrough handling of JSON/XML responses."""
from __future__ import annotations

from trawl.pipeline import PipelineResult, to_dict


def test_pipeline_result_has_passthrough_fields():
    r = PipelineResult(
        url="https://x/y.json",
        query="",
        fetcher_used="passthrough",
        fetch_ms=0,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=0,
        page_chars=0,
        n_chunks_total=0,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[],
    )
    assert r.content_type is None
    assert r.truncated is False
    d = to_dict(r)
    assert "content_type" in d
    assert "truncated" in d
```

- [ ] **Step 2: Run test to verify it fails**

Run: `mamba run -n trawl pytest tests/test_passthrough.py::test_pipeline_result_has_passthrough_fields -v`
Expected: FAIL with `AttributeError: 'PipelineResult' object has no attribute 'content_type'`.

- [ ] **Step 3: Add fields to `PipelineResult`**

In `src/trawl/pipeline.py`, inside the `PipelineResult` dataclass, after the existing `rerank_ms: int = 0` field, add:

```python
    content_type: str | None = None
    truncated: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `mamba run -n trawl pytest tests/test_passthrough.py::test_pipeline_result_has_passthrough_fields -v`
Expected: PASS.

- [ ] **Step 5: Verify parity matrix still green (new optional fields must not regress anything)**

Run: `mamba run -n trawl python tests/test_pipeline.py`
Expected: `12/12 PASS`.

- [ ] **Step 6: Commit**

```bash
git add src/trawl/pipeline.py tests/test_passthrough.py
git commit -m "feat(pipeline): add content_type and truncated fields to PipelineResult"
```

---

## Task 2: Passthrough detection predicates (URL + Content-Type)

**Files:**
- Create: `src/trawl/fetchers/passthrough.py`
- Modify: `tests/test_passthrough.py`

- [ ] **Step 1: Write failing tests for predicates**

Append to `tests/test_passthrough.py`:

```python
from trawl.fetchers import passthrough


def test_matches_url_suffix_positive():
    assert passthrough.matches("https://api.example.com/data.json")
    assert passthrough.matches("https://feed.example.com/rss.xml")
    assert passthrough.matches("https://example.com/a.rss")
    assert passthrough.matches("https://example.com/a.atom")
    assert passthrough.matches("https://example.com/a.json?x=1")


def test_matches_url_suffix_negative():
    assert not passthrough.matches("https://example.com/index.html")
    assert not passthrough.matches("https://example.com/")
    assert not passthrough.matches("https://example.com/doc.pdf")


def test_is_passthrough_content_type():
    f = passthrough.is_passthrough_content_type
    assert f("application/json")
    assert f("application/json; charset=utf-8")
    assert f("application/vnd.api+json")
    assert f("application/xml")
    assert f("text/xml")
    assert f("application/rss+xml")
    assert f("application/atom+xml")
    assert f("application/problem+json; charset=utf-8")
    assert not f("text/html")
    assert not f("application/pdf")
    assert not f("application/octet-stream")
    assert not f("image/png")
    assert not f(None)
    assert not f("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: 3 new tests FAIL with `ModuleNotFoundError: trawl.fetchers.passthrough`.

- [ ] **Step 3: Create `src/trawl/fetchers/passthrough.py` with the predicates**

```python
"""Raw-passthrough fetcher for structured data responses.

Bypasses trawl's extraction/chunking/retrieval when the target URL
returns JSON, XML, RSS, or Atom. Detection is two-stage:

1. URL-suffix hint (this module's `matches`) — cheap, lets the pipeline
   take an httpx-only fast path without launching Playwright.
2. Content-Type post-check (`is_passthrough_content_type`) — used by
   the pipeline after Playwright has already loaded a suffix-less URL,
   to discover API endpoints like `/api/weather` that still return JSON.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

PASSTHROUGH_CONTENT_TYPES: tuple[str, ...] = (
    "application/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
)

PASSTHROUGH_URL_SUFFIXES: tuple[str, ...] = (".json", ".xml", ".rss", ".atom")

PASSTHROUGH_MAX_BYTES: int = int(
    os.environ.get("TRAWL_PASSTHROUGH_MAX_BYTES", "262144")
)


def matches(url: str) -> bool:
    """True when the URL path ends with a structured-data suffix."""
    path = urlsplit(url).path.lower()
    return path.endswith(PASSTHROUGH_URL_SUFFIXES)


def is_passthrough_content_type(ct: str | None) -> bool:
    """True when `ct` names a passthrough-eligible media type.

    Accepts explicit allow-list entries plus any `+json` / `+xml`
    structured-syntax suffix (RFC 6838 §4.2.8).
    """
    if not ct:
        return False
    base = ct.split(";", 1)[0].strip().lower()
    if base in PASSTHROUGH_CONTENT_TYPES:
        return True
    if base.endswith("+json") or base.endswith("+xml"):
        return True
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/fetchers/passthrough.py tests/test_passthrough.py
git commit -m "feat(passthrough): add URL and Content-Type detection predicates"
```

---

## Task 3: `passthrough.fetch` via httpx with streaming + truncation

**Files:**
- Modify: `src/trawl/fetchers/passthrough.py`
- Modify: `tests/test_passthrough.py`

- [ ] **Step 1: Write failing tests using a local HTTP server fixture**

Append to `tests/test_passthrough.py`:

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    # Populated per-test by the fixture.
    response_body: bytes = b""
    response_ct: str = "application/json"
    response_status: int = 200

    def log_message(self, *a, **kw):  # silence stderr noise
        pass

    def do_GET(self):
        self.send_response(self.response_status)
        self.send_header("Content-Type", self.response_ct)
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


@pytest.fixture
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", _Handler
    server.shutdown()
    server.server_close()


def test_fetch_json_ok(http_server):
    base, handler = http_server
    handler.response_body = json.dumps({"a": 1}).encode("utf-8")
    handler.response_ct = "application/json"
    handler.response_status = 200

    r = passthrough.fetch(f"{base}/data.json")
    assert r.ok
    assert r.content_type == "application/json"
    assert r.raw_bytes == b'{"a": 1}'
    assert r.truncated is False


def test_fetch_content_type_mismatch(http_server):
    base, handler = http_server
    handler.response_body = b"<html></html>"
    handler.response_ct = "text/html"
    r = passthrough.fetch(f"{base}/data.json")
    assert not r.ok
    assert "content-type mismatch" in (r.error or "")


def test_fetch_http_error(http_server):
    base, handler = http_server
    handler.response_body = b""
    handler.response_status = 404
    handler.response_ct = "application/json"
    r = passthrough.fetch(f"{base}/missing.json")
    assert not r.ok
    assert "HTTP 404" in (r.error or "")


def test_fetch_truncation(http_server, monkeypatch):
    monkeypatch.setattr(passthrough, "PASSTHROUGH_MAX_BYTES", 1024)
    base, handler = http_server
    handler.response_body = b"x" * 2048
    handler.response_ct = "application/json"
    r = passthrough.fetch(f"{base}/big.json")
    assert r.ok
    assert r.truncated is True
    assert len(r.raw_bytes) == 1024
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: new fetch tests FAIL (no `fetch` in passthrough module yet).

- [ ] **Step 3: Add `PassthroughResult` + `fetch` to `passthrough.py`**

Append to `src/trawl/fetchers/passthrough.py`:

```python
import time
from dataclasses import dataclass

import httpx


@dataclass
class PassthroughResult:
    """Result of a passthrough fetch. Mirrors FetchResult where it matters
    (url, ok, error, elapsed_ms) but carries raw bytes and content_type
    instead of rendered HTML, since passthrough skips extraction entirely.
    """
    url: str
    raw_bytes: bytes
    content_type: str | None
    elapsed_ms: int
    truncated: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch(url: str, *, timeout_s: float = 15.0) -> PassthroughResult:
    """GET `url` via httpx, stream-capped at PASSTHROUGH_MAX_BYTES.

    Returns `ok=False` (with an error string) when:
      - the request fails (network, timeout)
      - HTTP status is >= 400
      - Content-Type does not pass `is_passthrough_content_type`
      - the body is empty

    The caller is expected to fall through to another fetcher when `ok`
    is False, except in the terminal "empty body" case which is a real
    error (raw fetch worked, server returned nothing).
    """
    t0 = time.monotonic()
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout_s,
        ) as resp:
            if resp.status_code >= 400:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=resp.headers.get("content-type"),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"HTTP {resp.status_code}",
                )
            ct = resp.headers.get("content-type")
            if not is_passthrough_content_type(ct):
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"content-type mismatch: {ct!r}",
                )
            buf = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                remaining = PASSTHROUGH_MAX_BYTES - len(buf)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    buf.extend(chunk[:remaining])
                    truncated = True
                    break
                buf.extend(chunk)
            if not buf and not truncated:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error="empty body",
                )
            return PassthroughResult(
                url=url,
                raw_bytes=bytes(buf),
                content_type=ct,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                truncated=truncated,
            )
    except httpx.HTTPError as e:
        return PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/fetchers/passthrough.py tests/test_passthrough.py
git commit -m "feat(passthrough): add httpx-based fetch with streaming + byte cap"
```

---

## Task 4: Capture `Content-Type` in Playwright fetcher

**Files:**
- Modify: `src/trawl/fetchers/playwright.py`
- Modify: `tests/test_passthrough.py`

- [ ] **Step 1: Write a failing test that stubs Playwright**

Append to `tests/test_passthrough.py`:

```python
from trawl.fetchers.playwright import FetchResult


def test_playwright_fetch_result_has_content_type():
    r = FetchResult(
        url="https://x/",
        html="",
        markdown="",
        raw_html="",
        fetcher="playwright",
        elapsed_ms=0,
    )
    assert r.content_type is None
    r2 = FetchResult(
        url="https://x/",
        html="",
        markdown="",
        raw_html="",
        fetcher="playwright",
        elapsed_ms=0,
        content_type="application/json",
    )
    assert r2.content_type == "application/json"
```

- [ ] **Step 2: Run test to verify failure**

Run: `mamba run -n trawl pytest tests/test_passthrough.py::test_playwright_fetch_result_has_content_type -v`
Expected: FAIL with `TypeError: unexpected keyword argument 'content_type'` or `AttributeError`.

- [ ] **Step 3: Add `content_type` field and capture it**

In `src/trawl/fetchers/playwright.py`:

a) Add to the `FetchResult` dataclass (after `error: str | None = None`):

```python
    content_type: str | None = None
```

b) Add to `make_error_result` so the error path preserves the field signature (no behavior change; field defaults to None). No code change required there — the default covers it.

c) Modify `_open_context` to capture the navigation response:

Replace:

```python
        try:
            page.goto(url, wait_until="networkidle", timeout=goto_timeout_ms // 2)
        except PlaywrightTimeoutError:
            page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
        if wait_for_ms > 0:
            page.wait_for_timeout(wait_for_ms)
        html = page.content()
        yield context, page, html
```

with:

```python
        response = None
        try:
            response = page.goto(
                url, wait_until="networkidle", timeout=goto_timeout_ms // 2
            )
        except PlaywrightTimeoutError:
            response = page.goto(
                url, wait_until="domcontentloaded", timeout=goto_timeout_ms
            )
        if wait_for_ms > 0:
            page.wait_for_timeout(wait_for_ms)
        html = page.content()
        content_type = None
        if response is not None:
            try:
                content_type = response.header_value("content-type")
            except Exception:
                content_type = None
        yield context, page, html, content_type
```

d) Update `fetch()` to unpack the 4-tuple and thread `content_type` into the `FetchResult`:

Replace:

```python
            with _open_context(
                url,
                wait_for_ms=wait_for_ms,
                timeout_s=timeout_s,
                user_agent=user_agent,
            ) as (_ctx, _page, html):
                return FetchResult(
                    url=url,
                    html=html,
                    markdown="",
                    raw_html=html,
                    fetcher="playwright",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                )
```

with:

```python
            with _open_context(
                url,
                wait_for_ms=wait_for_ms,
                timeout_s=timeout_s,
                user_agent=user_agent,
            ) as (_ctx, _page, html, content_type):
                return FetchResult(
                    url=url,
                    html=html,
                    markdown="",
                    raw_html=html,
                    fetcher="playwright",
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    content_type=content_type,
                )
```

e) Update `render_session` similarly:

Replace:

```python
        with _open_context(
            url,
            wait_for_ms=wait_for_ms,
            timeout_s=timeout_s,
            user_agent=user_agent,
        ) as (_ctx, page, html):
            yield RenderResult(
                url=url,
                page=page,
                html=html,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
```

with:

```python
        with _open_context(
            url,
            wait_for_ms=wait_for_ms,
            timeout_s=timeout_s,
            user_agent=user_agent,
        ) as (_ctx, page, html, _content_type):
            yield RenderResult(
                url=url,
                page=page,
                html=html,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
```

- [ ] **Step 4: Run the new test**

Run: `mamba run -n trawl pytest tests/test_passthrough.py::test_playwright_fetch_result_has_content_type -v`
Expected: PASS.

- [ ] **Step 5: Re-run parity matrix to confirm no regression**

Run: `mamba run -n trawl python tests/test_pipeline.py`
Expected: `12/12 PASS`. (This exercises `_open_context` on live pages.)

- [ ] **Step 6: Commit**

```bash
git add src/trawl/fetchers/playwright.py tests/test_passthrough.py
git commit -m "feat(playwright): capture response Content-Type on fetch"
```

---

## Task 5: Pipeline integration — URL-hint path + `_build_passthrough_result`

**Files:**
- Modify: `src/trawl/pipeline.py`
- Modify: `tests/test_passthrough.py`

- [ ] **Step 1: Write a failing end-to-end test that goes through `fetch_relevant`**

Append to `tests/test_passthrough.py`:

```python
from trawl import fetch_relevant


def test_fetch_relevant_passthrough_json(http_server):
    base, handler = http_server
    body = b'{"hello": "world"}'
    handler.response_body = body
    handler.response_ct = "application/json"
    handler.response_status = 200

    r = fetch_relevant(f"{base}/data.json")
    assert r.error is None, r.error
    assert r.path == "raw_passthrough"
    assert r.content_type == "application/json"
    assert r.truncated is False
    assert r.fetcher_used == "passthrough"
    assert len(r.chunks) == 1
    assert r.chunks[0]["text"] == body.decode("utf-8")
    assert r.chunks[0]["chunk_index"] == 0
    assert r.n_chunks_total == 1


def test_fetch_relevant_passthrough_truncated(http_server, monkeypatch):
    from trawl.fetchers import passthrough as pt_mod
    monkeypatch.setattr(pt_mod, "PASSTHROUGH_MAX_BYTES", 32)
    base, handler = http_server
    handler.response_body = b'{"k":"' + b"x" * 100 + b'"}'
    handler.response_ct = "application/json"
    r = fetch_relevant(f"{base}/big.json")
    assert r.truncated is True
    assert len(r.chunks[0]["text"]) == 32
    assert r.error is None  # truncation is not an error
```

Note: `fetch_relevant` normally requires a query on the non-profile full pipeline. The passthrough branch must run BEFORE the "no query" guard fires.

- [ ] **Step 2: Run tests, expect failure**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v -k passthrough_json or truncated`
Expected: FAIL (either wrong `path`, `error` about missing query, or attribute missing).

- [ ] **Step 3: Add `_build_passthrough_result` to `pipeline.py`**

In `src/trawl/pipeline.py`, at the top add the import (next to other fetcher imports):

```python
from .fetchers import github, passthrough, pdf, playwright, stackexchange, wikipedia, youtube
```

Below `_chunk_to_dict` (before `_error_result`), add:

```python
def _decode_passthrough_body(body: bytes, content_type: str | None) -> str:
    """Decode passthrough bytes honoring `charset=` when present, else UTF-8.

    `errors="replace"` because the raw body might be binary-tainted
    (e.g. a JSON server returning malformed UTF-8); we prefer returning
    something readable over crashing the pipeline.
    """
    charset = "utf-8"
    if content_type:
        for part in content_type.split(";"):
            part = part.strip().lower()
            if part.startswith("charset="):
                charset = part.split("=", 1)[1].strip() or "utf-8"
                break
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _build_passthrough_result(
    url: str,
    query: str | None,
    *,
    body: bytes,
    content_type: str | None,
    fetcher_name: str,
    t_start: float,
    fetch_ms: int,
    truncated: bool,
) -> PipelineResult:
    text = _decode_passthrough_body(body, content_type)
    chunk = {
        "text": text,
        "heading": None,
        "char_count": len(text),
        "chunk_index": 0,
        "score": None,
    }
    return PipelineResult(
        url=url,
        query=query or "",
        fetcher_used=fetcher_name,
        fetch_ms=fetch_ms,
        chunk_ms=0,
        retrieval_ms=0,
        total_ms=int((time.monotonic() - t_start) * 1000),
        page_chars=len(text),
        n_chunks_total=1,
        structured_path=False,
        hyde_used=False,
        hyde_text="",
        chunks=[chunk],
        path="raw_passthrough",
        content_type=content_type,
        truncated=truncated,
    )
```

- [ ] **Step 4: Insert the URL-hint branch in `_run_full_pipeline`**

In `src/trawl/pipeline.py`, inside `_run_full_pipeline`, replace the current fetch block:

```python
    # 1. Fetch → markdown
    if _is_pdf_url(url):
        fetched = pdf.fetch(url)
        markdown = fetched.markdown
        fetcher_name = "pdf"
    else:
        for fetcher_mod, native_name in _API_FETCHERS:
```

with:

```python
    # 1. Fetch → markdown (or short-circuit to passthrough for structured data)
    if _is_pdf_url(url):
        fetched = pdf.fetch(url)
        markdown = fetched.markdown
        fetcher_name = "pdf"
    elif passthrough.matches(url):
        pt = passthrough.fetch(url)
        if pt.ok:
            return _build_passthrough_result(
                url,
                query,
                body=pt.raw_bytes,
                content_type=pt.content_type,
                fetcher_name="passthrough",
                t_start=t_start,
                fetch_ms=pt.elapsed_ms,
                truncated=pt.truncated,
            )
        logger.info(
            "passthrough URL hint matched but fetch failed (%s); falling through",
            pt.error,
        )
        # Fall through to the generic fetcher chain below.
        fetched = None
        markdown = ""
        fetcher_name = ""
        for fetcher_mod, native_name in _API_FETCHERS:
```

Then `fetched = None; markdown = ""; fetcher_name = ""` is shadowed immediately by the `for` loop body — but that only executes when a fetcher matches. To keep the logic simple and avoid duplicate loops, restructure: extract the current generic-fetcher loop + playwright fallback into a helper. See step 4b.

- [ ] **Step 4b: Extract generic-fetch into `_fetch_html(url)` helper**

To avoid duplicating the fetcher chain across the PDF/passthrough/normal branches, refactor. Replace the entire section in `_run_full_pipeline` from `# 1. Fetch → markdown` through the end of the `else:` branch that produces `markdown` and `fetcher_name`, with:

```python
    # 1. Fetch → markdown (or short-circuit for PDF / passthrough)
    if _is_pdf_url(url):
        fetched = pdf.fetch(url)
        markdown = fetched.markdown
        fetcher_name = "pdf"
    elif passthrough.matches(url):
        pt = passthrough.fetch(url)
        if pt.ok:
            return _build_passthrough_result(
                url,
                query,
                body=pt.raw_bytes,
                content_type=pt.content_type,
                fetcher_name="passthrough",
                t_start=t_start,
                fetch_ms=pt.elapsed_ms,
                truncated=pt.truncated,
            )
        logger.info(
            "passthrough URL hint matched but fetch failed (%s); falling through",
            pt.error,
        )
        fetched, markdown, fetcher_name = _fetch_html(url)
    else:
        fetched, markdown, fetcher_name = _fetch_html(url)
```

And define `_fetch_html` above `_run_full_pipeline`:

```python
def _fetch_html(url: str) -> tuple[object, str, str]:
    """Run the API-fetcher chain, falling back to Playwright + Trafilatura.

    Returns (fetched, markdown, fetcher_name). `fetched` is whatever
    the chosen fetcher produced; callers use its `.ok`, `.error`,
    `.elapsed_ms`, and (for Playwright) `.content_type`.
    """
    for fetcher_mod, native_name in _API_FETCHERS:
        if fetcher_mod.matches(url):
            fetched = fetcher_mod.fetch(url)
            if fetched.fetcher == native_name:
                return fetched, fetched.markdown, native_name
            # API fetcher fell back to playwright — re-extract.
            markdown = extraction.html_to_markdown(fetched.html) if fetched.ok else ""
            return fetched, markdown, "playwright+trafilatura"
    fetched = playwright.fetch(url)
    markdown = extraction.html_to_markdown(fetched.html) if fetched.ok else ""
    return fetched, markdown, "playwright+trafilatura"
```

- [ ] **Step 5: Run the new tests**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: all PASS.

- [ ] **Step 6: Run parity matrix to confirm no regression**

Run: `mamba run -n trawl python tests/test_pipeline.py`
Expected: `12/12 PASS`.

- [ ] **Step 7: Commit**

```bash
git add src/trawl/pipeline.py tests/test_passthrough.py
git commit -m "feat(pipeline): short-circuit to raw passthrough for structured data URLs"
```

---

## Task 6: Pipeline — Playwright post-detection passthrough

**Files:**
- Modify: `src/trawl/pipeline.py`
- Modify: `src/trawl/fetchers/passthrough.py`
- Modify: `tests/test_passthrough.py`

Handles suffix-less URLs like `/api/weather` whose passthrough nature is only revealed by the response `Content-Type` after Playwright has already loaded the page.

- [ ] **Step 1: Add `fetch_raw_body` to passthrough module**

Append to `src/trawl/fetchers/passthrough.py`:

```python
def fetch_raw_body(
    url: str,
    *,
    timeout_s: float = 15.0,
) -> PassthroughResult:
    """Re-fetch `url` via httpx to recover the original bytes.

    Used after Playwright has detected a passthrough Content-Type on
    the navigation response. Playwright's rendered HTML at that point
    is Chromium's JSON/XML viewer DOM, not the raw body, so we issue a
    direct httpx GET. Unlike `fetch`, this does not enforce the URL-
    hint gate or fail on Content-Type mismatch — the caller already
    decided the body is structured data.
    """
    t0 = time.monotonic()
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=timeout_s,
        ) as resp:
            if resp.status_code >= 400:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=resp.headers.get("content-type"),
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"HTTP {resp.status_code}",
                )
            ct = resp.headers.get("content-type")
            buf = bytearray()
            truncated = False
            for chunk in resp.iter_bytes():
                remaining = PASSTHROUGH_MAX_BYTES - len(buf)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    buf.extend(chunk[:remaining])
                    truncated = True
                    break
                buf.extend(chunk)
            if not buf and not truncated:
                return PassthroughResult(
                    url=url,
                    raw_bytes=b"",
                    content_type=ct,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error="empty body",
                )
            return PassthroughResult(
                url=url,
                raw_bytes=bytes(buf),
                content_type=ct,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                truncated=truncated,
            )
    except httpx.HTTPError as e:
        return PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
```

- [ ] **Step 2: Write failing unit test for the post-detection branch**

The test monkeypatches `_fetch_html` so we don't need live Playwright.

Append to `tests/test_passthrough.py`:

```python
def test_pipeline_post_detection_passthrough(http_server, monkeypatch):
    base, handler = http_server
    body = b'{"post": "detect"}'
    handler.response_body = body
    handler.response_ct = "application/json"

    # Simulate Playwright: return a FetchResult with content_type set but
    # a garbled HTML body (as Chromium's JSON viewer would produce).
    from trawl import pipeline as pipeline_mod
    from trawl.fetchers.playwright import FetchResult as PwFetchResult

    def fake_fetch_html(url: str):
        fr = PwFetchResult(
            url=url,
            html="<html><pre>{&quot;post&quot;: &quot;detect&quot;}</pre></html>",
            markdown="",
            raw_html="",
            fetcher="playwright",
            elapsed_ms=5,
            content_type="application/json; charset=utf-8",
        )
        return fr, "garbage-markdown", "playwright+trafilatura"

    monkeypatch.setattr(pipeline_mod, "_fetch_html", fake_fetch_html)

    # URL has no passthrough suffix so we go through _fetch_html.
    r = fetch_relevant(f"{base}/api/weather")
    assert r.error is None, r.error
    assert r.path == "raw_passthrough"
    assert r.fetcher_used == "playwright+passthrough"
    assert r.chunks[0]["text"] == body.decode("utf-8")
    assert r.content_type == "application/json; charset=utf-8"


def test_pipeline_post_detection_passthrough_fetch_fails(monkeypatch):
    """When re-fetch fails, return terminal error rather than falling back."""
    from trawl import pipeline as pipeline_mod
    from trawl.fetchers import passthrough as pt_mod
    from trawl.fetchers.playwright import FetchResult as PwFetchResult

    def fake_fetch_html(url: str):
        return (
            PwFetchResult(
                url=url,
                html="<html></html>",
                markdown="",
                raw_html="",
                fetcher="playwright",
                elapsed_ms=5,
                content_type="application/json",
            ),
            "",
            "playwright+trafilatura",
        )

    def fake_raw(url, *, timeout_s=15.0):
        return pt_mod.PassthroughResult(
            url=url,
            raw_bytes=b"",
            content_type=None,
            elapsed_ms=1,
            error="ConnectError: boom",
        )

    monkeypatch.setattr(pipeline_mod, "_fetch_html", fake_fetch_html)
    monkeypatch.setattr(pt_mod, "fetch_raw_body", fake_raw)

    r = fetch_relevant("https://example.test/api/x")
    assert r.path == "raw_passthrough"
    assert r.error and "passthrough raw body fetch failed" in r.error
    assert r.chunks == []
```

- [ ] **Step 3: Run tests, expect failure**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v -k post_detection`
Expected: FAIL (branch not implemented).

- [ ] **Step 4: Add the post-detection branch in `_run_full_pipeline`**

In `src/trawl/pipeline.py`, immediately after the fetch dispatch (after the `else: fetched, markdown, fetcher_name = _fetch_html(url)` line), insert:

```python
    # 1b. Playwright-path post-detection passthrough. When a suffix-less
    # URL returns JSON/XML, Chromium wraps it in a viewer DOM — so we
    # discard the rendered HTML and re-fetch the raw bytes via httpx.
    ct = getattr(fetched, "content_type", None)
    if passthrough.is_passthrough_content_type(ct):
        pt = passthrough.fetch_raw_body(url)
        if pt.ok:
            return _build_passthrough_result(
                url,
                query,
                body=pt.raw_bytes,
                content_type=pt.content_type or ct,
                fetcher_name="playwright+passthrough",
                t_start=t_start,
                fetch_ms=fetched.elapsed_ms + pt.elapsed_ms,
                truncated=pt.truncated,
            )
        return _error_result(
            url,
            query or "",
            f"passthrough raw body fetch failed: {pt.error}",
            t_start,
            fetcher_used="playwright+passthrough",
            fetch_ms=fetched.elapsed_ms + pt.elapsed_ms,
            page_chars=0,
            path="raw_passthrough",
            content_type=ct,
        )
```

Also update `_error_result` signature if needed: it already accepts `**overrides`, so `content_type="..."` passes through. But `PipelineResult` must accept `content_type` and `truncated` in its `__init__` — confirmed in Task 1.

- [ ] **Step 5: Run tests**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: all PASS.

- [ ] **Step 6: Run parity matrix**

Run: `mamba run -n trawl python tests/test_pipeline.py`
Expected: `12/12 PASS`.

- [ ] **Step 7: Commit**

```bash
git add src/trawl/pipeline.py src/trawl/fetchers/passthrough.py tests/test_passthrough.py
git commit -m "feat(pipeline): detect passthrough via Playwright Content-Type post-check"
```

---

## Task 7: MCP server smoke test

**Files:**
- Modify: `tests/test_mcp_server.py`

- [ ] **Step 1: Extend the MCP smoke test with a passthrough case**

The existing test uses a live URL (`https://example.com/`). Adding a live JSON URL would make CI flaky. Instead, reuse the local-server fixture from `test_passthrough.py` by starting the HTTP server inline.

Replace the body of `run()` in `tests/test_mcp_server.py` (keep all current assertions) and add a second `call_tool` invocation after the existing one:

Near the top of `tests/test_mcp_server.py`, add:

```python
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _PassthroughHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **kw):
        pass

    def do_GET(self):
        body = b'{"mcp": "passthrough"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_local_server() -> tuple[str, ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PassthroughHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", server
```

Then at the end of the existing `run()` function, just before `return 0`, add:

```python
            base, server = _start_local_server()
            try:
                print("→ calling fetch_page on local JSON endpoint")
                call_result = await session.call_tool(
                    "fetch_page",
                    {"url": f"{base}/data.json"},
                )
                assert call_result.content, "empty content returned"
                payload = json.loads(call_result.content[0].text)
                print(f"   passthrough payload keys: {sorted(payload.keys())}")
                assert payload["ok"] is True, f"passthrough call failed: {payload.get('error')}"
                assert payload["path"] == "raw_passthrough", payload.get("path")
                assert payload["content_type"] == "application/json"
                assert payload["truncated"] is False
                assert payload["chunks"][0]["text"] == '{"mcp": "passthrough"}'
            finally:
                server.shutdown()
                server.server_close()
```

- [ ] **Step 2: Run the MCP smoke test**

Run: `mamba run -n trawl python tests/test_mcp_server.py`
Expected: prints both the original and the passthrough call results; exits 0.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_server.py
git commit -m "test(mcp): exercise raw-passthrough path via stdio"
```

---

## Task 8: Documentation

**Files:**
- Modify: `.env.example`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document env var in `.env.example`**

Append to `.env.example`:

```
# Hard cap on raw-passthrough response size in bytes. When fetch_page
# receives JSON/XML/RSS/Atom, the body is returned as-is (no extraction,
# no chunking, no embedding) up to this many bytes. Default: 262144
# (256 KB ≈ 64K tokens — fits most local LLM context windows).
# TRAWL_PASSTHROUGH_MAX_BYTES=262144
```

- [ ] **Step 2: Document in `CLAUDE.md`**

In `CLAUDE.md`, inside the "Things NOT to change without re-running the full test matrix" table, add a row:

```
| `fetchers/passthrough.py` | `PASSTHROUGH_MAX_BYTES` env default `262144` | 256 KB ≈ 64K tokens; weather-like APIs fit, larger than local LLM contexts |
```

And inside the llama-server endpoint map section's bullet list (around the slot-pinning line), add a new bullet **outside** the table, after the slot-pinning bullet:

```
  - **Raw passthrough** — JSON/XML/RSS/Atom responses are returned as-is
    without extraction. URL suffixes (`.json`, `.xml`, `.rss`, `.atom`)
    take an httpx fast path; suffix-less API endpoints are detected by
    response `Content-Type`. Byte cap via `TRAWL_PASSTHROUGH_MAX_BYTES`
    (default 256 KB).
```

- [ ] **Step 3: Commit**

```bash
git add .env.example CLAUDE.md
git commit -m "docs: document TRAWL_PASSTHROUGH_MAX_BYTES and passthrough behaviour"
```

---

## Task 9: Final verification

- [ ] **Step 1: Full passthrough test suite**

Run: `mamba run -n trawl pytest tests/test_passthrough.py -v`
Expected: all PASS.

- [ ] **Step 2: Parity matrix**

Run: `mamba run -n trawl python tests/test_pipeline.py`
Expected: `12/12 PASS`.

- [ ] **Step 3: MCP smoke**

Run: `mamba run -n trawl python tests/test_mcp_server.py`
Expected: exits 0, prints both original and passthrough call results.

- [ ] **Step 4: Manual sanity check against a real JSON API (optional)**

```bash
mamba run -n trawl python -c "
from trawl import fetch_relevant, to_dict
import json
r = fetch_relevant('https://httpbin.org/json')
d = to_dict(r)
print('path:', d['path'])
print('content_type:', d['content_type'])
print('truncated:', d['truncated'])
print('text[:120]:', d['chunks'][0]['text'][:120])
"
```

Expected: `path: raw_passthrough`, `content_type: application/json`, `truncated: False`, and the first 120 chars of the body visible verbatim (no Chromium viewer artefacts).
