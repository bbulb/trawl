# C3 reranker title-injection spike — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread a page-level title into the bge-reranker-v2-m3 input, behind a feature flag, and A/B it against the 12-case parity matrix.

**Architecture:** Add a generic `extract_title()` helper (HTML `<title>` first, markdown H1 fallback, else `""`). Thread the extracted title from `pipeline.fetch_relevant()` through both the profile and non-profile paths into `reranking.rerank()`. The reranker branches its document string on `TRAWL_RERANK_INCLUDE_TITLE` and on which of title/heading are non-empty.

**Tech Stack:** Python 3.10+, BeautifulSoup (already a dep), `httpx` (already a dep). No new libraries.

**Spec:** `docs/superpowers/specs/2026-04-15-c3-reranker-title-spike-design.md`

**Pre-flight (run once before starting):**
- Working directory: `/Users/lyla/workspaces/trawl`
- Env: `mamba activate trawl`
- Smoke: `python tests/test_pipeline.py --only kbo_schedule` should pass. If not, fix before touching this plan.

---

## File structure

- Modify: `src/trawl/extraction.py` — add `extract_title(html, markdown) -> str`
- Modify: `src/trawl/reranking.py` — accept `page_title`, read env flag, build documents with title/section labels
- Modify: `src/trawl/pipeline.py` — extract title once per call, thread into both rerank call sites; add `page_title` field to `PipelineResult`
- Modify: `.env.example` — document `TRAWL_RERANK_INCLUDE_TITLE`
- Create: `tests/test_extract_title.py` — unit tests for the title helper
- Create: `tests/test_reranking_format.py` — unit tests for reranker input formatting (no network; test the pure string-assembly branch)
- Create: `notes/c3-spike-results.md` — A/B results (gitignored; `notes/` is already in `.gitignore`)

---

### Task 1: `extract_title()` helper with tests

**Files:**
- Create: `tests/test_extract_title.py`
- Modify: `src/trawl/extraction.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_extract_title.py`:

```python
"""Unit tests for extract_title()."""

from trawl.extraction import extract_title


def test_html_title_tag():
    html = "<html><head><title>  The Real Title  </title></head><body>x</body></html>"
    assert extract_title(html=html, markdown="") == "The Real Title"


def test_html_title_tag_beats_markdown_h1():
    html = "<html><head><title>HTML Title</title></head><body>x</body></html>"
    markdown = "# Markdown H1\n\nbody"
    assert extract_title(html=html, markdown=markdown) == "HTML Title"


def test_markdown_h1_fallback():
    assert extract_title(html="", markdown="# My Doc\n\nbody") == "My Doc"


def test_markdown_h1_fallback_when_html_has_no_title():
    html = "<html><body>no title tag</body></html>"
    markdown = "# Fallback H1\n\nbody"
    assert extract_title(html=html, markdown=markdown) == "Fallback H1"


def test_markdown_h1_skips_h2():
    md = "## Not This\n\nbody\n\n# This One"
    assert extract_title(html="", markdown=md) == "This One"


def test_empty_inputs():
    assert extract_title(html="", markdown="") == ""


def test_whitespace_only_title():
    html = "<html><head><title>   </title></head><body>x</body></html>"
    assert extract_title(html=html, markdown="") == ""


def test_malformed_html_does_not_raise():
    # BeautifulSoup must tolerate unclosed tags etc.
    html = "<title>Broken"
    # Either returns "Broken" or "" — both acceptable; must not raise.
    out = extract_title(html=html, markdown="")
    assert isinstance(out, str)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mamba run -n trawl pytest tests/test_extract_title.py -v`
Expected: ImportError / all fail — `extract_title` does not exist yet.

- [ ] **Step 3: Implement `extract_title()` in `src/trawl/extraction.py`**

Append to `src/trawl/extraction.py` (after the existing imports block at line 24, add the function at the bottom of the file; keep existing code untouched):

```python
import re

_MD_H1_RE = re.compile(r"^# +(.+?)\s*$", re.MULTILINE)


def extract_title(*, html: str, markdown: str) -> str:
    """Return a best-effort page title.

    Resolution order:
      1. HTML <title> tag content, whitespace-stripped.
      2. First markdown H1 line (`# ...`), whitespace-stripped.
      3. Empty string.

    Never raises. Callers should treat "" as "no title available".
    """
    if html:
        try:
            soup = BeautifulSoup(html, "html.parser")
            t = soup.title
            if t and t.string:
                stripped = t.string.strip()
                if stripped:
                    return stripped
        except Exception:
            pass

    if markdown:
        m = _MD_H1_RE.search(markdown)
        if m:
            return m.group(1).strip()

    return ""
```

Note: `re` must be imported at the top of the file if not already.

- [ ] **Step 4: Run tests to verify they pass**

Run: `mamba run -n trawl pytest tests/test_extract_title.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/extraction.py tests/test_extract_title.py
git commit -m "feat(extraction): add extract_title() helper for reranker title injection"
```

---

### Task 2: Reranker accepts `page_title` and reads env flag

**Files:**
- Create: `tests/test_reranking_format.py`
- Modify: `src/trawl/reranking.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_reranking_format.py`:

```python
"""Unit tests for reranker document-string assembly.

