#!/usr/bin/env python3
"""BGE-M3 reader-comparison gap diagnostic (one-shot).

Traces why two cases fail `answer_grounding_hit` under BGE-M3 default
settings (`TRAWL_HYBRID_RETRIEVAL=0`, `TRAWL_CHUNK_BUDGET=100`):

    github_fastapi_readme        missing_facts = ["endpoint_example"]
                                 (@app / uvicorn / import FastAPI)
                                 n_chunks_total = 71, no prefilter
    wiki_large_language_model    missing_facts = ["transformer"]
                                 (transformer / Transformer)
                                 n_chunks_total = 285, prefiltered to 100

For each case dumps per-keyword chunk indices + BM25 rank + dense
cosine rank + whether the chunk survives the prefilter + top-k.

Reads-only — no fixture writes. Run::

    mamba run -n trawl python benchmarks/bge_m3_reader_gap_diag.py
"""

from __future__ import annotations

import os
import time
from typing import Any

from trawl import bm25, chunking, extraction, retrieval
from trawl.fetchers import github as gh
from trawl.fetchers import playwright as pw
from trawl.fetchers import wikipedia as wp
from trawl.pipeline import _adaptive_k, _read_chunk_budget, fetch_relevant


def _route_fetch(url: str):
    """Mirror pipeline's _API_FETCHERS ordering for the two domains here."""
    if gh.matches(url):
        return gh.fetch(url), "github"
    if wp.matches(url):
        return wp.fetch(url), "wikipedia"
    return pw.fetch(url), "playwright"


def _to_markdown(fr, query: str) -> str:
    """If the fetcher already produced markdown use it; otherwise extract."""
    if fr.markdown:
        return fr.markdown
    if fr.html:
        return extraction.html_to_markdown(fr.html, query=query)
    return ""


CASES: list[dict[str, Any]] = [
    {
        "id": "github_fastapi_readme",
        "url": "https://github.com/fastapi/fastapi",
        "query": "how to install FastAPI and create a basic endpoint",
        "missing_fact_id": "endpoint_example",
        "missing_keywords": ["@app", "uvicorn", "import FastAPI"],
    },
    {
        "id": "wiki_large_language_model",
        "url": "https://en.wikipedia.org/wiki/Large_language_model",
        "query": "how are large language models trained and what are key techniques",
        "missing_fact_id": "transformer",
        "missing_keywords": ["transformer", "Transformer"],
    },
]


def _chunk_text_for_embed(chunk) -> str:
    """Reproduce retrieval.retrieve() chunk_text composition (no contextual)."""
    base = chunk.embed_text or chunk.text
    if chunk.heading:
        return chunk.heading + "\n\n" + base
    return base


def _chunks_containing_any(chunks, keywords: list[str]) -> list[int]:
    hits: list[int] = []
    for i, c in enumerate(chunks):
        text = (c.heading or "") + "\n" + c.text
        if any(kw in text for kw in keywords):
            hits.append(i)
    return hits


