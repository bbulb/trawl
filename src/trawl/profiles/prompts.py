"""VLM prompts for query-agnostic profile generation.

The profile layer asks the VLM "what are the meaningful content regions
of this page?" with no query context. Validated across 5 initial test
URLs and later a 36-case evaluation; works cleanly on most page types.
The one known failure mode is schedule/table-heavy pages where the
VLM's description format doesn't map to clean DOM element boundaries,
which the fetch_relevant fallback path covers.

Prompt iteration history:
  v1 (2026-04-10): initial prompt, 4/5 spike cases pass.
  v2 (2026-04-13): anchor uniqueness guidance added. 36-case eval
      showed 4 failures all caused by anchors matching sidebar TOC /
      breadcrumb text. Fix: instruct VLM to pick mid-paragraph text
      and avoid section headings that duplicate in navigation.
  v3 (2026-04-15): contiguous-run rule added. KBO schedule eval showed
      deterministic mapper failure (0/8) because VLM composed phrases
      like "KT vs NC score 6 to 0" and "broadcast info included" that
      have no matching contiguous text run in the DOM. Fix: require
      anchors to be literal contiguous text runs, forbid composed or
      summary phrases, and include a concrete bad/good example.
"""

SYSTEM_PROMPT = """\
You are analyzing a webpage screenshot to help extract its main content.
Output ONLY valid JSON matching the schema. No prose, no markdown fences,
no explanation. If a field cannot be determined, use null or an empty
list as appropriate, but never omit a field."""


USER_PROMPT = """\
Analyze this webpage screenshot and identify its meaningful content
region(s). Do not assume any specific question — describe what any
reader of this page would care about, treating the page as a whole.

Return JSON matching this schema EXACTLY:

{
  "page_type": "<one of: news_article | docs | schedule_or_results | product_or_pricing | forum_or_qa | listing | other>",
  "structure_description": "<2-4 sentences describing what this page is, its main visual regions, and what a human reader would care about. If repeating items exist (game rows, product cards, table rows), describe one row's fields.>",
  "content_anchors": [
    "<5 to 10 short exact-text snippets (3-10 words each) visibly present in the MAIN CONTENT area of the screenshot. CRITICAL RULES for picking anchors: (1) Each anchor MUST be a CONTIGUOUS text run exactly as it appears in a single visible line or cell on the page. DO NOT compose phrases by combining text from separate elements or boxes. Bad example: if the page shows team 'KT' in one cell, 'vs' implied by layout, team 'NC' in another cell, and a score '6' and '0' in score boxes, DO NOT produce 'KT vs NC score 6 to 0' — that phrase does not exist in the DOM as one run. Good example: pick a single visible run like '18:30' or a team name alone like '삼성' or a caption line that reads as one sentence. (2) Never invent summary phrases like 'score information included' or 'broadcast channel info' — only copy text that is literally printed on the page. (3) Pick text from INSIDE paragraphs, code blocks, data cells, or captions — NOT from section headings or titles. Section headings are often duplicated in sidebar tables of contents and breadcrumb navigation, which breaks DOM matching. (4) Each anchor must be text that a human can actually read in the image, not guessed. (5) Spread anchors across the content area, not clustered in one spot. (6) Avoid any text from nav bars, sidebars, footers, headers, cookie banners, or related-content widgets. (7) If the same text appears in both the main content and a sidebar/nav, do NOT use it.>"
  ],
  "noise_labels": [
    "<short freeform descriptions of regions to EXCLUDE: 'top nav', 'right sidebar ads', 'footer', 'related articles box', 'left sidebar table of contents', 'breadcrumb navigation', etc.>"
  ],
  "item_hints": {
    "has_repeating_items": <true or false>,
    "item_description": "<if true: one sentence describing one row's fields. If false: null.>",
    "example_row_anchors": [
      "<if has_repeating_items: 3-5 anchors that belong to ONE representative row. Each anchor must be a single contiguous text run as rule (1) above. Otherwise empty array.>"
    ]
  }
}

Output the JSON only. Do not wrap it in code fences."""


def build_user_prompt() -> str:
    """Return the user-role prompt string. Always query-agnostic.

    Kept as a function (rather than exposing the constant) so call sites
    read consistently with the spike's API and so future prompt-engineering
    iterations have a single attachment point.
    """
    return USER_PROMPT
