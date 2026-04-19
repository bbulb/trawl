"""Compositional payload enrichment (C16).

Pure functions that derive lightweight metadata from the existing
extraction output so agents can chain LLM-free. Never calls an LLM,
never opens a network connection, never mutates inputs.

Four enrichers, each populating one PipelineResult field:

    extract_excerpts(scored_chunks)            -> list of {chunk_idx, summary_120c}
    extract_outbound_links(chunks, *, cap)     -> list of {url, anchor_text, in_chunk_idx}
    extract_page_entities(title, headings)     -> list of str
    derive_chain_hints(url)                    -> dict

The pipeline calls these after retrieve+rerank so excerpts reflect the
top-k that was actually returned to the agent. Profile fast-path calls
them too with whichever chunk set was emitted.

Design notes
------------
- Costs: <50 ms total on the parity matrix (regex + dict).
- Caps protect against pathological pages (1000-link sidebars, 100-section
  Wikipedia articles): hard-capped at module constants below.
- Backward-compat: PipelineResult fields default to empty list/dict so
  callers that ignore enrichment see no behavior change.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Caps -------------------------------------------------------------

EXCERPTS_TOP_N = 3
EXCERPT_MAX_CHARS = 120
OUTBOUND_LINKS_MAX = 50
OUTBOUND_LINKS_MAX_BYTES = 10_240  # 10 KB hard ceiling for the field
PAGE_ENTITIES_MAX = 20

# Markdown link [text](url) — non-greedy text, scheme://host... url.
# Image links ![alt](url) are excluded by the negative lookbehind.
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+)\)")

# Naive sentence splitter — first '. ' / '! ' / '? ' or end of string.
# Korean: '. ' works. Chinese full stop '。' / Japanese '。' also handled.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?。！？])\s+")

# Strip markdown markup before excerpt cap so '120 chars' is meaningful.
_MD_HEADING_RE = re.compile(r"^#+\s+", re.MULTILINE)
_MD_CODE_FENCE_RE = re.compile(r"^```.*?$", re.MULTILINE)
_MD_BOLD_ITALIC_RE = re.compile(r"\*\*?([^*]+)\*\*?")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")

# Capitalised English noun-phrase (2+ tokens) or contiguous Korean
# syllables (2+ chars) — used to surface page-level "entities" without
# real NER.
_EN_NP_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9'-]+(?:\s+[A-Z][A-Za-z0-9'-]+)+)\b")
_KO_NP_RE = re.compile(r"[\uAC00-\uD7AF]{2,}")


# Excerpts ---------------------------------------------------------


def extract_excerpts(scored_chunks: list, *, top_n: int = EXCERPTS_TOP_N,
                     max_chars: int = EXCERPT_MAX_CHARS) -> list[dict]:
    """Top-N chunks → [{chunk_idx, summary_120c}].

    `scored_chunks` is whatever the pipeline produces just before
    serialisation: a list with a `.chunk` (Chunk dataclass) attribute
    OR a list of Chunk dataclasses directly. We accept both so the
    caller doesn't need to flatten.

    The summary is the chunk's first sentence (or first line if no
    sentence terminator exists in the first `max_chars*2` window),
    char-capped. Markdown markup is stripped so the cap is meaningful.
    """
    out: list[dict] = []
    for sc in scored_chunks[:top_n]:
        chunk = getattr(sc, "chunk", sc)
        text = getattr(chunk, "text", "") or ""
        idx = getattr(chunk, "chunk_index", None)
        if idx is None:
            # Defensive — chunk_index always set by chunking.py but a
            # caller might pass dict-shaped chunks in tests.
            if isinstance(chunk, dict):
                idx = chunk.get("chunk_index", 0)
                text = chunk.get("text", text) or text
            else:
                idx = 0
        summary = _first_sentence(text, max_chars)
        if not summary:
            continue
        out.append({"chunk_idx": int(idx), "summary_120c": summary})
    return out


def _first_sentence(md: str, cap: int) -> str:
    """Strip markup, return the first sentence or first line, char-capped."""
    if not md:
        return ""
    cleaned = _MD_CODE_FENCE_RE.sub("", md)
    cleaned = _MD_HEADING_RE.sub("", cleaned)
    cleaned = _MD_BOLD_ITALIC_RE.sub(r"\1", cleaned)
    cleaned = _MD_INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = cleaned.strip()
    if not cleaned:
        return ""
    # Take the first non-empty line.
    first_line = cleaned.split("\n", 1)[0].strip()
    if not first_line:
        return ""
    # Sentence-split that line; the first piece is the excerpt.
    parts = _SENTENCE_END_RE.split(first_line, maxsplit=1)
    candidate = parts[0].strip()
    if len(candidate) > cap:
        # Hard cap with ellipsis so callers can detect truncation.
        return candidate[: cap - 1].rstrip() + "…"
    return candidate


# Outbound links ---------------------------------------------------


def extract_outbound_links(chunks: list, *, cap: int = OUTBOUND_LINKS_MAX,
                           bytes_cap: int = OUTBOUND_LINKS_MAX_BYTES) -> list[dict]:
    """Walk top-k chunks for markdown `[text](url)` patterns.

    Returns `[{url, anchor_text, in_chunk_idx}]` capped at the lower of
    `cap` entries or `bytes_cap` total JSON size. Same accept-both
    chunk shape as `extract_excerpts`.

    Image references (`![alt](url)`) are excluded.
    """
    out: list[dict] = []
    seen: set[str] = set()
    accumulated_bytes = 0
    for sc in chunks:
        chunk = getattr(sc, "chunk", sc)
        text = getattr(chunk, "text", "") or ""
        idx = getattr(chunk, "chunk_index", None)
        if idx is None and isinstance(chunk, dict):
            idx = chunk.get("chunk_index", 0)
            text = chunk.get("text", text) or text
        if idx is None:
            idx = 0
        for m in _MD_LINK_RE.finditer(text):
            anchor = m.group(1).strip()
            url = m.group(2).strip()
            key = (url, anchor)
            if key in seen:
                continue
            seen.add(key)
            entry = {"url": url, "anchor_text": anchor, "in_chunk_idx": int(idx)}
            # Conservative byte estimate: JSON-encoded size approximation.
            est = len(url) + len(anchor) + 32
            if accumulated_bytes + est > bytes_cap:
                return out
            out.append(entry)
            accumulated_bytes += est
            if len(out) >= cap:
                return out
    return out


# Page entities ----------------------------------------------------


def extract_page_entities(page_title: str, heading_paths: list[list[str]],
                          *, cap: int = PAGE_ENTITIES_MAX) -> list[str]:
    """Surface noun-phrase candidates from title + chunk heading_paths.

    Two extractors:
      - English: 2+ contiguous Capitalised tokens
      - Korean: 2+ contiguous Hangul syllables (regex-based)

    Returns dedup-preserving list capped at `cap`. Empty list if no
    matches (Wikipedia / news / docs all hit it; pure code pages may
    not).
    """
    sources: list[str] = []
    if page_title:
        sources.append(page_title)
    for path in heading_paths:
        for h in path:
            if h and h not in sources:
                sources.append(h)

    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        for m in _EN_NP_RE.finditer(src):
            t = m.group(0).strip()
            if t not in seen:
                seen.add(t)
                out.append(t)
                if len(out) >= cap:
                    return out
        for m in _KO_NP_RE.finditer(src):
            t = m.group(0).strip()
            if t not in seen:
                seen.add(t)
                out.append(t)
                if len(out) >= cap:
                    return out
    return out


# Chain hints ------------------------------------------------------

_HOST_HINTS: dict[str, dict] = {
    # arXiv: agents commonly want "show me the PDF", "find related arxiv
    # papers", "look up the first author".
    "arxiv.org": {
        "recommended_followup_filter": "site:arxiv.org",
        "pdf_template": "https://arxiv.org/pdf/{id}",
        "abs_template": "https://arxiv.org/abs/{id}",
    },
    "github.com": {
        "recommended_followup_filter": "site:github.com",
        "raw_template": "https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}",
    },
    "en.wikipedia.org": {
        "recommended_followup_filter": "site:en.wikipedia.org",
        "search_template": "https://en.wikipedia.org/w/index.php?search={query}",
    },
    "ko.wikipedia.org": {
        "recommended_followup_filter": "site:ko.wikipedia.org",
        "search_template": "https://ko.wikipedia.org/w/index.php?search={query}",
    },
    "ja.wikipedia.org": {
        "recommended_followup_filter": "site:ja.wikipedia.org",
    },
    "youtube.com": {
        "recommended_followup_filter": "site:youtube.com",
    },
    "www.youtube.com": {
        "recommended_followup_filter": "site:youtube.com",
    },
    "stackoverflow.com": {
        "recommended_followup_filter": "site:stackoverflow.com",
        "tag_template": "https://stackoverflow.com/questions/tagged/{tag}",
    },
}


def derive_chain_hints(url: str) -> dict:
    """Per-host follow-up hints. Empty dict for unknown hosts.

    Catalogue starts with the top-traffic categories agents revisit
    most: arxiv, github, wikipedia (en/ko/ja), youtube, stackoverflow.
    Add hosts to `_HOST_HINTS` as patterns emerge.
    """
    if not url:
        return {}
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return {}
    return dict(_HOST_HINTS.get(host, {}))
