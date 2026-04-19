"""Top-level entry point: fetch_relevant(url, query) → top-k chunks.

Workflow:
    1. track_visit(url) — increment the per-URL visit counter for the
       lazy suggest_profile hint.
    2. If a cached profile exists for this URL, take the profile fast
       path: render, apply the cached selector, verify via anchors,
       extract, chunk, and either return all chunks directly (small
       subtree) or retrieve top-k (large subtree with a query).
    3. Otherwise fall back to the existing pipeline: PDF via
       httpx+pymupdf, otherwise Playwright+Trafilatura → chunking →
       (optional HyDE) → bge-m3 cosine top-k.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from . import chunking, enrichment, extraction, fetch_cache, hyde, reranking, retrieval, telemetry
from .fetchers import github, passthrough, pdf, playwright, stackexchange, wikipedia, youtube

logger = logging.getLogger(__name__)


PROFILE_DIRECT_CHUNK_THRESHOLD = int(
    os.environ.get(
        "TRAWL_PROFILE_DIRECT_CHUNK_THRESHOLD",
        "20",
    )
)
VISIT_HINT_THRESHOLD = int(
    os.environ.get(
        "TRAWL_PROFILE_VISIT_HINT_THRESHOLD",
        "3",
    )
)
PROFILE_TRANSFER_MIN_RATIO = float(
    os.environ.get(
        "TRAWL_PROFILE_TRANSFER_MIN_RATIO",
        "0.3",
    )
)
PROFILE_TRANSFER_MAX_RATIO = float(
    os.environ.get(
        "TRAWL_PROFILE_TRANSFER_MAX_RATIO",
        "3.0",
    )
)


@dataclass
class PipelineResult:
    url: str
    query: str
    fetcher_used: str
    fetch_ms: int
    chunk_ms: int
    retrieval_ms: int
    total_ms: int
    page_chars: int
    n_chunks_total: int
    structured_path: bool
    hyde_used: bool
    hyde_text: str
    chunks: list[dict]
    error: str | None = None
    # New fields for the profile feature. All have safe defaults so
    # existing callers that construct PipelineResult by hand keep working.
    profile_used: bool = False
    profile_hash: str | None = None
    path: str = "full_page_retrieval"
    suggest_profile: bool = False
    suggest_profile_reason: str | None = None
    rerank_used: bool = False
    rerank_ms: int = 0
    content_type: str | None = None
    truncated: bool = False
    page_title: str = ""
    # C16 — compositional payload enrichment. All four fields populate
    # from `src/trawl/enrichment.py` (no LLM, no network) so agents can
    # chain follow-up fetches without re-parsing markdown. See
    # docs/superpowers/specs/2026-04-19-c16-compositional-payload-design.md.
    excerpts: list[dict] = field(default_factory=list)
    outbound_links: list[dict] = field(default_factory=list)
    page_entities: list[str] = field(default_factory=list)
    chain_hints: dict = field(default_factory=dict)
    # C8 — per-fetch cache hit indicator. Default False; set True when
    # the full pipeline path resumed from a cached fetch. See
    # docs/superpowers/specs/2026-04-20-c8-per-fetch-cache-design.md.
    cache_hit: bool = False

    @property
    def output_chars(self) -> int:
        return sum(c.get("char_count", 0) for c in self.chunks)

    @property
    def compression_ratio(self) -> float:
        return self.page_chars / max(self.output_chars, 1)


def _adaptive_k(n_chunks: int, override: int | None = None) -> int:
    """Pick top-k based on chunk pool size.

    Smaller pools need larger k because rank noise is proportional to
    pool size; larger pools can afford tighter k because the top of
    the distribution is more stable. Thresholds are empirically tuned
    from the parity matrix — see CLAUDE.md "Things NOT to change".
    """
    if override is not None:
        return override
    if n_chunks < 30:
        return min(8, max(5, n_chunks // 2 + 2))
    if n_chunks < 100:
        return 8
    if n_chunks < 200:
        return 10
    return 12


def _is_pdf_url(url: str) -> bool:
    lower = url.lower()
    return lower.endswith(".pdf") or "/pdf/" in lower


# (fetcher_module, native_fetcher_name) — each module exposes `matches(url) -> bool`
# and `fetch(url) -> FetchResult`. Checked in order; first match wins.
_API_FETCHERS = [
    (youtube, "youtube"),
    (github, "github"),
    (stackexchange, "stackexchange"),
    (wikipedia, "wikipedia"),
]


def _chunk_to_dict(chunk, *, score: float | None) -> dict:
    return {
        "text": chunk.text,
        "heading": chunk.heading,
        "char_count": chunk.char_count,
        "chunk_index": chunk.chunk_index,
        "score": score,
    }


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


def _try_passthrough(
    url: str,
    query: str | None,
    t_start: float,
) -> PipelineResult | None:
    """Attempt to satisfy `url` via the raw-passthrough path.

    Two ordered strategies:
    1. URL suffix hint (.json/.xml/.rss/.atom) — straight httpx GET via
       `passthrough.fetch`.
    2. HEAD pre-probe for suffix-less URLs — if the origin answers HEAD
       with a passthrough Content-Type, re-GET the body via
       `passthrough.fetch_raw_body` and skip Playwright entirely.

    Returns a PipelineResult when one of the strategies succeeds, else
    None so the caller continues to the normal HTML path.
    """
    if passthrough.matches(url):
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
        return None

    probed_ct = passthrough.probe(url)
    if probed_ct:
        pt = passthrough.fetch_raw_body(url)
        if pt.ok:
            return _build_passthrough_result(
                url,
                query,
                body=pt.raw_bytes,
                content_type=probed_ct,
                fetcher_name="passthrough-probed",
                t_start=t_start,
                fetch_ms=pt.elapsed_ms,
                truncated=pt.truncated,
            )
        logger.info(
            "passthrough HEAD probe hit but body fetch failed (%s); falling through",
            pt.error,
        )
    return None


def _error_result(
    url: str,
    query: str,
    error: str,
    t_start: float,
    **overrides,
) -> PipelineResult:
    """Build a PipelineResult for an error path.

    All counters default to zero; callers override the ones that carry
    meaningful partial state (fetch_ms, chunk_ms, hyde_used, etc.).
    """
    fields = {
        "url": url,
        "query": query,
        "fetcher_used": "",
        "fetch_ms": 0,
        "chunk_ms": 0,
        "retrieval_ms": 0,
        "total_ms": int((time.monotonic() - t_start) * 1000),
        "page_chars": 0,
        "n_chunks_total": 0,
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "chunks": [],
        "error": error,
        "path": "error",
    }
    fields.update(overrides)
    return PipelineResult(**fields)


def _contains_all_tokens(text: str, anchor: str) -> bool:
    """Mirror the mapper's token-level match rule in Python so the
    profile fast path can pick the right element among multiple matches
    of the same selector.
    """
    normalized = " ".join((text or "").split())
    tokens = (anchor or "").split()
    return bool(tokens) and all(t in normalized for t in tokens)


def _build_profile_result(
    url: str,
    query: str | None,
    *,
    profile,
    subtree_html: str,
    k: int | None,
    t_start: float,
    fetch_ms: int,
    use_rerank: bool = True,
) -> PipelineResult:
    """Given a subtree HTML snippet already extracted via a profile's
    selector, build the PipelineResult that the caller returns.

    Shared by the exact-match fast path and the host-transfer path.
    """
    t_chunk = time.monotonic()
    md = extraction.html_to_markdown(subtree_html)
    chunks = chunking.chunk_markdown(md)
    chunk_ms = int((time.monotonic() - t_chunk) * 1000)

    # Profile path operates on a subtree; the full-page <title> isn't
    # available here, so fall back to markdown H1 only.
    page_title = extraction.extract_title(html="", markdown=md)

    # Fields shared by every return from this function.
    base_kwargs = {
        "url": url,
        "query": query or "",
        "fetcher_used": "profile+trafilatura",
        "fetch_ms": fetch_ms,
        "chunk_ms": chunk_ms,
        "page_chars": len(md),
        "n_chunks_total": len(chunks),
        "structured_path": False,
        "hyde_used": False,
        "hyde_text": "",
        "profile_used": True,
        "profile_hash": profile.url_hash,
        "page_title": page_title,
    }

    rerank_ms = 0
    if len(chunks) <= PROFILE_DIRECT_CHUNK_THRESHOLD:
        path = "profile_direct"
        retrieved_dicts = [_chunk_to_dict(c, score=None) for c in chunks]
        retrieval_ms = 0
    elif query:
        path = "profile_retrieval"
        t_ret = time.monotonic()
        chosen_k = _adaptive_k(len(chunks), override=k)
        retrieve_k = min(chosen_k * 2, len(chunks)) if use_rerank else chosen_k
        retrieved = retrieval.retrieve(query, chunks, k=retrieve_k)
        retrieval_ms = int((time.monotonic() - t_ret) * 1000)
        if retrieved.error:
            return PipelineResult(
                **base_kwargs,
                retrieval_ms=retrieval_ms,
                total_ms=int((time.monotonic() - t_start) * 1000),
                chunks=[],
                error=retrieved.error,
                path=path,
            )
        if use_rerank and retrieved.scored:
            t_rr = time.monotonic()
            final_scored = reranking.rerank(
                query, retrieved.scored, k=chosen_k, page_title=page_title
            )
            rerank_ms = int((time.monotonic() - t_rr) * 1000)
        else:
            final_scored = retrieved.scored
        retrieved_dicts = [_chunk_to_dict(s.chunk, score=s.score) for s in final_scored]
        emitted_chunks = [s.chunk for s in final_scored]
    else:
        path = "profile_direct_large"
        retrieved_dicts = [_chunk_to_dict(c, score=None) for c in chunks]
        retrieval_ms = 0
        emitted_chunks = list(chunks)

    if path == "profile_direct":
        # Direct path returned all chunks above; mirror them for enrichment.
        emitted_chunks = list(chunks)

    return PipelineResult(
        **base_kwargs,
        retrieval_ms=retrieval_ms,
        total_ms=int((time.monotonic() - t_start) * 1000),
        chunks=retrieved_dicts,
        path=path,
        rerank_used=use_rerank and path == "profile_retrieval",
        rerank_ms=rerank_ms,
        excerpts=enrichment.extract_excerpts(emitted_chunks),
        outbound_links=enrichment.extract_outbound_links(emitted_chunks),
        page_entities=enrichment.extract_page_entities(
            page_title, [getattr(c, "heading_path", []) for c in emitted_chunks]
        ),
        chain_hints=enrichment.derive_chain_hints(url),
    )


def _profile_fast_path(
    url: str,
    query: str | None,
    *,
    profile,
    k: int | None,
    t_start: float,
    use_rerank: bool = True,
) -> PipelineResult | None:
    """Attempt the profile fast path. Returns a PipelineResult on
    success, or None if the profile has drifted (selector no longer
    matches) so the caller should fall back to the full pipeline.
    """
    t_fetch = time.monotonic()
    with playwright.render_session(
        url,
        profile_selector=profile.mapper.main_selector,
    ) as r:
        # A profile with zero verification anchors is untrustworthy: we
        # would vacuously accept the first selector match (all([]) is
        # True) and silently return wrong content on a drifted page.
        # Treat it as drift so the caller falls back to the full
        # pipeline.
        if not profile.mapper.verification_anchors:
            logger.info(
                "profile for %s has no verification anchors; treating as drift",
                url,
            )
            return None
        els = r.page.query_selector_all(profile.mapper.main_selector)
        chosen = None
        for el in els:
            text = el.inner_text()
            if all(_contains_all_tokens(text, a) for a in profile.mapper.verification_anchors):
                chosen = el
                break
        if chosen is None:
            logger.info(
                "profile drift for %s: selector=%r returned %d candidates, "
                "none passed verification anchors %r",
                url,
                profile.mapper.main_selector,
                len(els),
                profile.mapper.verification_anchors,
            )
            return None
        subtree_html = chosen.evaluate("el => el.outerHTML")
    fetch_ms = int((time.monotonic() - t_fetch) * 1000)

    return _build_profile_result(
        url,
        query,
        profile=profile,
        subtree_html=subtree_html,
        k=k,
        t_start=t_start,
        fetch_ms=fetch_ms,
        use_rerank=use_rerank,
    )


def _profile_transfer_path(
    url: str,
    query: str | None,
    *,
    k: int | None,
    t_start: float,
    use_rerank: bool = True,
) -> PipelineResult | None:
    """Try to match `url` against existing same-host profiles.

    On success: render, apply the matched profile's selector, verify
    the extracted subtree's char count is within PROFILE_TRANSFER_*_RATIO
    of the candidate's recorded subtree_char_count, extract + chunk +
    return via _build_profile_result, and persist a copy of the
    profile under `url`'s hash.

    Returns None if no candidate matches.
    """
    from trawl.profiles import (
        build_profile_copy,
        extract_fresh_anchors,
        list_host_profiles,
        save_profile,
    )

    host = urlsplit(url).netloc.lower()
    if not host:
        return None
    candidates = list_host_profiles(host)
    if not candidates:
        return None

    t_fetch = time.monotonic()
    with playwright.render_session(url) as r:
        for profile in candidates:
            if not profile.mapper.main_selector:
                continue
            scc = profile.mapper.subtree_char_count or 0
            if scc <= 0:
                continue
            try:
                els = r.page.query_selector_all(profile.mapper.main_selector)
            except Exception as e:
                logger.exception(
                    "transfer: selector %r failed on %s: %s",
                    profile.mapper.main_selector,
                    url,
                    e,
                )
                continue
            lo = scc * PROFILE_TRANSFER_MIN_RATIO
            hi = scc * PROFILE_TRANSFER_MAX_RATIO
            for el in els:
                try:
                    text = el.inner_text()
                except Exception as e:
                    logger.exception("transfer: inner_text raised: %s", e)
                    continue
                n = len(text)
                if not (lo <= n <= hi):
                    continue
                try:
                    subtree_html = el.evaluate("el => el.outerHTML")
                except Exception as e:
                    logger.exception("transfer: outerHTML extract raised: %s", e)
                    continue
                fresh = extract_fresh_anchors(text)
                copy = build_profile_copy(profile, url, fresh)
                if fresh:
                    # Only persist when the copy has content anchors the
                    # exact-match fast path can use for drift detection.
                    # An empty-anchor copy would be treated as drift on
                    # the next visit and fall back into this transfer
                    # path, causing an infinite re-render/re-save loop.
                    try:
                        save_profile(copy)
                    except Exception as e:
                        # Persistence failed but the in-memory copy is
                        # correct — log with traceback and still return
                        # the result. `copy.url_hash` is already
                        # url_hash(url), so profile_hash is correct.
                        logger.exception(
                            "transfer: save of copy for %s failed: %s",
                            url,
                            e,
                        )
                else:
                    logger.info(
                        "transfer: matched %s but fresh anchors empty; "
                        "not persisting copy (next visit will re-transfer)",
                        url,
                    )
                fetch_ms = int((time.monotonic() - t_fetch) * 1000)
                logger.info(
                    "transfer: matched %s under profile %s (selector=%r, %d chars)",
                    url,
                    profile.url_hash,
                    profile.mapper.main_selector,
                    n,
                )
                return _build_profile_result(
                    url,
                    query,
                    profile=copy,
                    subtree_html=subtree_html,
                    k=k,
                    t_start=t_start,
                    fetch_ms=fetch_ms,
                    use_rerank=use_rerank,
                )
    logger.info(
        "transfer: no host-local profile matched %s (scanned %d)",
        url,
        len(candidates),
    )
    return None


def fetch_relevant(
    url: str,
    query: str | None = None,
    *,
    k: int | None = None,
    use_hyde: bool = False,
    use_rerank: bool = True,
) -> PipelineResult:
    """Public entry point. See _fetch_relevant_impl for logic.

    Records one telemetry event per call when TRAWL_TELEMETRY=1.
    Telemetry failures never propagate.
    """
    result = _fetch_relevant_impl(
        url,
        query,
        k=k,
        use_hyde=use_hyde,
        use_rerank=use_rerank,
    )
    telemetry.record(result)
    return result


def _fetch_relevant_impl(
    url: str,
    query: str | None = None,
    *,
    k: int | None = None,
    use_hyde: bool = False,
    use_rerank: bool = True,
) -> PipelineResult:
    """Fetch `url`, return the main content.

    - If a cached profile exists for `url`, the profile fast path is
      taken and embedding is skipped for small subtrees. `query` is
      optional in this case — it's only used if the profiled subtree
      exceeds PROFILE_DIRECT_CHUNK_THRESHOLD and retrieval is needed.
    - If no profile exists (or the profile has drifted), falls back to
      the existing pipeline. `query` is required for this path; calling
      without a query returns an error PipelineResult with
      suggest_profile=True.

    Never raises — errors land in `result.error`.
    """
    t_start = time.monotonic()

    # Lazy import so a broken profiles subpackage cannot break trawl
    # startup. track_visit is cheap (one file read/write).
    try:
        from trawl.profiles import (
            get_visit_count,
            load_profile,
            track_visit,
        )

        track_visit(url)
        profile = load_profile(url)
    except Exception as e:
        logger.warning("profiles subsystem unavailable, falling back: %s", e)
        profile = None
        track_visit = None
        get_visit_count = None

    if profile is not None and profile.mapper.main_selector:
        try:
            result = _profile_fast_path(
                url,
                query,
                profile=profile,
                k=k,
                t_start=t_start,
                use_rerank=use_rerank,
            )
        except Exception as e:
            logger.warning("profile fast path raised, falling through: %s", e)
            result = None
        if result is not None:
            return result
        # Drift → fall through to transfer path.

    # Host-transfer path (exact miss or exact drift).
    try:
        transfer_result = _profile_transfer_path(
            url,
            query,
            k=k,
            t_start=t_start,
            use_rerank=use_rerank,
        )
    except Exception as e:
        logger.warning("profile transfer path raised, falling through: %s", e)
        transfer_result = None
    if transfer_result is not None:
        return transfer_result

    # Passthrough short-circuit: structured-data URLs (JSON, XML, RSS, Atom)
    # don't need a query — the raw bytes are the answer. Check before the
    # query=None guard so callers can omit query for these URLs. Covers
    # both URL-suffix and HEAD-probed (suffix-less) API endpoints.
    pt_result = _try_passthrough(url, query, t_start)
    if pt_result is not None:
        return pt_result

    # Full pipeline (existing behavior + query=None guard + suggest_profile).
    if not query:
        visit_count = get_visit_count(url) if get_visit_count else 0
        return _error_result(
            url,
            "",
            "no profile for URL; provide a query or call profile_page first",
            t_start,
            suggest_profile=visit_count >= VISIT_HINT_THRESHOLD,
            suggest_profile_reason=(
                f"visited {visit_count} times; profile_page({url!r}) would speed up future calls"
                if visit_count >= VISIT_HINT_THRESHOLD
                else None
            ),
        )

    result = _run_full_pipeline(
        url,
        query,
        k=k,
        use_hyde=use_hyde,
        use_rerank=use_rerank,
        t_start=t_start,
    )

    # Populate lazy suggest_profile hint on the fallback path.
    if get_visit_count is not None:
        visit_count = get_visit_count(url)
        if visit_count >= VISIT_HINT_THRESHOLD:
            result.suggest_profile = True
            result.suggest_profile_reason = (
                f"visited {visit_count} times; profile_page({url!r}) would speed up future calls"
            )
    return result


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


def _run_full_pipeline(
    url: str,
    query: str,
    *,
    k: int | None,
    use_hyde: bool,
    use_rerank: bool,
    t_start: float,
) -> PipelineResult:
    """Non-profile pipeline: fetch → extract → chunk → (HyDE) → retrieve → rerank."""
    # 1. Fetch → markdown (or short-circuit for PDF / passthrough).
    # C8: try the per-URL fetch cache first. Hit reuses pre-computed
    # markdown + page_title so Playwright/Trafilatura are skipped;
    # chunking / embedding / retrieval still run fresh because they're
    # query-dependent.
    cached = fetch_cache.get(url)
    cache_hit = cached is not None
    fetch_elapsed_ms = 0
    content_type: str | None = None
    fetched_html = ""

    if cache_hit:
        markdown = cached.markdown
        page_title = cached.page_title
        fetcher_name = cached.fetcher_used
        content_type = cached.content_type
    else:
        if _is_pdf_url(url):
            fetched = pdf.fetch(url)
            markdown = fetched.markdown
            fetcher_name = "pdf"
        else:
            pt_result = _try_passthrough(url, query, t_start)
            if pt_result is not None:
                return pt_result
            # C7: HEAD probe for suffix-less PDFs (download links, redirects).
            # Mirrors the passthrough.probe pattern: small HEAD lets us catch
            # `application/pdf` Content-Type before paying for a Playwright
            # render that would only return PDF viewer chrome. Probe failure
            # is silent — fall through to the existing HTML path.
            if pdf.probe(url):
                fetched = pdf.fetch(url)
                markdown = fetched.markdown
                fetcher_name = "pdf-probed"
            else:
                fetched, markdown, fetcher_name = _fetch_html(url)

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
                    content_type=ct or pt.content_type,
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

        if not fetched.ok or not markdown:
            return _error_result(
                url,
                query,
                fetched.error or "empty markdown after extraction",
                t_start,
                fetcher_used=fetcher_name,
                fetch_ms=fetched.elapsed_ms,
                page_chars=len(markdown),
            )

        fetch_elapsed_ms = fetched.elapsed_ms
        content_type = ct
        fetched_html = getattr(fetched, "html", "") or ""
        page_title = extraction.extract_title(html=fetched_html, markdown=markdown)
        # Populate the cache for next time. Only successful HTML/PDF
        # fetches land here (error/passthrough branches already returned).
        fetch_cache.put(
            fetch_cache.CachedFetch(
                url=url,
                markdown=markdown,
                page_title=page_title,
                fetcher_used=fetcher_name,
                content_type=content_type,
                cached_at=time.time(),
                fetch_elapsed_ms=fetch_elapsed_ms,
            )
        )

    # 2. Chunk
    t_chunk = time.monotonic()
    chunks = chunking.chunk_markdown(markdown)
    chunk_ms = int((time.monotonic() - t_chunk) * 1000)

    # 3. Optional HyDE
    extras: list[str] = []
    hyde_text = ""
    if use_hyde:
        hyde_text = hyde.expand(query)
        if hyde_text:
            extras = [hyde_text]

    # 4. Retrieve + rerank
    chosen_k = _adaptive_k(len(chunks), override=k)
    retrieve_k = min(chosen_k * 2, len(chunks)) if use_rerank else chosen_k
    retrieved = retrieval.retrieve(query, chunks, k=retrieve_k, extra_query_texts=extras)
    if retrieved.error:
        return _error_result(
            url,
            query,
            retrieved.error,
            t_start,
            fetcher_used=fetcher_name,
            fetch_ms=fetch_elapsed_ms,
            chunk_ms=chunk_ms,
            retrieval_ms=retrieved.elapsed_ms,
            page_chars=len(markdown),
            n_chunks_total=len(chunks),
            hyde_used=use_hyde,
            hyde_text=hyde_text,
        )

    rerank_ms = 0
    if use_rerank and retrieved.scored:
        t_rerank = time.monotonic()
        final_scored = reranking.rerank(query, retrieved.scored, k=chosen_k, page_title=page_title)
        rerank_ms = int((time.monotonic() - t_rerank) * 1000)
    else:
        final_scored = retrieved.scored

    emitted_chunks = [s.chunk for s in final_scored]
    return PipelineResult(
        url=url,
        query=query,
        fetcher_used=fetcher_name,
        fetch_ms=fetch_elapsed_ms,
        chunk_ms=chunk_ms,
        retrieval_ms=retrieved.elapsed_ms,
        total_ms=int((time.monotonic() - t_start) * 1000),
        page_chars=len(markdown),
        n_chunks_total=len(chunks),
        structured_path=False,
        hyde_used=use_hyde,
        hyde_text=hyde_text,
        chunks=[_chunk_to_dict(c, score=s.score) for c, s in zip(emitted_chunks, final_scored, strict=True)],
        excerpts=enrichment.extract_excerpts(emitted_chunks),
        outbound_links=enrichment.extract_outbound_links(emitted_chunks),
        page_entities=enrichment.extract_page_entities(
            page_title, [getattr(c, "heading_path", []) for c in emitted_chunks]
        ),
        chain_hints=enrichment.derive_chain_hints(url),
        path="full_page_retrieval",
        rerank_used=use_rerank,
        rerank_ms=rerank_ms,
        page_title=page_title,
        content_type=content_type,
        cache_hit=cache_hit,
    )


def to_dict(result: PipelineResult) -> dict:
    d = asdict(result)
    d["output_chars"] = result.output_chars
    d["compression_ratio"] = round(result.compression_ratio, 1)
    return d
