#!/usr/bin/env python3
"""Stack Exchange extraction diagnostic (one-shot, gitignored usage).

Drives the two failing `code_heavy_query` agent patterns through each
pipeline phase and emits a per-keyword substring-presence report so the
drop-off point (fetch vs chunk vs retrieve) is unambiguous. Matches the
Case A / B / C classification in
``notes/next-session-2026-04-27-followups.md`` §1.

Run::

    mamba run -n trawl python benchmarks/stackexchange_extraction_diag.py

No writes to disk; pipe stdout into a file manually if you want to keep
the report. Exits 0 even on partial failures so the table is always
printed.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from trawl import bm25, chunking, retrieval
from trawl.fetchers import stackexchange as se
from trawl.pipeline import _adaptive_k, fetch_relevant


CASES: list[dict[str, Any]] = [
    {
        "id": "serverfault_378860",
        "url": "https://serverfault.com/questions/378860/nginx-reverse-proxy-cookies",
        "query": "preserve original Host header through nginx reverse proxy",
        "keywords": ["proxy_set_header", "Host", "X-Forwarded-For", "$http_host"],
    },
    {
        "id": "stackoverflow_44488350",
        "url": "https://stackoverflow.com/questions/44488350/python-asyncio-subprocess-with-timeout",
        "query": "asyncio subprocess with timeout",
        "keywords": ["asyncio", "subprocess", "wait_for", "timeout"],
    },
]


def _fetch_api_withbody(url: str) -> tuple[str, dict[str, Any]]:
    """Phase 0 — raw HTML body from the default ``withbody`` filter.

    Mirrors exactly the request the current stackexchange fetcher
    makes so we compare like-for-like.
    """
    parsed = se._parse_se_url(url)
    if not parsed:
        return "", {"reason": "not SE URL"}
    site, qid = parsed
    params = {"site": site, "filter": "withbody", "order": "desc", "sort": "votes"}
    try:
        with httpx.Client(timeout=15.0) as client:
            q = client.get(f"{se._SE_API_BASE}/questions/{qid}", params=params)
            q.raise_for_status()
            q_json = q.json()
            a = client.get(f"{se._SE_API_BASE}/questions/{qid}/answers", params=params)
            a.raise_for_status()
            a_json = a.json()
    except Exception as e:
        return "", {"reason": f"api error: {e!r}"}
    items = q_json.get("items", []) + a_json.get("items", [])
    html = "\n\n===ITEM===\n\n".join(it.get("body", "") or "" for it in items)
    return html, {
        "n_question_items": len(q_json.get("items", [])),
        "n_answer_items": len(a_json.get("items", [])),
        "quota_remaining": q_json.get("quota_remaining"),
        "has_body_markdown_field": any("body_markdown" in it for it in items),
    }


def _fetch_api_body_markdown(url: str) -> tuple[str | None, str]:
    """Phase 0b — probe a custom filter that exposes ``body_markdown``.

    ``/filters/create`` is anonymous-allowed and lets us include
    ``question.body_markdown`` + ``answer.body_markdown`` without an
    API key. Returns (markdown_or_None, status_message).
    """
    parsed = se._parse_se_url(url)
    if not parsed:
        return None, "not SE URL"
    site, qid = parsed
    try:
        with httpx.Client(timeout=15.0) as client:
            f = client.get(
                f"{se._SE_API_BASE}/filters/create",
                params={
                    "include": "question.body_markdown;answer.body_markdown",
                    "base": "default",
                    "unsafe": "false",
                },
            )
            f.raise_for_status()
            filt_items = f.json().get("items", [])
            if not filt_items:
                return None, "filters/create returned no items"
            filt = filt_items[0]["filter"]
            q = client.get(
                f"{se._SE_API_BASE}/questions/{qid}",
                params={"site": site, "filter": filt},
            )
            q.raise_for_status()
            q_json = q.json()
            a = client.get(
                f"{se._SE_API_BASE}/questions/{qid}/answers",
                params={"site": site, "filter": filt, "order": "desc", "sort": "votes"},
            )
            a.raise_for_status()
            a_json = a.json()
    except Exception as e:
        return None, f"api error: {e!r}"
    items = q_json.get("items", []) + a_json.get("items", [])
    parts = [it.get("body_markdown") or "" for it in items if it.get("body_markdown")]
    if not parts:
        return None, "body_markdown field empty on all items"
    return "\n\n===ITEM===\n\n".join(parts), "ok"


def _presence_row(text: str, keywords: list[str]) -> str:
    return "  ".join(f"{kw}={'YES' if kw in text else 'no '}" for kw in keywords)


def _chunks_with(chunks: list, keyword: str) -> list[int]:
    return [c.chunk_index for c in chunks if keyword in c.text]


def run(case: dict[str, Any]) -> None:
    url = case["url"]
    query = case["query"]
    keywords = case["keywords"]
    print("=" * 92)
    print(f"CASE: {case['id']}")
    print(f"URL:    {url}")
    print(f"QUERY:  {query}")
    print(f"KEYWORDS: {keywords}")
    print(f"BM25 query tokens: {bm25.tokenize(query)}")
    print()

    # Phase 0 — raw API withbody HTML.
    t = time.monotonic()
    api_html, info = _fetch_api_withbody(url)
    print(
        f"[p0]  API withbody HTML        {int((time.monotonic()-t)*1000)}ms  "
        f"info={info}"
    )
    print(f"      {_presence_row(api_html, keywords)}")
    print(f"      char_count={len(api_html)}")

    # Phase 0b — body_markdown via custom filter (probe).
    t = time.monotonic()
    api_md, status = _fetch_api_body_markdown(url)
    elapsed = int((time.monotonic() - t) * 1000)
    if api_md is None:
        print(f"[p0b] API body_markdown         {elapsed}ms  status={status!r}")
    else:
        print(f"[p0b] API body_markdown         {elapsed}ms  status=ok")
        print(f"      {_presence_row(api_md, keywords)}")
        print(f"      char_count={len(api_md)}")
        sample = api_md[:400].replace("\n", "\\n")
        print(f"      sample[:400]: {sample!r}")

    # Phase 1 — stackexchange.fetch() markdown (current production path).
    t = time.monotonic()
    fr = se.fetch(url)
    print(
        f"[p1]  se.fetch() markdown       {fr.elapsed_ms}ms  "
        f"fetcher={fr.fetcher}  error={fr.error!r}"
    )
    print(f"      {_presence_row(fr.markdown, keywords)}")
    print(f"      char_count={len(fr.markdown)}")

    # Phase 2 — chunks.
    chunks = chunking.chunk_markdown(fr.markdown)
    print(f"[p2]  chunking.chunk_markdown  n_chunks={len(chunks)}")
    for kw in keywords:
        idxs = _chunks_with(chunks, kw)
        print(f"      {kw!r} in chunk_idxs {idxs}")

    # Phase 2b — BM25-only ranking on chunk.embed_text (what the C6
    # hybrid path and the chunk_budget prefilter see).
    if chunks:
        docs = [c.embed_text for c in chunks]
        bm25_order = bm25.bm25_rank(query, docs)
        top5 = bm25_order[:5]
        print(f"[p2b] BM25 rank (embed_text)   top5_chunk_idx={top5}")
        for kw in keywords:
            hit = next(
                (r for r, i in enumerate(bm25_order) if kw in chunks[i].text),
                None,
            )
            print(f"      {kw!r} first BM25 rank = {hit}")

    # Phase 3 — dense retrieval (current default path, no rerank).
    retrieved_text = ""
    if chunks:
        chosen_k = _adaptive_k(len(chunks))
        retrieve_k = min(chosen_k * 2, len(chunks))
        t = time.monotonic()
        retrieved = retrieval.retrieve(query, chunks, k=retrieve_k)
        ret_ms = int((time.monotonic() - t) * 1000)
        if retrieved.error:
            print(f"[p3]  dense retrieve ERROR     {ret_ms}ms  error={retrieved.error}")
        else:
            retrieved_text = "\n\n".join(s.chunk.text for s in retrieved.scored)
            print(
                f"[p3]  dense retrieve top-{len(retrieved.scored)}      {ret_ms}ms  "
                f"chosen_k={chosen_k}  retrieve_k={retrieve_k}"
            )
            print(f"      {_presence_row(retrieved_text, keywords)}")
            for kw in keywords:
                rk = next(
                    (r for r, s in enumerate(retrieved.scored) if kw in s.chunk.text),
                    None,
                )
                print(f"      {kw!r} first dense rank = {rk}")
    else:
        print("[p3]  dense retrieve skipped (no chunks)")

    # Phase 4 — full fetch_relevant (what agent_patterns assertion sees).
    # Use a per-run cache-skip to avoid interference with earlier runs.
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
        f"[p4]  fetch_relevant final      {tot_ms}ms  "
        f"n_chunks_total={result.n_chunks_total}  n_chunks_embedded={result.n_chunks_embedded}  "
        f"path={result.path}  fetcher_used={result.fetcher_used}"
    )
    print(f"      {_presence_row(final_text, keywords)}")
    print(f"      returned_chunks={len(result.chunks)}")
    if result.error:
        print(f"      error={result.error!r}")
    print()


def main() -> None:
    for case in CASES:
        try:
            run(case)
        except Exception as e:
            print(f"CASE {case['id']} crashed: {type(e).__name__}: {e}")
    print("=" * 92)
    print("DONE")


if __name__ == "__main__":
    main()
