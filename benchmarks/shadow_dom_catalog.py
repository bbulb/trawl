"""Shadow-DOM custom-element catalog for `SHADOW_DOM_UNWRAP_TAGS`.

Scans a set of URLs (defaults to the 16 `code_heavy_query` patterns
in `tests/agent_patterns/coding.yaml`) and reports, per URL:

    - which custom elements (tag names containing `-`) are present,
    - how many have a populated shadow root,
    - whether the shadow root has `pre > code` structure (our
      current unwrap pattern),
    - a preview of the shadow textContent so we can decide if a
      given tag is code-block-bearing.

Intent: after PR #34 shipped `SHADOW_DOM_UNWRAP_TAGS = ("mdn-code-
example",)`, the 0.4.0 release notes flagged allow-list expansion
as a follow-up. Rather than waiting for a new `code_heavy_query`
failure to surface, this script catalogues **what's actually on
the URLs we already test** so additions to the allow-list go
through an informed decision rather than a reactive one.

Runs with `TRAWL_SHADOW_DOM_UNWRAP=0` so the scan sees the raw
page state (no unwrap applied), then queries `element.shadowRoot`
directly via `page.evaluate()`.

Invoke:
    python benchmarks/shadow_dom_catalog.py --dry-run
    python benchmarks/shadow_dom_catalog.py                   # 16 coding URLs
    python benchmarks/shadow_dom_catalog.py --shards coding,wiki_reference
    python benchmarks/shadow_dom_catalog.py --url <one URL>

Writes `benchmarks/results/shadow-dom-catalog/<ts>/`:
    catalog.json   per-URL custom-element breakdown
    catalog.md     human-readable report with candidate-tag summary

Exit code:
    0  — scan completed.
    2  — infra failure (first URL unfetchable, etc.).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
RESULTS_ROOT = BENCH_DIR / "results" / "shadow-dom-catalog"
TESTS_DIR = REPO_ROOT / "tests"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_patterns import load_shard  # noqa: E402

DEFAULT_SHARDS = ("coding",)


def _probe_page(url: str, verbose: bool) -> dict[str, Any]:
    """Fetch the URL with Playwright (unwrap disabled) and dump custom
    elements + their shadow-root structure. Uses our own Playwright
    plumbing so we inherit the stealth + wait-for-content-ready
    tuning. The env var `TRAWL_SHADOW_DOM_UNWRAP=0` is set by main()
    before importing the fetcher.
    """
    from trawl.fetchers.playwright import _browser_holder, _lock, _open_context

    # Ensure browser exists (ensure is idempotent).
    _browser_holder.ensure()

    t0 = time.monotonic()
    info: dict[str, Any] = {"url": url, "ok": False}

    with _lock:
        try:
            with _open_context(
                url, wait_for_ms=5000, timeout_s=30.0, user_agent=None,
                profile_selector=None,
            ) as (_ctx, page, _html, _ct):
                info.update(page.evaluate("""
                    () => {
                        const customEls = Array.from(document.querySelectorAll('*'))
                            .filter(el => el.tagName.includes('-'));
                        const byTag = {};
                        for (const el of customEls) {
                            const tag = el.tagName.toLowerCase();
                            let entry = byTag[tag];
                            if (!entry) {
                                entry = {
                                    tag,
                                    count: 0,
                                    shadow_count: 0,
                                    with_pre_code: 0,
                                    populated_shadow: 0,
                                    shadow_preview: null,
                                };
                                byTag[tag] = entry;
                            }
                            entry.count += 1;
                            const root = el.shadowRoot;
                            if (root) {
                                entry.shadow_count += 1;
                                const html = root.innerHTML || '';
                                if (html.trim().length > 0) {
                                    entry.populated_shadow += 1;
                                    if (!entry.shadow_preview) {
                                        entry.shadow_preview = {
                                            html_head: html.slice(0, 300),
                                            text_head: (root.textContent || '').slice(0, 300),
                                        };
                                    }
                                }
                                if (root.querySelector('pre > code')) {
                                    entry.with_pre_code += 1;
                                }
                            }
                        }
                        return {
                            ok: true,
                            custom_element_tags: Object.values(byTag).sort((a, b) => b.count - a.count),
                            total_custom_elements: customEls.length,
                            total_shadow_roots: customEls.filter(e => e.shadowRoot).length,
                        };
                    }
                """))
        except Exception as e:  # noqa: BLE001
            info["ok"] = False
            info["error"] = f"{type(e).__name__}: {e}"
            info["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
            return info

    info["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    if verbose:
        n_tags = len(info.get("custom_element_tags") or [])
        n_shadow = info.get("total_shadow_roots", 0)
        print(f"  {url[:70]} → {n_tags} custom tags, {n_shadow} shadow roots, {info['elapsed_ms']}ms", file=sys.stderr)
    return info


def _aggregate_candidates(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-tag stats across pages so we can rank candidates for
    the `SHADOW_DOM_UNWRAP_TAGS` allow-list.
    """
    agg: dict[str, Any] = {}
    for p in pages:
        for entry in p.get("custom_element_tags") or []:
            tag = entry["tag"]
            if tag not in agg:
                agg[tag] = {
                    "tag": tag,
                    "total_count": 0,
                    "total_shadow": 0,
                    "total_populated_shadow": 0,
                    "total_with_pre_code": 0,
                    "seen_on_urls": [],
                    "sample_preview": None,
                }
            a = agg[tag]
            a["total_count"] += entry["count"]
            a["total_shadow"] += entry["shadow_count"]
            a["total_populated_shadow"] += entry["populated_shadow"]
            a["total_with_pre_code"] += entry["with_pre_code"]
            a["seen_on_urls"].append(p["url"])
            if a["sample_preview"] is None and entry.get("shadow_preview"):
                a["sample_preview"] = entry["shadow_preview"]

    # Sort: prioritise tags whose shadow root contains `pre > code`
    # (our current unwrap signature) and are present on multiple URLs.
    sorted_tags = sorted(
        agg.values(),
        key=lambda t: (t["total_with_pre_code"], t["total_populated_shadow"], t["total_count"]),
        reverse=True,
    )
    return {"candidates": sorted_tags, "total_unique_tags": len(agg)}


