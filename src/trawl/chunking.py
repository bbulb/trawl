"""Section-preserving markdown chunker.

Design notes:
- We split on markdown headings (#, ##, ###) and keep each section intact.
- A section is split further only if it exceeds `max_chars` (default 1500).
- Tables (lines starting with `|`) inside a section are NEVER split — the whole
  table is kept with its preceding heading.
- Each chunk carries its heading path (parent headings) as metadata so the
  embedding step can use it for retrieval and the caller can show context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")

# Minimum plain-text length (after stripping markdown markup) for a chunk to
# be worth embedding. Anything shorter is almost always nav/boilerplate.
MIN_PLAIN_CHARS = 20


@dataclass
class Chunk:
    text: str  # original markdown, shown to user / passed to LLM
    heading_path: list[str] = field(default_factory=list)
    char_count: int = 0
    chunk_index: int = 0
    embed_text: str = ""  # markdown-stripped version used for embedding

    @property
    def heading(self) -> str:
        return " > ".join(self.heading_path) if self.heading_path else ""


_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_WHITESPACE_RE = re.compile(r"\s+")


def plain_text(md: str) -> str:
    """Strip markdown markup so we embed the semantic content, not the noise.

    - Images removed entirely (no semantic content beyond the alt text, which
      is usually empty on wiki/news).
    - Links collapsed to their link text (`[이순신](https://...)` → `이순신`).
    - Standalone URLs removed.
    - Whitespace collapsed.
    """
    md = _IMAGE_RE.sub("", md)
    md = _LINK_RE.sub(r"\1", md)
    md = _URL_RE.sub("", md)
    md = _WHITESPACE_RE.sub(" ", md)
    return md.strip()


def chunk_markdown(md: str, *, max_chars: int | None = None) -> list[Chunk]:
    """Split markdown into heading-bound, table-preserving chunks.

    When `max_chars` is None, the default is chosen adaptively based on page
    size:
      - small pages (<20k chars): 900 — keeps sections + code blocks together
        because there's less top-k competition on a small page
      - larger pages: 450 — smaller chunks keep specific facts concentrated
        and the top-k more diverse on pages with many sections

    Tables are never split either way. The retrieval layer's per-input
    truncation is the final safety net.
    """
    if max_chars is None:
        max_chars = 900 if len(md) < 20_000 else 450
    if not md.strip():
        return []

    sections = _split_by_headings(md)
    chunks: list[Chunk] = []
    idx = 0
    for heading_path, body in sections:
        for piece in _split_section(body, max_chars=max_chars):
            text = piece.strip()
            if not text:
                continue
            embed = plain_text(text)
            if len(embed) < MIN_PLAIN_CHARS:
                # Pure nav / image / link-only chunk — drop it.
                continue
            chunks.append(
                Chunk(
                    text=text,
                    heading_path=heading_path,
                    char_count=len(text),
                    chunk_index=idx,
                    embed_text=embed,
                ),
            )
            idx += 1
    return chunks


def _split_by_headings(md: str) -> list[tuple[list[str], str]]:
    """Walk markdown line-by-line, return [(heading_path, body), ...]."""
    lines = md.split("\n")
    sections: list[tuple[list[str], str]] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    buf: list[str] = []

    def flush() -> None:
        if buf:
            path = [t for _, t in stack]
            sections.append((path, "\n".join(buf)))

    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buf.append(line)
    flush()
    if not sections:
        # No headings at all; treat the whole thing as one anonymous section.
        sections = [([], md)]
    return sections


def _split_section(body: str, *, max_chars: int) -> list[str]:
    """Split a section into pieces ≤ max_chars without breaking tables.

    The primary split boundary is line breaks. For pages that arrive as one
    giant single-line blob (PDFs converted to markdown commonly look like
    this), we fall back to sentence / word / character splitting via
    `_split_long_line` before returning.
    """
    if len(body) <= max_chars:
        return [body]

    lines = body.split("\n")
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    in_table = False

    def push() -> None:
        nonlocal current, current_len
        if current:
            pieces.append("\n".join(current).strip())
            current = []
            current_len = 0

    for line in lines:
        if TABLE_LINE_RE.match(line):
            in_table = True
        elif in_table and not TABLE_LINE_RE.match(line):
            in_table = False

        # Line itself is oversized: break it into smaller bits first.
        if len(line) > max_chars and not in_table:
            push()
            pieces.extend(_split_long_line(line, max_chars=max_chars))
            continue

        line_len = len(line) + 1  # +1 for the newline
        if current_len + line_len > max_chars and not in_table and current:
            push()
        current.append(line)
        current_len += line_len
    push()
    return pieces


# Sentence terminators across the languages we actually see (en, ko, ja, zh).
# Whitespace after the punctuation is OPTIONAL because PDF conversions often
# strip them, which is exactly when we need this fallback the most.
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？])\s*")


def _split_long_line(line: str, *, max_chars: int) -> list[str]:
    """Fallback splitter for lines that exceed max_chars.

    Tries sentence → word → character boundaries in that order. Used for
    pages like PDF-converted markdown that arrive as a single giant line.
    """
    if len(line) <= max_chars:
        return [line]

    # Sentence-level first. Keep multiple sentences per chunk up to max_chars.
    sentences = [s for s in _SENTENCE_RE.split(line) if s]
    if len(sentences) > 1:
        return _pack(sentences, max_chars=max_chars, separator=" ")

    # Single-sentence line: fall back to words.
    words = line.split(" ")
    if len(words) > 1:
        return _pack(words, max_chars=max_chars, separator=" ")

    # Single-word line (or no spaces): hard char split.
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]


def _pack(units: list[str], *, max_chars: int, separator: str) -> list[str]:
    """Greedily group `units` into chunks of ≤ max_chars, re-joining with `separator`.

    If any single unit is itself larger than max_chars, it's recursively
    passed back through `_split_long_line` so we never emit an oversized
    piece.
    """
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    sep_len = len(separator)

    for unit in units:
        if len(unit) > max_chars:
            if current:
                pieces.append(separator.join(current).strip())
                current = []
                current_len = 0
            pieces.extend(_split_long_line(unit, max_chars=max_chars))
            continue

        added = len(unit) + (sep_len if current else 0)
        if current_len + added > max_chars and current:
            pieces.append(separator.join(current).strip())
            current = [unit]
            current_len = len(unit)
        else:
            current.append(unit)
            current_len += added

    if current:
        pieces.append(separator.join(current).strip())
    return pieces