We test the pure string-building branch only — no network. The real
rerank() call goes over HTTP; these tests target a refactored private
helper `_build_documents(scored, page_title, include_title)`.
"""

from dataclasses import dataclass

from trawl.chunking import Chunk
from trawl.retrieval import ScoredChunk
from trawl.reranking import _build_documents


def _sc(text, heading_path=None):
    return ScoredChunk(
        chunk=Chunk(text=text, heading_path=heading_path or []),
        score=0.0,
    )


def test_title_and_heading():
    docs = _build_documents(
        [_sc("body text", ["Top", "Sub"])],
        page_title="The Page",
        include_title=True,
    )
    assert docs == ["Title: The Page\nSection: Top > Sub\n\nbody text"]


def test_title_only():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="The Page",
        include_title=True,
    )
    assert docs == ["Title: The Page\n\nbody text"]


def test_heading_only():
    docs = _build_documents(
        [_sc("body text", ["Top"])],
        page_title="",
        include_title=True,
    )
    assert docs == ["Top\n\nbody text"]


def test_neither():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="",
        include_title=True,
    )
    assert docs == ["body text"]


def test_include_title_disabled_drops_title_keeps_heading():
    docs = _build_documents(
        [_sc("body text", ["Top"])],
        page_title="The Page",
        include_title=False,
    )
    assert docs == ["Top\n\nbody text"]


def test_include_title_disabled_without_heading():
    docs = _build_documents(
        [_sc("body text", [])],
        page_title="The Page",
        include_title=False,
    )
    assert docs == ["body text"]


def test_embed_text_preferred_over_text():
    # When chunk has embed_text set, that is what's fed to the reranker
    # (mirrors current behaviour in reranking.py).
    c = Chunk(text="short", heading_path=[], embed_text="longer embed text")
    docs = _build_documents(
        [ScoredChunk(chunk=c, score=0.0)],
        page_title="T",
        include_title=True,
    )
    assert docs == ["Title: T\n\nlonger embed text"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `mamba run -n trawl pytest tests/test_reranking_format.py -v`
Expected: ImportError — `_build_documents` does not exist yet.

- [ ] **Step 3: Refactor `reranking.py` to add `_build_documents()` and wire up the flag**

Replace the contents of `src/trawl/reranking.py` with:

```python
"""Cross-encoder reranking via a local llama-server /v1/rerank endpoint.

