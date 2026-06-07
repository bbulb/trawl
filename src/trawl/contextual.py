"""Deterministic contextual text for retrieval inputs.

The returned strings are ranking-only inputs. They must not replace
``Chunk.text`` or any public payload text.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .chunking import Chunk

DEFAULT_MAX_PREFIX_CHARS = 320
PREFIX_VERSION = "deterministic-v1"
AUTO_MIN_CHUNKS = 16
AUTO_TINY_PAGE_MAX_CHUNKS = 2
_IDENTIFIER_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*[.:/][A-Za-z0-9_./:-]+|[A-Za-z_][A-Za-z0-9_]*\(\))"
)
_CODE_HINT_RE = re.compile(
    r"\b(api|class|cli|def|function|handler|method|module|parameter|signature|"
    r"traceback|import|async|await|exception|error|config|endpoint|sdk)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContextualText:
    text: str
    prefix_chars: int


@dataclass(frozen=True)
class ContextualTextBatch:
    texts: list[str]
    prefix_chars_total: int
    prefix_chars_avg: float


def mode() -> str:
    """Return contextual retrieval mode: off, on, or auto."""
    raw = os.environ.get("TRAWL_CONTEXTUAL_RETRIEVAL", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return "on"
    if raw == "auto":
        return "auto"
    return "off"


def is_enabled() -> bool:
    """Return True only when contextual retrieval is forced on."""
    return mode() == "on"


def max_prefix_chars() -> int:
    """Return the configured prefix cap, falling back to the default."""
    raw = os.environ.get("TRAWL_CONTEXT_PREFIX_MAX_CHARS", str(DEFAULT_MAX_PREFIX_CHARS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_PREFIX_CHARS
    return max(0, value)


def prefix_version() -> str:
    """Return the contextual prefix version used for cache keying."""
    return os.environ.get("TRAWL_CONTEXT_PREFIX_VERSION", PREFIX_VERSION).strip() or PREFIX_VERSION


def should_use_contextual(
    *,
    query: str,
    chunks: list[Chunk],
    page_title: str = "",
) -> bool:
    """Return whether contextual retrieval should be used for this request."""
    current = mode()
    if current == "off":
        return False
    if max_prefix_chars() <= 0:
        return False
    if current == "on":
        return True
    if not chunks:
        return False
    if any(c.record_group_id is not None for c in chunks):
        return True
    if len(chunks) <= AUTO_TINY_PAGE_MAX_CHUNKS:
        return False
    if _looks_identifier_query(query):
        return True
    if len(chunks) >= AUTO_MIN_CHUNKS:
        return True
    return False


def _looks_identifier_query(query: str) -> bool:
    if _IDENTIFIER_RE.search(query):
        return True
    if "`" in query:
        return True
    return bool(_CODE_HINT_RE.search(query) and re.search(r"[A-Za-z_][A-Za-z0-9_]*", query))


def build_contextual_text(
    chunk: Chunk,
    *,
    page_title: str,
    previous_heading: str,
    next_heading: str,
    total_chunks: int,
) -> ContextualText:
    """Build one ranking-only contextual string for ``chunk``."""
    body = chunk.embed_text or chunk.text
    prefix_limit = max_prefix_chars()
    if prefix_limit <= 0:
        return ContextualText(text=body, prefix_chars=0)

    lines: list[str] = []
    title = page_title.strip()
    if title:
        lines.append(f"Title: {title}")
    if chunk.heading:
        lines.append(f"Section: {chunk.heading}")
    if total_chunks > 0:
        lines.append(f"Position: chunk {chunk.chunk_index + 1} of {total_chunks}")
    if chunk.record_group_id is not None and chunk.record_index is not None:
        lines.append(
            f"Record: item {chunk.record_index + 1} in repeated group {chunk.record_group_id}"
        )

    nearby = [h for h in (previous_heading, next_heading) if h]
    if nearby:
        lines.append(f"Nearby sections: {' | '.join(nearby)}")

    prefix = "\n".join(lines).strip()
    if len(prefix) > prefix_limit:
        prefix = prefix[:prefix_limit].rstrip()
    if not prefix:
        return ContextualText(text=body, prefix_chars=0)
    return ContextualText(text=f"{prefix}\n\n{body}", prefix_chars=len(prefix))


def build_contextual_texts(chunks: list[Chunk], *, page_title: str) -> ContextualTextBatch:
    """Build contextual retrieval inputs aligned with ``chunks``."""
    texts: list[str] = []
    prefix_total = 0
    total_chunks = len(chunks)
    headings = [c.heading for c in chunks]

    for index, chunk in enumerate(chunks):
        previous_heading = _nearest_heading(headings, index, step=-1)
        next_heading = _nearest_heading(headings, index, step=1)
        item = build_contextual_text(
            chunk,
            page_title=page_title,
            previous_heading=previous_heading,
            next_heading=next_heading,
            total_chunks=total_chunks,
        )
        texts.append(item.text)
        prefix_total += item.prefix_chars

    avg = prefix_total / total_chunks if total_chunks else 0.0
    return ContextualTextBatch(
        texts=texts,
        prefix_chars_total=prefix_total,
        prefix_chars_avg=avg,
    )


def _nearest_heading(headings: list[str], index: int, *, step: int) -> str:
    i = index + step
    while 0 <= i < len(headings):
        if headings[i]:
            return headings[i]
        i += step
    return ""