def run(case: dict[str, Any]) -> None:
    url = case["url"]
    query = case["query"]
    kws = case["missing_keywords"]
    print("=" * 92)
    print(f"CASE: {case['id']}")
    print(f"URL:    {url}")
    print(f"QUERY:  {query}")
    print(f"MISSING FACT: {case['missing_fact_id']!r} → any_of={kws}")
    print(f"BM25 query tokens: {bm25.tokenize(query)}")
    print()

    # Phase 1 — fetch via standard pipeline routing.
    t = time.monotonic()
    fr, route = _route_fetch(url)
    md = _to_markdown(fr, query)
    print(
        f"[p1]  fetcher                   {fr.elapsed_ms}ms  "
        f"route={route}  fetcher_field={fr.fetcher}  error={fr.error!r}  "
        f"md_chars={len(md)}  raw_html={len(fr.html or '')}"
    )
    for kw in kws:
        cnt = md.count(kw)
        print(f"      markdown count {kw!r}: {cnt}")
    print()

    # Phase 2 — chunks.
    chunks = chunking.chunk_markdown(md, source_url=url)
    n_total = len(chunks)
    print(f"[p2]  chunks                   n_total={n_total}")
    kw_chunk_idxs = _chunks_containing_any(chunks, kws)
    print(f"      chunk indices containing any({kws}): {kw_chunk_idxs}")
    for idx in kw_chunk_idxs[:5]:
        c = chunks[idx]
        head = (c.heading or "(no heading)")[:80]
        body_snip = c.text[:120].replace("\n", " ")
        print(f"      idx={idx}  chars={len(c.text)}  heading={head!r}  body={body_snip!r}")
    if not kw_chunk_idxs:
        print("      !! NO chunk contains the missing keyword. Source / chunker issue.")
        return

    # Phase 3 — BM25 prefilter behavior.
    chunk_texts = [_chunk_text_for_embed(c) for c in chunks]
    bm25_ranked = bm25.bm25_rank(query, chunk_texts)
    budget = _read_chunk_budget()
    print(
        f"[p3]  BM25 prefilter            budget={budget}  "
        f"prefilter_active={budget > 0 and n_total > budget}"
    )
    for kw in kws:
        # Where does the FIRST chunk that contains this keyword sit in BM25?
        first_idx = None
        for c_i, c in enumerate(chunks):
            text = (c.heading or "") + "\n" + c.text
            if kw in text:
                first_idx = c_i
                break
        if first_idx is None:
            continue
        bm25_rank_of_first = bm25_ranked.index(first_idx) if first_idx in bm25_ranked else -1
        survives = (budget == 0) or (n_total <= budget) or (bm25_rank_of_first < budget)
        print(
            f"      first chunk with {kw!r} → idx={first_idx}  "
            f"BM25 rank={bm25_rank_of_first}  survives_prefilter={survives}"
        )

    # Apply prefilter explicitly (mirrors retrieval.retrieve() logic).
    if budget > 0 and n_total > budget:
        kept = sorted(bm25_ranked[:budget])
        survived_chunks = [chunks[i] for i in kept]
    else:
        survived_chunks = chunks

    n_embedded = len(survived_chunks)
    print(f"      n_chunks_embedded={n_embedded}  dropped={n_total - n_embedded}")
    print()

    # Phase 4 — dense cosine retrieval over survived chunks.
    if not survived_chunks:
        print("[p4]  dense retrieve skipped (no survived chunks)")
        return
    chosen_k = _adaptive_k(n_total)
    use_rerank = True  # match pipeline default
    retrieve_k = min(chosen_k * 2, n_embedded) if use_rerank else chosen_k
    t = time.monotonic()
    retrieved = retrieval.retrieve(
        query,
        survived_chunks,
        k=retrieve_k,
        chunk_budget=0,  # already applied above
    )
    ret_ms = int((time.monotonic() - t) * 1000)
    print(
        f"[p4]  dense retrieve            {ret_ms}ms  "
        f"chosen_k={chosen_k}  retrieve_k={retrieve_k}  error={retrieved.error}"
    )
    if retrieved.error:
        return
    scored = retrieved.scored
    print(f"      scored count = {len(scored)}")
    # First rank that contains each keyword in the dense scored output.
    for kw in kws:
        rank = next(
            (
                r
                for r, s in enumerate(scored)
                if kw in (s.chunk.heading or "") + "\n" + s.chunk.text
            ),
            None,
        )
        if rank is None:
            print(
                f"      {kw!r} NOT in dense top-{len(scored)} (post-prefilter pool of {n_embedded})"
            )
        else:
            print(
                f"      {kw!r} first dense rank = {rank}  (chunk_idx={scored[rank].chunk.chunk_index})"
            )
    print()

    # Phase 5 — full fetch_relevant for sanity (reproduces production result).
    prev_ttl = os.environ.get("TRAWL_FETCH_CACHE_TTL")
    os.environ["TRAWL_FETCH_CACHE_TTL"] = "0"
    try:
        t = time.monotonic()
        result = fetch_relevant(url, query)
        tot_ms = int((time.monotonic() - t) * 1000)
    finally:
        if prev_ttl is None:
            os.environ.pop("TRAWL_FETCH_CACHE_TTL", None)
        else:
            os.environ["TRAWL_FETCH_CACHE_TTL"] = prev_ttl
    final_text = "\n\n".join(c["text"] for c in result.chunks)
    print(
        f"[p5]  fetch_relevant            {tot_ms}ms  "
        f"n_chunks_total={result.n_chunks_total}  n_chunks_embedded={result.n_chunks_embedded}  "
        f"returned={len(result.chunks)}  rerank_used={result.rerank_used}"
    )
    for kw in kws:
        present = kw in final_text
        print(f"      final returned has {kw!r}: {present}")
    if result.error:
        print(f"      error={result.error!r}")
    print()


def main() -> None:
    for case in CASES:
        try:
            run(case)
        except Exception as e:
            print(f"CASE {case['id']} crashed: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
    print("=" * 92)
    print("DONE")


if __name__ == "__main__":
    main()