def _render_md(catalog: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"# Shadow-DOM custom-element catalog — {catalog['generated_at']}")
    lines.append("")
    lines.append(f"**Scanned URLs:** {len(catalog['pages'])}")
    lines.append(f"**Unique custom-element tag names:** {catalog['summary']['total_unique_tags']}")
    lines.append(f"**Current allow-list (`SHADOW_DOM_UNWRAP_TAGS`):** `{catalog['current_allow_list']}`")
    lines.append("")
    lines.append("## Candidate tags, ranked by shadow-`pre > code` presence")
    lines.append("")
    lines.append("| tag | total | shadow | populated | `pre > code` | # URLs |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for c in catalog["summary"]["candidates"][:30]:
        lines.append(
            f"| `{c['tag']}` | {c['total_count']} | {c['total_shadow']} | "
            f"{c['total_populated_shadow']} | {c['total_with_pre_code']} | "
            f"{len(c['seen_on_urls'])} |"
        )
    lines.append("")
    lines.append("### Candidates with `pre > code` in shadow root")
    lines.append("")
    preco = [c for c in catalog["summary"]["candidates"] if c["total_with_pre_code"] > 0]
    if not preco:
        lines.append("_None — the current allow-list or Shadow-DOM-less pages only._")
    else:
        for c in preco:
            lines.append(f"#### `{c['tag']}`")
            lines.append(f"- Total elements: {c['total_count']}")
            lines.append(f"- Populated shadow roots: {c['total_populated_shadow']}")
            lines.append(f"- `pre > code` matches: {c['total_with_pre_code']}")
            lines.append(f"- Seen on URLs:")
            for u in c["seen_on_urls"]:
                lines.append(f"    - `{u}`")
            if c["sample_preview"]:
                preview = c["sample_preview"]
                lines.append("- Sample `textContent` preview:")
                lines.append("")
                lines.append("  ```")
                lines.append("  " + (preview.get("text_head") or "").replace("\n", " ")[:240])
                lines.append("  ```")
            lines.append("")
    lines.append("## Per-URL detail")
    lines.append("")
    for p in catalog["pages"]:
        if not p.get("ok"):
            lines.append(f"### `{p['url']}` — **fetch failed**")
            lines.append(f"  - error: {p.get('error')}")
            lines.append("")
            continue
        tags = p.get("custom_element_tags") or []
        lines.append(f"### `{p['url']}`")
        lines.append(
            f"- total custom elements: {p['total_custom_elements']}, "
            f"shadow roots: {p['total_shadow_roots']}"
        )
        if tags:
            lines.append("- tags:")
            for t in tags[:10]:
                shadow_note = ""
                if t["shadow_count"]:
                    parts = []
                    parts.append(f"{t['shadow_count']} shadow")
                    if t["populated_shadow"]:
                        parts.append(f"{t['populated_shadow']} populated")
                    if t["with_pre_code"]:
                        parts.append(f"{t['with_pre_code']} `pre>code`")
                    shadow_note = " (" + ", ".join(parts) + ")"
                lines.append(f"    - `{t['tag']}` × {t['count']}{shadow_note}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Force unwrap OFF for this scan so we see the raw page.
    os.environ["TRAWL_SHADOW_DOM_UNWRAP"] = "0"

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shards", default=",".join(DEFAULT_SHARDS),
                        help="Comma-separated shard names from tests/agent_patterns/*.yaml. Default: coding.")
    parser.add_argument("--category", default="code_heavy_query",
                        help="Optional category filter on pattern metadata. Default: code_heavy_query. Use empty to disable.")
    parser.add_argument("--url", action="append", default=[], help="Override URL list (repeatable).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    urls: list[str] = list(args.url)
    if not urls:
        for shard in args.shards.split(","):
            shard = shard.strip()
            if not shard:
                continue
            try:
                patterns = load_shard(shard)
            except Exception as e:  # noqa: BLE001
                print(f"failed to load shard {shard!r}: {e}", file=sys.stderr)
                return 2
            if args.category:
                patterns = [p for p in patterns if p.category == args.category]
            urls.extend(p.url for p in patterns)

    if not urls:
        print("no URLs selected", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"plan: scan {len(urls)} URLs")
        for u in urls:
            print(f"  {u}")
        return 0

    ts = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
    out_dir = RESULTS_ROOT / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"catalog → {out_dir}", file=sys.stderr)
    print(f"  scanning {len(urls)} URLs", file=sys.stderr)

    # Import current allow-list for the report header.
    from trawl.fetchers.playwright import SHADOW_DOM_UNWRAP_TAGS

    pages: list[dict[str, Any]] = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}", file=sys.stderr)
        pages.append(_probe_page(url, args.verbose))

    summary = _aggregate_candidates(pages)
    catalog = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "current_allow_list": list(SHADOW_DOM_UNWRAP_TAGS),
        "pages": pages,
        "summary": summary,
    }
    (out_dir / "catalog.json").write_text(
        json.dumps(catalog, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "catalog.md").write_text(_render_md(catalog), encoding="utf-8")

    print(f"\nunique tags: {summary['total_unique_tags']}", file=sys.stderr)
    with_preco = [c for c in summary["candidates"] if c["total_with_pre_code"] > 0]
    print(f"tags with `pre > code` shadow: {len(with_preco)}", file=sys.stderr)
    for c in with_preco:
        print(
            f"  `{c['tag']}`: {c['total_with_pre_code']} pre>code hits on {len(c['seen_on_urls'])} URLs",
            file=sys.stderr,
        )
    print(f"report: {out_dir}/catalog.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
