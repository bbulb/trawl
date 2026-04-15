"""Embed query + chunks via a bge-m3 llama-server, cosine top-k.

Hits the OpenAI-compatible /v1/embeddings endpoint. Default is a local
llama-server at localhost:8081 with bge-m3 loaded; any OpenAI-
compatible embedding endpoint works if you override TRAWL_EMBED_URL.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

import httpx

from .chunking import Chunk

DEFAULT_EMBEDDING_URL = os.environ.get("TRAWL_EMBED_URL", "http://localhost:8081/v1")
DEFAULT_EMBEDDING_MODEL = os.environ.get("TRAWL_EMBED_MODEL", "bge-m3")
EMBEDDING_BATCH = 64
# Character cap per input. llama-server is configured with `--ubatch-size 2048`
# and Korean is roughly 1 token/char, so 1800 leaves a ~10% headroom before
# the server starts rejecting with HTTP 500. If ubatch is rolled back to 512,
# drop this to 450 (and lower chunking.max_chars to match).
MAX_EMBED_INPUT_CHARS = 1800
HTTP_TIMEOUT_S = 60.0


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float


@dataclass
class RetrievalResult:
    scored: list[ScoredChunk]
    elapsed_ms: int
    embed_calls: int
    error: str | None = None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _truncate(text: str, limit: int = MAX_EMBED_INPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _embed_batch(
    client: httpx.Client,
    base_url: str,
    model: str,
    texts: list[str],
) -> list[list[float]]:
    safe_texts = [_truncate(t) for t in texts]
    payload = {"model": model, "input": safe_texts}
    r = client.post(f"{base_url}/embeddings", json=payload)
    r.raise_for_status()
    data = r.json()
    return [item["embedding"] for item in data["data"]]


def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
) -> RetrievalResult:
    """Embed query + chunks, return the top-k chunks by cosine similarity.

    `extra_query_texts` allows passing HyDE outputs (or any extra reformulations)
    that will be averaged with the query embedding before scoring.
    """
    if not chunks:
        return RetrievalResult(scored=[], elapsed_ms=0, embed_calls=0)

    t0 = time.monotonic()
    embed_calls = 0
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            query_inputs = [query]
            if extra_query_texts:
                query_inputs.extend(extra_query_texts)
            q_embs = _embed_batch(client, base_url, model, query_inputs)
            embed_calls += 1

            # Embed the markdown-stripped `embed_text` so links/images don't
            # pollute the vectors. Heading path is still prepended because it
            # carries strong topical signal ("명량 해전" header tells the
            # embedding what the section is about even before the body).
            chunk_texts = [
                (c.heading + "\n\n" + (c.embed_text or c.text))
                if c.heading
                else (c.embed_text or c.text)
                for c in chunks
            ]
            chunk_embs: list[list[float]] = []
            for start in range(0, len(chunk_texts), EMBEDDING_BATCH):
                batch = chunk_texts[start : start + EMBEDDING_BATCH]
                chunk_embs.extend(_embed_batch(client, base_url, model, batch))
                embed_calls += 1
    except httpx.HTTPError as e:
        return RetrievalResult(
            scored=[],
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            embed_calls=embed_calls,
            error=f"{type(e).__name__}: {e}",
        )

    # Average query + extras into a single vector for scoring.
    avg_q = [sum(col) / len(col) for col in zip(*q_embs, strict=True)]

    scored = [
        ScoredChunk(chunk=c, score=cosine(avg_q, ce))
        for c, ce in zip(chunks, chunk_embs, strict=True)
    ]
    scored.sort(key=lambda s: -s.score)

    return RetrievalResult(
        scored=scored[:k],
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        embed_calls=embed_calls,
    )
