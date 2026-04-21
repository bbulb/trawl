"""Cross-encoder reranking via a local llama-server /v1/rerank endpoint.

Rescores bi-encoder candidates using bge-reranker-v2-m3. Designed as a
second stage after retrieval.retrieve() -- call with the top-2k cosine
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

# Defensive payload caps. The reranker model (bge-reranker-v2-m3) has an
# 8192-token context; requests exceeding it get fast-rejected as HTTP 500
# by the server-side validator. The 2026-04-20 chunk-window-cap spike
# bracketed the failure threshold empirically: 40 000 chars total (query
# + docs) passes; 50 000 chars fails. Defaults pick the safe side of that
# boundary. Normal trawl workload (retrieve_k <= 24 * typical per-doc
# length ~1500 chars ~= 36 000 chars) stays inside, so the caps do not
# bite in the parity / code_heavy_query tests. They exist to prevent
# regressions from future tuning changes or unexpected external callers.
DEFAULT_MAX_DOCS = 30
DEFAULT_MAX_CHARS = 40000
# Minimum per-doc budget retained when proportional truncation fires.
# Anything shorter than this provides no useful signal to the reranker.
MIN_PER_DOC_CHARS = 200


def _include_title_default() -> bool:
    return os.environ.get("TRAWL_RERANK_INCLUDE_TITLE", "1") != "0"


def _max_docs_env() -> int:
    """Max document count passed to the reranker. ``<= 0`` disables the cap."""
    try:
        v = int(os.environ.get("TRAWL_RERANK_MAX_DOCS", str(DEFAULT_MAX_DOCS)))
    except ValueError:
        return DEFAULT_MAX_DOCS
    return v


def _max_chars_env() -> int:
    """Max total character count (query + docs) passed to the reranker.
    ``<= 0`` disables the cap."""
    try:
        v = int(os.environ.get("TRAWL_RERANK_MAX_CHARS", str(DEFAULT_MAX_CHARS)))
    except ValueError:
        return DEFAULT_MAX_CHARS
    return v


def _apply_caps(
    query: str,
    scored: list[ScoredChunk],
    documents: list[str],
) -> tuple[list[ScoredChunk], list[str], dict[str, int]]:
    """Clamp documents to fit the reranker's context window.

    Returns the (possibly reduced) ``scored`` and ``documents`` lists
    plus a telemetry dict describing what fired. The lists stay
    index-aligned so the caller can still map server ``index`` fields
    back to the source chunks.
    """
    max_docs = _max_docs_env()
    max_chars = _max_chars_env()

    pre_docs = len(documents)
    pre_chars = len(query) + sum(len(d) for d in documents)

    docs = documents
    ranked = scored

    if max_docs > 0 and len(docs) > max_docs:
        docs = docs[:max_docs]
        ranked = ranked[:max_docs]

    # Per-doc proportional truncation. Only fires once the doc count has
    # already been clamped above, so `len(docs)` here is the final count.
    if max_chars > 0 and docs:
        total = len(query) + sum(len(d) for d in docs)
        if total > max_chars:
            budget = (max_chars - len(query)) // len(docs)
            budget = max(MIN_PER_DOC_CHARS, budget)
            docs = [d[:budget] for d in docs]

    post_chars = len(query) + sum(len(d) for d in docs)
    telemetry = {
        "pre_docs": pre_docs,
        "post_docs": len(docs),
        "pre_chars": pre_chars,
        "post_chars": post_chars,
    }
    if (
        telemetry["pre_docs"] != telemetry["post_docs"]
        or telemetry["pre_chars"] != telemetry["post_chars"]
    ):
        logger.warning(
            "reranker input capped: docs=%d->%d chars=%d->%d "
            "(TRAWL_RERANK_MAX_DOCS=%d TRAWL_RERANK_MAX_CHARS=%d)",
            telemetry["pre_docs"],
            telemetry["post_docs"],
            telemetry["pre_chars"],
            telemetry["post_chars"],
            max_docs,
            max_chars,
        )
    return ranked, docs, telemetry


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
) -> tuple[list[ScoredChunk], bool]:
    """Rerank candidates via cross-encoder. Returns (top-k, capped).

    `capped` is True when `_apply_caps` dropped documents or truncated
    any doc (same predicate as the existing WARNING log). Remains True
    even when the subsequent HTTP call fails, so callers see the cap
    fired regardless of the downstream outcome.

    On any HTTP error, logs a warning and returns the input list
    truncated to k (graceful fallback to cosine ranking).
    """
    if not scored or k <= 0:
        return scored[:k], False

    documents = _build_documents(
        scored,
        page_title=page_title,
        include_title=_include_title_default(),
    )

    scored, documents, tel = _apply_caps(query, scored, documents)
    capped = tel["pre_docs"] != tel["post_docs"] or tel["pre_chars"] != tel["post_chars"]

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
        return scored[:k], capped

    reranked = []
    for item in results:
        idx = item["index"]
        sc = scored[idx]
        reranked.append(ScoredChunk(chunk=sc.chunk, score=item["relevance_score"]))

    reranked.sort(key=lambda s: -s.score)
    return reranked[:k], capped
