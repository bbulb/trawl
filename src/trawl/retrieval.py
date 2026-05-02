"""Embed query + chunks via a bge-m3 llama-server, cosine top-k.

Hits the OpenAI-compatible /v1/embeddings endpoint. Default is a local
llama-server at localhost:8081 with bge-m3 loaded; any OpenAI-
compatible embedding endpoint works if you override TRAWL_EMBED_URL.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .bm25 import bm25_rank
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
    n_chunks_embedded: int = 0
    retrieval_mode: str = "dense"
    query_type: str = "concept"
    fusion_weights: dict[str, float] | None = None
    rank_diagnostics: list[dict] | None = None
    sparse_rank_error: str | None = None


@dataclass
class SparseRankResult:
    ranking: list[int]
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


_IDENTIFIER_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*[.:/][A-Za-z0-9_./:-]+|[A-Za-z_][A-Za-z0-9_]*\(\))"
)
_CODE_HINT_RE = re.compile(
    r"\b(api|class|cli|def|function|handler|method|module|parameter|signature|"
    r"traceback|import|async|await|exception|error|config|endpoint|sdk)\b",
    re.IGNORECASE,
)


def _classify_query(query: str) -> str:
    if _IDENTIFIER_RE.search(query):
        return "identifier"
    if "`" in query:
        return "identifier"
    if _CODE_HINT_RE.search(query) and re.search(r"[A-Za-z_][A-Za-z0-9_]*", query):
        return "identifier"
    return "concept"


def _fusion_weights(query_type: str, ranker_names: list[str]) -> dict[str, float]:
    if query_type == "identifier":
        base = {"dense": 0.6, "bm25": 3.0, "bge_m3_sparse": 3.0}
    else:
        base = {"dense": 1.2, "bm25": 0.8, "bge_m3_sparse": 0.8}
    return {name: base.get(name, 1.0) for name in ranker_names}


def _weighted_rrf_fuse(
    rankings: dict[str, list[int]],
    *,
    weights: dict[str, float],
    k: int,
) -> tuple[list[int], dict[int, dict]]:
    scores: dict[int, float] = {}
    diagnostics: dict[int, dict] = {}
    for name, ranking in rankings.items():
        weight = weights.get(name, 1.0)
        for rank, idx in enumerate(ranking):
            contribution = weight * (1.0 / (k + rank))
            scores[idx] = scores.get(idx, 0.0) + contribution
            diag = diagnostics.setdefault(
                idx,
                {"chunk_index": idx, "ranks": {}, "contributions": {}, "fusion_score": 0.0},
            )
            diag["ranks"][name] = rank
            diag["contributions"][name] = round(contribution, 6)
            diag["fusion_score"] = round(scores[idx], 6)
    ordered = sorted(scores, key=lambda i: -scores[i])
    return ordered, diagnostics


def _ranking_from_scores(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: -scores[i])


def _ranking_from_sparse_payload(payload: Any, n_documents: int) -> list[int]:
    if isinstance(payload, dict) and isinstance(payload.get("ranking"), list):
        ranking = [int(i) for i in payload["ranking"]]
        return [i for i in ranking if 0 <= i < n_documents]
    if isinstance(payload, dict) and isinstance(payload.get("scores"), list):
        scores = [float(s) for s in payload["scores"]]
        if len(scores) == n_documents:
            return _ranking_from_scores(scores)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        rows = payload["data"]
        pairs = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if "index" in row and "score" in row:
                idx = int(row["index"])
                if 0 <= idx < n_documents:
                    pairs.append((idx, float(row["score"])))
        if pairs:
            return [idx for idx, _score in sorted(pairs, key=lambda p: -p[1])]
    return []


def _bge_m3_sparse_rank(
    query: str,
    documents: list[str],
    *,
    endpoint: str,
    model: str,
) -> SparseRankResult:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            response = client.post(
                endpoint,
                json={"model": model, "query": query, "documents": documents},
            )
            response.raise_for_status()
            ranking = _ranking_from_sparse_payload(response.json(), len(documents))
    except httpx.HTTPError as e:
        return SparseRankResult(ranking=[], error=f"{type(e).__name__}: {e}")
    except (TypeError, ValueError) as e:
        return SparseRankResult(ranking=[], error=f"{type(e).__name__}: {e}")
    if not ranking:
        return SparseRankResult(ranking=[], error="empty sparse ranking")
    return SparseRankResult(ranking=ranking)


def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
    hybrid: bool = False,
    chunk_budget: int = 0,
    context_texts: list[str] | None = None,
) -> RetrievalResult:
    """Embed query + chunks, return the top-k chunks by cosine similarity.

    `extra_query_texts` allows passing HyDE outputs (or any extra reformulations)
    that will be averaged with the query embedding before scoring.

    When `hybrid=True`, a BM25 ranking is computed over the same chunk
    texts and fused with the dense ranking via RRF. The `score` field
    on each returned `ScoredChunk` still holds the raw dense cosine —
    the fused ordering only affects which chunks make the top-k, not
    downstream consumers (telemetry, reranker) that read `score`.

    When `chunk_budget > 0` and `len(chunks) > chunk_budget`, a BM25
    prefilter keeps only the top `chunk_budget` chunks before
    embedding. Caps embedding cost for longform pages (wiki / long
    PDF). `n_chunks_embedded` on the result reports the post-prefilter
    count (equals `len(chunks)` when prefilter is a no-op).
    """
    if not chunks:
        return RetrievalResult(scored=[], elapsed_ms=0, embed_calls=0, n_chunks_embedded=0)

    if context_texts is not None and len(context_texts) != len(chunks):
        return RetrievalResult(
            scored=[],
            elapsed_ms=0,
            embed_calls=0,
            error=(
                f"context_texts length {len(context_texts)} "
                f"does not match chunks length {len(chunks)}"
            ),
            n_chunks_embedded=0,
        )

    t0 = time.monotonic()
    embed_calls = 0

    # Embed the markdown-stripped `embed_text` so links/images don't
    # pollute the vectors. Heading path is still prepended because it
    # carries strong topical signal ("명량 해전" header tells the
    # embedding what the section is about even before the body).
    if context_texts is not None:
        chunk_texts = list(context_texts)
    else:
        chunk_texts = [
            (c.heading + "\n\n" + (c.embed_text or c.text))
            if c.heading
            else (c.embed_text or c.text)
            for c in chunks
        ]

    # BM25 prefilter. Only runs when a positive budget is set and the
    # pool actually exceeds it; otherwise it's cheap no-op. We keep
    # the surviving indices in ascending order so downstream code
    # doesn't have to think about permutation.
    if chunk_budget > 0 and len(chunks) > chunk_budget:
        ranked = bm25_rank(query, chunk_texts)
        kept = sorted(ranked[:chunk_budget])
        chunks = [chunks[i] for i in kept]
        chunk_texts = [chunk_texts[i] for i in kept]

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            query_inputs = [query]
            if extra_query_texts:
                query_inputs.extend(extra_query_texts)
            q_embs = _embed_batch(client, base_url, model, query_inputs)
            embed_calls += 1

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
            n_chunks_embedded=len(chunks),
        )

    # Average query + extras into a single vector for scoring.
    avg_q = [sum(col) / len(col) for col in zip(*q_embs, strict=True)]

    cosines = [cosine(avg_q, ce) for ce in chunk_embs]

    query_type = _classify_query(query)
    if hybrid:
        dense_ranked = sorted(range(len(chunks)), key=lambda i: -cosines[i])
        rankings = {
            "dense": dense_ranked,
            "bm25": bm25_rank(query, chunk_texts),
        }
        sparse_rank_error = None
        sparse_endpoint = os.environ.get("TRAWL_BGE_M3_SPARSE_URL", "").strip()
        if sparse_endpoint:
            sparse_result = _bge_m3_sparse_rank(
                query, chunk_texts, endpoint=sparse_endpoint, model=model
            )
            sparse_rank_error = sparse_result.error
            if sparse_result.ranking:
                rankings["bge_m3_sparse"] = sparse_result.ranking
        weights = _fusion_weights(query_type, list(rankings))
        fused, diagnostics_by_idx = _weighted_rrf_fuse(
            rankings,
            weights=weights,
            k=int(os.environ.get("TRAWL_HYBRID_RRF_K", "60")),
        )
        scored = [ScoredChunk(chunk=chunks[i], score=cosines[i]) for i in fused]
        for idx, diagnostics in diagnostics_by_idx.items():
            diagnostics["pool_index"] = idx
            diagnostics["chunk_index"] = chunks[idx].chunk_index
        rank_diagnostics = [diagnostics_by_idx[i] for i in fused[:k]]
    else:
        scored = [ScoredChunk(chunk=c, score=s) for c, s in zip(chunks, cosines, strict=True)]
        scored.sort(key=lambda s: -s.score)
        weights = {"dense": 1.0}
        rank_diagnostics = None
        sparse_rank_error = None

    return RetrievalResult(
        scored=scored[:k],
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        embed_calls=embed_calls,
        n_chunks_embedded=len(chunks),
        retrieval_mode="hybrid" if hybrid else "dense",
        query_type=query_type,
        fusion_weights=weights,
        rank_diagnostics=rank_diagnostics,
        sparse_rank_error=sparse_rank_error,
    )
