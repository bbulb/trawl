"""BM25 lexical ranking + RRF fusion used by the hybrid retrieval path.

The dense retrieval path in `retrieval.py` already handles semantic
similarity well for prose; it struggles on code / API-reference pages
where exact symbol matches matter more than vibe. This module adds a
lightweight BM25 layer that is fused with the dense ranking via
Reciprocal Rank Fusion (RRF). Tokenization is rule-based and
multilingual:

- Latin words stay whole so ``asyncio.gather`` → ``["asyncio", "gather"]``
  preserves the identifiers code queries actually look up.
- Hangul runs emit character bigrams so 2+-syllable words remain a
  single shared term (bge-m3 sparse ran the same way).
- Kana / CJK-unified chars emit individual characters (no word
  boundaries in ja/zh).

No HTTP calls, no model dependency — BM25Okapi + regex only.
"""

from __future__ import annotations

import os
import re

from rank_bm25 import BM25Okapi

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_HANGUL_RUN = re.compile(r"[가-힣]+")
_CJK_CHAR = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")

DEFAULT_RRF_K = int(os.environ.get("TRAWL_HYBRID_RRF_K", "60"))


def tokenize(text: str) -> list[str]:
    """Return BM25 terms for mixed-language text.

    Latin: lower-cased word tokens (identifiers kept intact).
    Hangul: character bigrams, 1-syllable runs kept as-is.
    Kana / CJK-unified: single characters.
    """
    if not text:
        return []
    text = text.lower()
    tokens: list[str] = list(_LATIN_WORD.findall(text))
    for run in _HANGUL_RUN.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    tokens.extend(_CJK_CHAR.findall(text))
    return tokens


def bm25_rank(query: str, documents: list[str]) -> list[int]:
    """Rank document indices by BM25Okapi score, best first.

    Returns ``[0, 1, ..., N-1]`` (original order) for empty corpora,
    empty queries, or corpora where every document tokenizes to an
    empty list — BM25 can't say anything useful in those cases and we
    don't want to inject a spurious rank into the RRF fusion.
    """
    if not documents:
        return []
    tokenized_corpus = [tokenize(d) for d in documents]
    if not any(tokenized_corpus):
        return list(range(len(documents)))
    q_tokens = tokenize(query)
    if not q_tokens:
        return list(range(len(documents)))
    bm25 = BM25Okapi(tokenized_corpus)
    scores = bm25.get_scores(q_tokens)
    return sorted(range(len(documents)), key=lambda i: -scores[i])


def rrf_fuse(rankings: list[list[int]], *, k: int = DEFAULT_RRF_K) -> list[int]:
    """Reciprocal-rank-fuse several rankings into a single ordered list.

    Each input list is a ranking of document indices (best first).
    Output is the union of all indices sorted descending by the sum of
    ``1 / (k + rank)`` across input rankings. Ties keep insertion order
    (Python dict preservation).
    """
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda i: -scores[i])
