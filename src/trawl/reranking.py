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


def rerank(
    query: str,
    scored: list[ScoredChunk],
    *,
    k: int,
    base_url: str = DEFAULT_RERANKER_URL,
    model: str = DEFAULT_RERANKER_MODEL,
) -> list[ScoredChunk]:
    """Rerank candidates via cross-encoder. Returns top-k by relevance.

    On any HTTP error, logs a warning and returns the input list
    truncated to k (graceful fallback to cosine ranking).
    """
    if not scored or k <= 0:
        return scored[:k]

    documents = [
        (s.chunk.heading + "\n\n" + (s.chunk.embed_text or s.chunk.text))
        if s.chunk.heading
        else (s.chunk.embed_text or s.chunk.text)
        for s in scored
    ]

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

    # Map reranker scores back to ScoredChunk objects.
    reranked = []
    for item in results:
        idx = item["index"]
        sc = scored[idx]
        reranked.append(ScoredChunk(chunk=sc.chunk, score=item["relevance_score"]))

    reranked.sort(key=lambda s: -s.score)
    return reranked[:k]
