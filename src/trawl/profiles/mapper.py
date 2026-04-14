"""Anchor text → DOM element → ancestor walk → LCA → CSS selector.

Algorithm:
1. For each anchor string from the VLM, ask Playwright for up to N
   candidate elements via get_by_text(...).
2. For each candidate, walk up its ancestors until the subtree's
   innerText length crosses min_chars OR the element is a recognised
   semantic container (article/main/section/table/[role=main]).
3. Compute the LCA across all candidate containers (one per anchor).
4. Generate a CSS selector for the LCA: prefer #id, then a stable
   class chain, then a tag-path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.sync_api import Page

SEMANTIC_TAGS = {"ARTICLE", "MAIN", "SECTION", "TABLE", "UL", "OL"}
DEFAULT_MIN_CHARS = 300
DEFAULT_MAX_CANDIDATES_PER_ANCHOR = 5


@dataclass
class MappedAnchor:
    anchor: str
    found_count: int  # how many DOM matches the anchor produced
    container_path: list[str]  # tag path of the chosen container, root-first
    container_chars: int


@dataclass
class MapResult:
    selector: str | None  # None if mapping failed (collapsed to body)
    lca_tag: str
    lca_path: list[str]
    subtree_html: str  # outerHTML of the LCA element
    subtree_chars: int
    anchors_found: list[MappedAnchor]
    anchors_missed: list[str]
    notes: list[str] = field(default_factory=list)
    # Anchors whose containers were dropped by the median-depth outlier
    # filter before LCA computation. These matched something in the DOM
    # but are typically nav/header text, so they are NOT inside the final
    # subtree and must NOT be used as verification anchors downstream.
    outlier_anchors: list[str] = field(default_factory=list)


# JS function executed in the browser. Takes a list of anchor strings and
# returns the mapping data so we don't have to make N round-trips.
_ANCESTOR_LCA_JS = r"""
(args) => {
  const { anchors, minChars, maxCandidates, semanticTags } = args;

  // Helper: normalize whitespace for substring search.
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  // Noise region detection: check if an element sits inside a nav,
  // sidebar, table of contents, breadcrumb, or similar non-content
  // region. Used to deprioritise (not discard) candidate matches so
  // the LCA doesn't collapse to <body> due to sidebar TOC entries
  // that duplicate heading text.
  const NOISE_TAGS = new Set(["NAV", "ASIDE", "FOOTER", "HEADER"]);
  const NOISE_ROLES = new Set(["navigation", "complementary", "banner", "contentinfo"]);
  const NOISE_CLS_RE = /\b(nav|sidebar|toc|table-of-contents|breadcrumb|menu|site-header|site-footer)\b/i;

  const isInNoiseRegion = (el) => {
    let n = el;
    while (n && n !== document.body) {
      if (NOISE_TAGS.has(n.tagName)) return true;
      const role = (n.getAttribute("role") || "").toLowerCase();
      if (NOISE_ROLES.has(role)) return true;
      const cls = (n.className && typeof n.className === "string") ? n.className : "";
      if (NOISE_CLS_RE.test(cls)) return true;
      const id = n.id || "";
      if (NOISE_CLS_RE.test(id)) return true;
      n = n.parentElement;
    }
    return false;
  };

  // Find candidate elements by token-level containment on innerText.
  // An anchor like "KIA 6" is split into tokens ["KIA", "6"]; we find
  // the smallest element whose normalised innerText contains ALL tokens.
  //
  // Why: textContent concatenates children with no separator, so a team
  // name and adjacent score produce "KIA승네일스코어6" — no substring
  // match. innerText respects CSS block boundaries and inserts newlines
  // (normalised to spaces here), but composite anchors still don't line
  // up as contiguous substrings ("KIA 승 네일 스코어 6" doesn't contain
  // "KIA 6"). Token-level matching handles this correctly while still
  // matching simple single-phrase anchors (all tokens are in that one
  // element's text).
  const findCandidatesForAnchor = (anchor) => {
    const needle = norm(anchor);
    if (!needle) return [];
    const tokens = needle.split(/\s+/).filter(t => t.length >= 1);
    if (tokens.length === 0) return [];

    const containsAllTokens = (text) => tokens.every(t => text.includes(t));

    const matches = [];
    const walker = document.createTreeWalker(
      document.body,
      NodeFilter.SHOW_ELEMENT,
      {
        acceptNode: (el) => {
          const text = norm(el.innerText || "");
          if (!containsAllTokens(text)) {
            return NodeFilter.FILTER_REJECT; // prune subtree
          }
          for (const child of el.children) {
            const ct = norm(child.innerText || "");
            if (containsAllTokens(ct)) {
              return NodeFilter.FILTER_SKIP; // visit children, smaller match exists
            }
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      }
    );
    while (walker.nextNode()) {
      matches.push(walker.currentNode);
      if (matches.length >= maxCandidates) break;
    }
    return matches;
  };

  // Walk ancestors until innerText >= minChars or we hit a semantic tag.
  const expandToContainer = (el) => {
    let current = el;
    while (current && current !== document.body) {
      const text = norm(current.innerText || "");
      if (text.length >= minChars) return current;
      if (semanticTags.includes(current.tagName)) return current;
      current = current.parentElement;
    }
    return current; // body fallback
  };

  // Path of an element from <html> down, as ["HTML","BODY","DIV","UL"...].
  const tagPath = (el) => {
    const out = [];
    let n = el;
    while (n) {
      out.unshift(n.tagName);
      n = n.parentElement;
    }
    return out;
  };

  // LCA of two elements as an actual element, not a tag path. Walk
  // ancestors of A into a Set, walk ancestors of B until one hits the
  // set. Tag-path intersection is wrong on pages where multiple distinct
  // subtrees share the same tag-path prefix (Wikipedia's sitenotice and
  // main article both sit under html>body>div>div>div).
  const lcaOfTwo = (a, b) => {
    if (!a || !b) return null;
    const ancestorsA = new Set();
    let n = a;
    while (n) { ancestorsA.add(n); n = n.parentElement; }
    let m = b;
    while (m) {
      if (ancestorsA.has(m)) return m;
      m = m.parentElement;
    }
    return null;
  };

  // Run mapping for each anchor, collecting one container per anchor.
  // We pick the LCA of all anchor candidates for that anchor (to handle
  // duplicate matches), then later compute LCA across anchors.
  const containers = []; // { anchor, candidates: Element[], chosen: Element, foundCount, inNoise }
  const missed = [];
  for (const a of anchors) {
    const candidates = findCandidatesForAnchor(a);
    if (candidates.length === 0) {
      missed.push(a);
      continue;
    }
    // Prefer candidates NOT in a noise region (nav, sidebar, TOC,
    // breadcrumb). If all candidates are in noise, use the first one
    // anyway — graceful degradation over silent discard.
    const nonNoise = candidates.filter(c => !isInNoiseRegion(c));
    const best = nonNoise.length > 0 ? nonNoise[0] : candidates[0];
    const inNoise = nonNoise.length === 0;
    const chosen = expandToContainer(best);
    containers.push({ anchor: a, foundCount: candidates.length, chosen, inNoise });
  }

  if (containers.length === 0) {
    return { ok: false, reason: "all anchors missed", missed, found: [] };
  }

  // Outlier drop: if we have >= 3 containers, drop any whose tagPath
  // depth is < median - 2. This removes nav/header matches that would
  // otherwise drag the LCA up to <body>. Only applied if at least 2
  // containers survive the filter.
  const outlierDrops = [];
  let usedContainers = containers;
  if (containers.length >= 3) {
    const depths = containers.map(c => tagPath(c.chosen).length);
    const sortedDepths = [...depths].sort((a, b) => a - b);
    const median = sortedDepths[Math.floor(sortedDepths.length / 2)];
    const threshold = median - 2;
    const kept = containers.filter(c => tagPath(c.chosen).length >= threshold);
    if (kept.length >= 2 && kept.length < containers.length) {
      for (const c of containers) {
        if (tagPath(c.chosen).length < threshold) {
          outlierDrops.push({
            anchor: c.anchor,
            depth: tagPath(c.chosen).length,
            median,
          });
        }
      }
      usedContainers = kept;
    }
  }

  // LCA across the surviving containers — reduce via lcaOfTwo.
  let lcaEl = usedContainers[0].chosen;
  for (let i = 1; i < usedContainers.length; i++) {
    lcaEl = lcaOfTwo(lcaEl, usedContainers[i].chosen);
    if (!lcaEl) break;
  }
  if (!lcaEl) {
    return { ok: false, reason: "LCA computation returned null", missed, found: [] };
  }
  const lcaTagPath = tagPath(lcaEl);

  // Generate a CSS selector for lcaEl.
  // Priority: #id > tag.class.class (stable-looking) > nth-of-type tag path.
  const isLikelyStableClass = (cls) =>
    cls && /^[a-zA-Z][a-zA-Z0-9_-]{2,}$/.test(cls) && !/^[a-zA-Z]+-?\d{3,}$/.test(cls);

  // CSS Modules / styled-components / vanilla-extract all append a build-time
  // hash to class names, e.g. `ScheduleAllType_match_list__zuZyQ`. Those
  // change on every deploy, so a selector hard-coding the hash is brittle.
  // Detect the suffix and return a { base, isHashed } pair so selectorFor
  // can emit `[class*="base"]` instead of `.base__hash`.
  const hashSuffixRe = /^(.*?)([_-]{1,2})([A-Za-z0-9]{5,})$/;
  const splitHashSuffix = (cls) => {
    const m = cls.match(hashSuffixRe);
    if (!m) return { base: cls, isHashed: false };
    const base = m[1];
    const tail = m[3];
    // Heuristic for "tail looks like a hash":
    //   (a) tail has both upper and lower case letters, or
    //   (b) tail is >= 7 chars.
    // Also require the base to be non-empty and meaningful (>= 3 chars) so
    // we don't mis-strip genuinely short suffix words like `_wrap`, `_item`.
    const hasBothCases = /[a-z]/.test(tail) && /[A-Z]/.test(tail);
    if (base.length < 3) return { base: cls, isHashed: false };
    if (hasBothCases || tail.length >= 7) return { base, isHashed: true };
    return { base: cls, isHashed: false };
  };

  const selectorFor = (el) => {
    if (el.id && /^[a-zA-Z][a-zA-Z0-9_-]+$/.test(el.id)) return `#${el.id}`;
    const tag = el.tagName.toLowerCase();
    const classes = (el.className && typeof el.className === "string"
      ? el.className.split(/\s+/).filter(isLikelyStableClass)
      : []
    );
    if (classes.length > 0) {
      // For each class, either emit .foo (stable) or [class*="base"]
      // (build-time hash suffix stripped). Multiple selectors compose.
      const parts = classes.map(cls => {
        const { base, isHashed } = splitHashSuffix(cls);
        return isHashed ? `[class*="${base}"]` : `.${cls}`;
      });
      return tag + parts.join("");
    }
    // Fall back to tag path with nth-of-type from html down (3 levels max).
    const parts = [];
    let cur = el;
    let depth = 0;
    while (cur && cur !== document.documentElement && depth < 4) {
      const parent = cur.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter(c => c.tagName === cur.tagName);
      const idx = siblings.indexOf(cur) + 1;
      parts.unshift(`${cur.tagName.toLowerCase()}:nth-of-type(${idx})`);
      cur = parent;
      depth++;
    }
    return parts.join(" > ");
  };

  return {
    ok: true,
    selector: selectorFor(lcaEl),
    lcaTag: lcaEl.tagName,
    lcaPath: lcaTagPath,
    subtreeHtml: lcaEl.outerHTML,
    subtreeChars: norm(lcaEl.innerText || "").length,
    found: containers.map(c => ({
      anchor: c.anchor,
      foundCount: c.foundCount,
      inNoise: c.inNoise || false,
      containerPath: tagPath(c.chosen),
      containerChars: norm(c.chosen.innerText || "").length,
    })),
    missed,
    outlierDrops,
  };
};
"""


def find_main_subtree(
    page: Page,
    anchors: list[str],
    *,
    min_chars: int = DEFAULT_MIN_CHARS,
    max_candidates_per_anchor: int = DEFAULT_MAX_CANDIDATES_PER_ANCHOR,
) -> MapResult:
    """Run the JS mapper inside `page` and convert the result."""
    raw = page.evaluate(
        _ANCESTOR_LCA_JS,
        {
            "anchors": anchors,
            "minChars": min_chars,
            "maxCandidates": max_candidates_per_anchor,
            "semanticTags": sorted(SEMANTIC_TAGS),
        },
    )

    if not raw.get("ok"):
        return MapResult(
            selector=None,
            lca_tag="",
            lca_path=[],
            subtree_html="",
            subtree_chars=0,
            anchors_found=[],
            anchors_missed=raw.get("missed", []),
            notes=[f"mapping failed: {raw.get('reason')}"],
        )

    lca_tag = raw["lcaTag"]
    notes: list[str] = []
    outlier_drops_raw = raw.get("outlierDrops", []) or []
    outlier_anchor_names = [od["anchor"] for od in outlier_drops_raw]
    for od in outlier_drops_raw:
        notes.append(
            f"dropped outlier container: anchor={od['anchor']!r} "
            f"depth={od['depth']} (median={od['median']})"
        )
    if lca_tag in {"BODY", "HTML"}:
        notes.append(f"LCA collapsed to {lca_tag} — main_selector treated as failure")
        return MapResult(
            selector=None,
            lca_tag=lca_tag,
            lca_path=raw["lcaPath"],
            subtree_html=raw["subtreeHtml"],
            subtree_chars=raw["subtreeChars"],
            anchors_found=[
                MappedAnchor(
                    anchor=f["anchor"],
                    found_count=f["foundCount"],
                    container_path=f["containerPath"],
                    container_chars=f["containerChars"],
                )
                for f in raw["found"]
            ],
            anchors_missed=raw.get("missed", []),
            notes=notes,
            outlier_anchors=outlier_anchor_names,
        )

    return MapResult(
        selector=raw["selector"],
        lca_tag=lca_tag,
        lca_path=raw["lcaPath"],
        subtree_html=raw["subtreeHtml"],
        subtree_chars=raw["subtreeChars"],
        anchors_found=[
            MappedAnchor(
                anchor=f["anchor"],
                found_count=f["foundCount"],
                container_path=f["containerPath"],
                container_chars=f["containerChars"],
            )
            for f in raw["found"]
        ],
        anchors_missed=raw.get("missed", []),
        notes=notes,
        outlier_anchors=outlier_anchor_names,
    )
