"""Indirect prompt-injection defense for fetched web content.

trawl returns web page text verbatim to MCP agents, so a page can try
to smuggle instructions into the agent's context. This module is the
deterministic, model-free defense described in
``docs/superpowers/specs/2026-06-05-injection-defense-design.md``:

1. Unicode tag characters (U+E0000-U+E007F) are stripped — they render
   invisibly but reach a parser, and have no legitimate use in fetched
   prose.
2. CSS-hidden text and instruction-like phrasing are *annotated*, never
   deleted: the chunk keeps its text and gains a ``suspicious_hidden``
   / ``suspicious_injection`` flag so the consuming agent can decide.

trawl cannot solve prompt injection (that needs agent-side defenses).
The goal is to raise the cost of smuggling and to hand the agent a
clear untrusted-content signal. Disable the whole scan with
``TRAWL_INJECTION_SCAN=0``.
"""

from __future__ import annotations

import os
import re

from bs4 import BeautifulSoup

# Unicode "tag" block — invisible to renderers, visible to parsers. No
# legitimate role in fetched prose, so removal is deterministic.
_TAG_CHARS_RE = re.compile(r"[\U000E0000-\U000E007F]")

# Recall-biased instruction patterns. Precision is the consuming
# agent's problem; a false positive only adds a flag, a false negative
# lets an instruction through. Each requires an instruction-related
# object so benign prose like "ignore previous failures" does not fire.
_INSTRUCTION_RES = [
    re.compile(
        r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+"
        r"(instruction|prompt|message|context|rule|direction)",
        re.IGNORECASE,
    ),
    re.compile(
        r"disregard\s+(all\s+)?(previous|prior|above|the)\s+"
        r"(instruction|prompt|message|context|rule|direction)",
        re.IGNORECASE,
    ),
    re.compile(r"forget\s+(everything|all|your)\s+(above|previous|prior)", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an|the)\b", re.IGNORECASE),
    re.compile(r"your\s+new\s+(instruction|task|role|goal|prompt)", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(a|an|if)\b", re.IGNORECASE),
    # Markdown image/link exfiltration: ![](http...) or [..](http...)
    # whose URL carries query-like data — a common silent-exfil shape.
    re.compile(r"!\[[^\]]*\]\(https?://[^)]*[?&=][^)]*\)", re.IGNORECASE),
]

# Inline-style markers for visually hidden content.
_HIDDEN_STYLE_RES = [
    re.compile(r"display\s*:\s*none", re.IGNORECASE),
    re.compile(r"visibility\s*:\s*hidden", re.IGNORECASE),
    re.compile(r"opacity\s*:\s*0(\b|\.0|%)", re.IGNORECASE),
    re.compile(r"font-size\s*:\s*0(\b|px|pt|em)", re.IGNORECASE),
    re.compile(r"(left|top)\s*:\s*-\d{4,}px", re.IGNORECASE),
]

_MIN_HIDDEN_LEN = 12  # ignore tiny hidden snippets (icons, single chars)


def is_enabled() -> bool:
    """Injection scan is on unless ``TRAWL_INJECTION_SCAN=0``."""
    return os.environ.get("TRAWL_INJECTION_SCAN", "1") != "0"


def strip_tag_chars(text: str) -> tuple[str, int]:
    """Remove Unicode tag characters; return (cleaned_text, n_removed)."""
    if not text:
        return text, 0
    n = len(_TAG_CHARS_RE.findall(text))
    return (_TAG_CHARS_RE.sub("", text), n) if n else (text, 0)


def looks_like_injection(text: str) -> bool:
    """True if `text` matches any instruction-injection pattern."""
    return any(r.search(text) for r in _INSTRUCTION_RES)


def hidden_text_segments(html: str) -> list[str]:
    """Return *instruction-like* text inside hidden nodes.

    Covers CSS-hidden (inline display/visibility/opacity/font-size/
    off-screen), and aria-hidden nodes. Benign hidden content is
    pervasive on real pages (sr-only text, collapsed menus/accordions,
    lazy tabs), so a hidden node is only reported when its text both
    clears ``_MIN_HIDDEN_LEN`` and matches an instruction pattern —
    otherwise default-on scanning would flood ordinary pages with
    meaningless warnings.
    """
    if not html:
        return []
    return _scan_html(BeautifulSoup(html, "html.parser"))[1]


def _scan_html(soup: BeautifulSoup) -> tuple[int, list[str]]:
    """Single-parse scan: (tag-char count, instruction-like hidden segs)."""
    n_tag = len(_TAG_CHARS_RE.findall(soup.get_text()))
    hidden: list[str] = []
    for el in soup.find_all(style=True):
        style = el.get("style", "")
        if any(r.search(style) for r in _HIDDEN_STYLE_RES):
            txt = el.get_text(" ", strip=True)
            if len(txt) >= _MIN_HIDDEN_LEN and looks_like_injection(txt):
                hidden.append(txt)
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        txt = el.get_text(" ", strip=True)
        if len(txt) >= _MIN_HIDDEN_LEN and looks_like_injection(txt):
            hidden.append(txt)
    return n_tag, hidden


def annotate_chunks(
    chunk_dicts: list[dict],
    *,
    html: str | None = None,
) -> tuple[list[dict], list[str]]:
    """Strip tag chars and flag suspicious chunks in place.

    Mutates each chunk dict: tag chars are removed from ``text``; a
    chunk that matches an instruction pattern gains
    ``suspicious_injection: true``; a chunk whose text overlaps a
    CSS-hidden segment from `html` gains ``suspicious_hidden: true``.
    Flag keys are added only when true, so the clean path is unchanged.

    A hidden segment found in `html` is reported in `warnings` even
    when extraction already dropped it from every chunk — the agent
    should know the page *contained* hidden directives regardless of
    whether they survived into the output.

    Returns (chunk_dicts, warnings).
    """
    # Single HTML parse for both the tag-char count and hidden-segment
    # scan. Tag chars usually do not survive extraction (Trafilatura
    # drops them), so the decoded raw HTML is the smuggle signal — the
    # agent should be told the page carried them even once they're gone.
    if html:
        n_tag, hidden = _scan_html(BeautifulSoup(html, "html.parser"))
    else:
        n_tag, hidden = 0, []

    n_injection = 0
    n_hidden_chunks = 0
    for c in chunk_dicts:
        text = c.get("text") or ""
        cleaned, removed = strip_tag_chars(text)
        if removed:
            # Defense in depth: strip from any chunk where extraction let
            # them through. Counted separately from the HTML-level scan.
            c["text"] = cleaned
            text = cleaned
            if not html:
                n_tag += removed
        if looks_like_injection(text):
            c["suspicious_injection"] = True
            n_injection += 1
        if hidden and any(_overlaps(text, seg) for seg in hidden):
            c["suspicious_hidden"] = True
            n_hidden_chunks += 1

    warnings: list[str] = []
    if hidden:
        warnings.append(f"injection-scan: page contains {len(hidden)} hidden text segment(s)")
    if n_tag:
        warnings.append(f"injection-scan: stripped {n_tag} unicode tag char(s)")
    if n_injection:
        warnings.append(f"injection-scan: {n_injection} chunk(s) flagged suspicious_injection")
    if n_hidden_chunks:
        warnings.append(f"injection-scan: {n_hidden_chunks} chunk(s) flagged suspicious_hidden")
    return chunk_dicts, warnings


def _overlaps(chunk_text: str, hidden_segment: str) -> bool:
    """True if a hidden segment is substantially present in chunk text.

    Extraction may reflow whitespace, so compare on collapsed
    whitespace and accept a containment match in either direction.
    """
    a = " ".join(chunk_text.split())
    b = " ".join(hidden_segment.split())
    if len(b) < _MIN_HIDDEN_LEN:
        return False
    return b in a or a in b