Rescores bi-encoder candidates using bge-reranker-v2-m3. Designed as a
second stage after retrieval.retrieve() — call with the top-2k cosine
candidates and get back top-k by cross-encoder relevance.
"""

from __future__ import annotations

import logging
import os

import httpx

from .retrieval import ScoredChunk

logger = logging.getLogger(__name__)

DEFAULT_RERANKER_URL = os.environ.get(
    "TRAWL_RERANK_URL",
    "http://localhost:8083/v1",
)
DEFAULT_RERANKER_MODEL = os.environ.get(
    "TRAWL_RERANK_MODEL",
    "bge-reranker-v2-m3",
)
HTTP_TIMEOUT_S = 30.0


def _include_title_default() -> bool:
    return os.environ.get("TRAWL_RERANK_INCLUDE_TITLE", "1") != "0"


def _build_documents(
    scored: list[ScoredChunk],
    page_title: str,
    include_title: bool,
) -> list[str]:
    """Assemble the per-candidate document strings fed to the reranker."""
    docs: list[str] = []
    for s in scored:
        body = s.chunk.embed_text or s.chunk.text
        heading = s.chunk.heading
        title = page_title if include_title else ""
        if title and heading:
            docs.append(f"Title: {title}\nSection: {heading}\n\n{body}")
        elif title:
            docs.append(f"Title: {title}\n\n{body}")
        elif heading:
            docs.append(f"{heading}\n\n{body}")
        else:
            docs.append(body)
    return docs


def rerank(
    query: str,
    scored: list[ScoredChunk],
    *,
    k: int,
    page_title: str = "",
    base_url: str = DEFAULT_RERANKER_URL,
    model: str = DEFAULT_RERANKER_MODEL,
) -> list[ScoredChunk]:
    """Rerank candidates via cross-encoder. Returns top-k by relevance.

    On any HTTP error, logs a warning and returns the input list
    truncated to k (graceful fallback to cosine ranking).
    """
    if not scored or k <= 0:
        return scored[:k]

    documents = _build_documents(
        scored,
        page_title=page_title,
        include_title=_include_title_default(),
    )

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.post(
                f"{base_url}/rerank",
                json={
                    "model": model,
                    "query": query,
                    "documents": documents,
                },
            )
            r.raise_for_status()
            results = r.json()["results"]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        logger.warning("reranker unavailable, falling back to cosine: %s", e)
        return scored[:k]

    reranked = []
    for item in results:
        idx = item["index"]
        sc = scored[idx]
        reranked.append(ScoredChunk(chunk=sc.chunk, score=item["relevance_score"]))

    reranked.sort(key=lambda s: -s.score)
    return reranked[:k]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `mamba run -n trawl pytest tests/test_reranking_format.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/reranking.py tests/test_reranking_format.py
git commit -m "feat(reranking): title + section fields in reranker input, flag-gated"
```

---

### Task 3: Thread `page_title` through `pipeline.py`

**Files:**
- Modify: `src/trawl/pipeline.py`

- [ ] **Step 1: Add `page_title` to `PipelineResult`**

In `src/trawl/pipeline.py`, modify the `PipelineResult` dataclass (around line 55-80). Add the field at the end of the existing fields, after `content_type: str | None = None`:

```python
    page_title: str = ""
```

- [ ] **Step 2: Extract title in the profile path and thread into rerank**

In `_assemble_profile_result` (around line 238-316), after the markdown is built and before the rerank call, extract the title. Change the section starting around line 254:

Find:
```python
    t_chunk = time.monotonic()
    md = extraction.html_to_markdown(subtree_html)
    chunks = chunking.chunk_markdown(md)
    chunk_ms = int((time.monotonic() - t_chunk) * 1000)

    # Fields shared by every return from this function.
    base_kwargs = {
```

Replace with:
```python
    t_chunk = time.monotonic()
    md = extraction.html_to_markdown(subtree_html)
    chunks = chunking.chunk_markdown(md)
    chunk_ms = int((time.monotonic() - t_chunk) * 1000)

    # Profile path operates on a subtree; the full-page <title> isn't
    # available here, so fall back to markdown H1 only.
    page_title = extraction.extract_title(html="", markdown=md)

    # Fields shared by every return from this function.
    base_kwargs = {
```

Add `"page_title": page_title,` to `base_kwargs` (right after `"profile_hash": profile.url_hash,`).

Update the rerank call at line 298:

Find:
```python
            final_scored = reranking.rerank(query, retrieved.scored, k=chosen_k)
```

Replace with:
```python
            final_scored = reranking.rerank(
                query, retrieved.scored, k=chosen_k, page_title=page_title
            )
```

- [ ] **Step 3: Extract title in the non-profile path and thread into rerank**

In the non-profile path function (around line 648-780, docstring: `"""Non-profile pipeline: fetch → extract → chunk → (HyDE) → retrieve → rerank."""`), local variable names are `fetched` (FetchResult; PDF path does not have a `.html` attribute, Playwright path does) and `markdown`.

Insert a title extraction line immediately after chunking, one line above `# 3. Optional HyDE` (at line 727). Use `getattr()` so PDF path (no HTML) still works:

```python
    page_title = extraction.extract_title(
        html=getattr(fetched, "html", "") or "",
        markdown=markdown,
    )
```

Update the rerank call at line 758:

Find:
```python
        final_scored = reranking.rerank(query, retrieved.scored, k=chosen_k)
```

Replace with:
```python
        final_scored = reranking.rerank(
            query, retrieved.scored, k=chosen_k, page_title=page_title
        )
```

Add `page_title=page_title,` to the `PipelineResult(...)` constructor at the end of this function (the return near lines 763-780). Place it right after `rerank_ms=rerank_ms,`.

- [ ] **Step 4: Run the full parity matrix to verify no regression with flag default on**

Run: `mamba run -n trawl env TRAWL_RERANK_INCLUDE_TITLE=1 python tests/test_pipeline.py`
Expected: 12/12 pass.

If any case regresses, STOP. Diagnose before proceeding — the spike is specifically set up to detect this. Run the failing case with `--only <case_id> --verbose` and compare chunks.

- [ ] **Step 5: Run parity matrix with flag off (must match current main behaviour)**

Run: `mamba run -n trawl env TRAWL_RERANK_INCLUDE_TITLE=0 python tests/test_pipeline.py`
Expected: 12/12 pass.

- [ ] **Step 6: Run MCP smoke test**

Run: `mamba run -n trawl python tests/test_mcp_server.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/trawl/pipeline.py
git commit -m "feat(pipeline): thread page_title into reranker; expose on PipelineResult"
```

---

### Task 4: Document the flag

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Add the flag to `.env.example`**

In `.env.example`, locate the block documenting `TRAWL_RERANK_URL` / `TRAWL_RERANK_MODEL` (around lines 12-14). Append underneath that block:

```
# Include page title + section heading as labelled fields in the
# reranker input (DeepQSE-style). Default on. Set to 0 to restore
# the legacy heading-only format.
# TRAWL_RERANK_INCLUDE_TITLE=1
```

- [ ] **Step 2: Commit**

```bash
git add .env.example
git commit -m "docs: document TRAWL_RERANK_INCLUDE_TITLE flag"
```

---

### Task 5: A/B measurement and decision

**Files:**
- Create: `notes/c3-spike-results.md` (gitignored — `notes/` is already in `.gitignore`)

- [ ] **Step 1: Capture baseline (flag off) output**

Run and save:

```bash
mamba run -n trawl env TRAWL_RERANK_INCLUDE_TITLE=0 \
  python tests/test_pipeline.py --verbose > /tmp/c3_off.log 2>&1
```

Expected: 12/12 pass in the summary.

- [ ] **Step 2: Capture treatment (flag on) output**

Run and save:

```bash
mamba run -n trawl env TRAWL_RERANK_INCLUDE_TITLE=1 \
  python tests/test_pipeline.py --verbose > /tmp/c3_on.log 2>&1
```

Expected: 12/12 pass in the summary.

- [ ] **Step 3: Diff per-case top-k scores**

Compare `/tmp/c3_off.log` vs `/tmp/c3_on.log`. For each of the 12 cases, note:
- top-1 chunk identity: same / different
- top-1 rerank score: baseline → treatment
- whether the ground-truth chunk's rank changed

Write a summary into `notes/c3-spike-results.md` with a per-case table. Template:

```markdown
# C3 spike results — 2026-04-15

Flag: TRAWL_RERANK_INCLUDE_TITLE
Baseline: off (legacy)
Treatment: on (title + section labels)

Parity: both 12/12.

| case | top-1 same? | score off | score on | rank of ground-truth (off → on) |
|---|---|---|---|---|
| kbo_schedule | ... | ... | ... | ... |
| ... | ... | ... | ... | ... |

## Decision
- Accept / Reject: ...
- Rationale: ...
```

- [ ] **Step 4: Apply acceptance criteria**

Per spec (§Measurement protocol, Acceptance criteria):
- No parity regression (12/12 both).
- At least one case with a measurable positive shift — either
  (a) higher top-1 rerank score on ground-truth chunk, OR
  (b) ground-truth chunk moves up in top-k on a near-miss case.
- No case shows a negative shift that would be a regression if
  ground truth were tightened.

If all three hold → **Accept**. If not → **Reject**.

- [ ] **Step 5: Update RESEARCH.md §C3**

If accepted, change the header in `notes/RESEARCH.md` from:
```
## C3. DeepQSE식 2-stage + 경량 reranker 학습  — `status: pending`
```
to:
```
## C3. DeepQSE식 2-stage + 경량 reranker 학습  — `status: accepted (title-injection only, 2026-04-15)`
```

Append a short "Spike outcome (2026-04-15)" subsection with the result table and the link to `docs/superpowers/specs/2026-04-15-c3-reranker-title-spike-design.md`. Mention that adapter fine-tune remains deferred.

If rejected: change status to `rejected (2026-04-15)`, append a "Spike outcome" subsection mirroring C1's rejection record, and revert the code with:

```bash
git revert <task3_commit_sha> <task2_commit_sha> <task1_commit_sha>
```

(Keep the `.env.example` change and the RESEARCH.md update.)

- [ ] **Step 6: Commit the RESEARCH.md update**

`notes/RESEARCH.md` is **not** gitignored even though `notes/` is (see `.gitignore` and recent commit `fa597d6` — `RESEARCH.md` is tracked specifically). Verify with `git status` before committing.

```bash
git add notes/RESEARCH.md
git commit -m "docs(research): record C3 spike outcome"
```

---

## Done criteria

- `extract_title()` unit tests pass.
- `_build_documents()` unit tests pass.
- Parity matrix 12/12 under both flag values.
- MCP smoke test passes.
- `notes/c3-spike-results.md` contains per-case table and decision.
- `notes/RESEARCH.md` §C3 has updated status.
- If accepted: code lives on `develop`. If rejected: code changes reverted, decision documented.
