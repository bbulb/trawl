"""Repeating-record detection.

Scans rendered HTML for groups of sibling elements that share a structural
signature (tag + sorted class list) and annotates each member with a text
sentinel so the downstream chunker can preserve record boundaries.

The sentinel is an ASCII-only ``[[TRAWL-REC|{gid}|{idx}]]`` token injected
as a text node at the start of each record (and a matching
``[[TRAWL-RECEND|{gid}]]`` after the last record). It survives the recall,
precision, and BS4 fallback extractors; the chunker strips sentinel lines
from emitted chunks so they never appear in user-facing output.

Heuristics are intentionally conservative:
- Record signatures must include an explicit class (bare ``<li>`` / ``<p>``
  siblings don't count) to avoid flagging ordinary prose lists.
- Groups require ≥3 consecutive matching siblings.
- Ancestors matching the shared noise regex (nav / sidebar / toc / menu /
  breadcrumb / site-header / site-footer) are skipped.
- Record text-length median must clear a floor so tab bars and
  pagination controls are ignored.
- Pages detecting more than ``MAX_GROUPS_PER_PAGE`` independent groups
  are treated as over-detection (sidebar widget soup) and annotation is
  skipped entirely for the page.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag

logger = logging.getLogger(__name__)


# ASCII-only sentinel. An earlier revision used U+2063 invisible separator
# as the delimiter but Trafilatura strips invisible whitespace characters
# before emitting markdown, collapsing "\u2063TRAWL-REC\u20630\u20631\u2063"
# down to "TRAWL-REC01" and losing the group/index distinction. ASCII
# pipes survive both the recall and precision paths as well as the BS4
# fallback. The marker is on its own line after markdown conversion and
# ``chunking._split_by_record_sentinels`` strips those lines before
# emission, so the tokens never appear in user-facing chunk text.
SENTINEL_PREFIX = "[[TRAWL-REC|"
SENTINEL_SUFFIX = "]]"
SENTINEL_END_PREFIX = "[[TRAWL-RECEND|"
SENTINEL_LINE_RE = re.compile(
    r"^\[\[TRAWL-REC\|(\d+)\|(\d+)\]\]$",
)
SENTINEL_END_LINE_RE = re.compile(
    r"^\[\[TRAWL-RECEND\|(\d+)\]\]$",
)

# Mirrors profiles/mapper.py NOISE_CLS_RE so record detection and the VLM
# mapper apply the same noise definition. Kept duplicated (vs a shared
# constant) because the mapper's copy is embedded in a JS string.
NOISE_CLS_RE = re.compile(
    r"\b(nav|sidebar|toc|table-of-contents|breadcrumb|menu|site-header|site-footer)\b",
    re.IGNORECASE,
)
NOISE_TAGS = {"nav", "aside", "footer", "header", "pre", "code"}
NOISE_ROLES = {
    "navigation",
    "complementary",
    "banner",
    "contentinfo",
    "tablist",
    "tab",
    "tabpanel",
}
# Docusaurus / Material / Bootstrap style tab class patterns. Tabs are
# semantically alternatives (only one is active at a time), so splitting
# them into separate records fragments the logical unit.
TAB_CLS_RE = re.compile(
    r"\btab(?:Item|panel|-item|-panel|-pane|_|content-pane)\b",
    re.IGNORECASE,
)

MIN_RECORDS_PER_GROUP = 3
MIN_RECORD_TEXT_LEN_MEDIAN = 20
# Ignore groups nested so deeply that they're almost certainly inside
# some widget — empirically anchors at depth > 20 are noise.
MAX_ANCESTOR_DEPTH = 20
# Pages with many independent record groups are almost always over-detecting
# (sidebar navigation with repeating region lists, footer link columns, etc).
# Empirically, genuine "the main content is a list" pages stay at 1–3 groups;
# 8 is a generous ceiling before we bail out.
MAX_GROUPS_PER_PAGE = 8


@dataclass
class RecordGroup:
    group_id: int
    parent_tag: str
    signature: str
    count: int
    median_text_len: int


def annotate_records(html: str) -> tuple[str, list[RecordGroup]]:
    """Inject sentinel lines around detected repeating sibling groups.

    Returns ``(annotated_html, groups)``. If nothing qualifies or parsing
    fails, returns ``(html, [])`` so callers can blindly substitute the
    result for the original HTML.
    """
    if not html:
        return html, []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("records: BS4 parse failed (%s); skipping annotation", e)
        return html, []

    groups: list[RecordGroup] = []
    next_id = 0
    for parent in soup.find_all(True):
        if not isinstance(parent, Tag):
            continue
        record_spans = _find_repeating_spans(parent)
        for signature, members in record_spans:
            if _in_noise_region(parent):
                continue
            # Members' own tag/class can carry noise signals independent of
            # the parent (e.g. a content <div> whose children are <nav>
            # siblings for section-link rails).
            if _in_noise_region(members[0]):
                continue
            if _any_member_is_tablike(members):
                continue
            text_lens = [_text_len(m) for m in members]
            if statistics.median(text_lens) < MIN_RECORD_TEXT_LEN_MEDIAN:
                continue
            gid = next_id
            next_id += 1
            for i, m in enumerate(members):
                _inject_sentinel(m, gid, i)
            _inject_end_sentinel(members[-1], gid)
            groups.append(
                RecordGroup(
                    group_id=gid,
                    parent_tag=parent.name,
                    signature=signature,
                    count=len(members),
                    median_text_len=int(statistics.median(text_lens)),
                )
            )

    if not groups:
        return html, []
    if len(groups) > MAX_GROUPS_PER_PAGE:
        # Over-detection: the DOM has many independent repeating
        # structures, which in practice means sidebar widgets / footer
        # link columns / regional selectors — not a content list. Bail
        # out rather than fragmenting the page into hundreds of chunks.
        logger.info(
            "records: %d groups exceed MAX_GROUPS_PER_PAGE=%d; skipping annotation",
            len(groups),
            MAX_GROUPS_PER_PAGE,
        )
        return html, []
    logger.info(
        "records: annotated %d group(s); total records=%d",
        len(groups),
        sum(g.count for g in groups),
    )
    return str(soup), groups


def _signature(el: Tag) -> str | None:
    """Structural signature for sibling comparison.

    Returns ``None`` when the element lacks any class (bare-tag repetition
    is treated as prose, not records).
    """
    classes = el.get("class")
    if not classes:
        return None
    return f"{el.name}|{' '.join(sorted(classes))}"


def _find_repeating_spans(parent: Tag) -> list[tuple[str, list[Tag]]]:
    """Scan `parent`'s direct Tag children for runs of identical signatures.

    Returns ``[(signature, [members, ...]), ...]`` for every contiguous run
    of length ≥ MIN_RECORDS_PER_GROUP. A parent can produce multiple runs
    of different signatures (e.g. a left rail with nav items then a main
    column with game cards).
    """
    spans: list[tuple[str, list[Tag]]] = []
    current_sig: str | None = None
    current_members: list[Tag] = []

    def flush() -> None:
        if current_sig is not None and len(current_members) >= MIN_RECORDS_PER_GROUP:
            spans.append((current_sig, list(current_members)))

    for child in parent.children:
        if not isinstance(child, Tag):
            continue
        sig = _signature(child)
        if sig is None or sig != current_sig:
            flush()
            current_sig = sig
            current_members = [child] if sig is not None else []
            continue
        current_members.append(child)
    flush()
    return spans


def _in_noise_region(el: Tag) -> bool:
    """Walk ancestors and decide whether the element is inside nav/sidebar/etc.

    Also rejects tabbed UIs (``role=tablist`` / ``tabpanel`` / ``.tabItem*``)
    because tab siblings are alternatives, not parallel records — splitting
    them fragments the logical unit.
    """
    node: Tag | None = el
    depth = 0
    while node is not None and depth < MAX_ANCESTOR_DEPTH:
        name = (node.name or "").lower()
        if name in NOISE_TAGS:
            return True
        role = (node.get("role") or "").lower()
        if role in NOISE_ROLES:
            return True
        classes = node.get("class") or []
        class_str = " ".join(classes) if classes else ""
        if class_str and NOISE_CLS_RE.search(class_str):
            return True
        if class_str and TAB_CLS_RE.search(class_str):
            return True
        el_id = node.get("id") or ""
        if el_id and NOISE_CLS_RE.search(el_id):
            return True
        node = node.parent if isinstance(node.parent, Tag) else None
        depth += 1
    return False


def _any_member_is_tablike(members: list[Tag]) -> bool:
    """Detect tab-alternative siblings by attribute.

    Exact-match on the direct members (not ancestors) catches cases where a
    tab group's siblings carry ``role=tabpanel`` / ``aria-hidden="true"`` /
    a tab class but sit inside a content wrapper that the ancestor walk
    would classify as main content.
    """
    for m in members:
        role = (m.get("role") or "").lower()
        if role in {"tabpanel", "tab"}:
            return True
        if (m.get("aria-hidden") or "").lower() == "true":
            return True
        if m.get("hidden") is not None:
            return True
        classes = m.get("class") or []
        if classes and TAB_CLS_RE.search(" ".join(classes)):
            return True
    return False


def _text_len(el: Tag) -> int:
    return len(el.get_text(" ", strip=True))


def _inject_sentinel(el: Tag, group_id: int, index: int) -> None:
    """Prepend a sentinel text node as the first child of `el`.

    Separate from the element's own text so the sentinel always starts its
    own line after markdown conversion (BS / Trafilatura insert a newline
    between block-level boundaries).
    """
    marker = f"\n{SENTINEL_PREFIX}{group_id}|{index}{SENTINEL_SUFFIX}\n"
    el.insert(0, marker)


def _inject_end_sentinel(last_member: Tag, group_id: int) -> None:
    """Append a group-end sentinel immediately after `last_member`.

    Without this marker the chunker can't tell where the final record ends
    from where post-group sibling content begins. Appending to the parent
    after the last member keeps the marker in document order.
    """
    marker = f"\n{SENTINEL_END_PREFIX}{group_id}{SENTINEL_SUFFIX}\n"
    last_member.insert_after(marker)
