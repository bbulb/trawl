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
