"""Ad-hoc audit of candidate record-detection benchmark URLs.

Runs fetch_relevant + records.annotate_records on each candidate URL and
reports:
  - groups detected (signature, count, median_text_len, parent_tag)
  - n_chunks_total, page_chars, total_ms
  - top-5 chunks' heading + first 120 chars + record metadata
  - fetcher_used (some URLs get API fetcher bypass)

Output: tests/results/_audit_records/<ts>/report.md + raw.json.
Not committed; for spike analysis only.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from trawl import fetch_relevant, to_dict
from trawl import records as rec
from trawl.fetchers import playwright

logging.basicConfig(level=logging.WARNING)

CANDIDATES = [
    {
        "id": "weather_naver_today",
        "url": "https://weather.naver.com/today/09140650",
        "query": "오늘 서울 시간별 날씨",
        "genre": "weather",
    },
    {
        "id": "daum_news_popular",
        "url": "https://news.daum.net/ranking/popular",
        "query": "오늘 인기 뉴스",
        "genre": "news-ranking",
    },
    {
        "id": "naver_kbo_record",
        "url": "https://sports.news.naver.com/kbaseball/record/index",
        "query": "KBO 팀 순위",
        "genre": "sports-table",
    },
    {
        "id": "naver_finance_marketsum",
        "url": "https://finance.naver.com/sise/sise_market_sum.naver",
        "query": "코스피 시가총액 상위 종목",
        "genre": "finance-table",
    },
    {
        "id": "aladin_bestsellers",
        "url": "https://www.aladin.co.kr/shop/common/wbest.aspx?BranchType=1",
        "query": "알라딘 이번주 베스트셀러",
        "genre": "commerce-grid",
    },
    {
        "id": "wanted_frontend",
        "url": "https://www.wanted.co.kr/wdlist/518",
        "query": "프론트엔드 개발자 채용 공고",
        "genre": "jobs-list",
    },
    {
        "id": "hn_front",
        "url": "https://news.ycombinator.com/",
        "query": "today top tech news",
        "genre": "news-ranking-en",
    },
    {
        "id": "hada_front",
        "url": "https://news.hada.io/",
        "query": "최근 IT 뉴스",
        "genre": "news-aggregator",
    },
]


def audit_groups(url: str) -> list[dict]:
    """Render the page once and return detected record group metadata."""
    with playwright.render_session(url) as r:
        html = r.html
    _, groups = rec.annotate_records(html)
    return [
        {
            "group_id": g.group_id,
            "parent_tag": g.parent_tag,
            "signature": g.signature,
            "count": g.count,
            "median_text_len": g.median_text_len,
        }
        for g in groups
    ]


def main() -> None:
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(__file__).parent / "results" / "_audit_records" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for case in CANDIDATES:
        print(f"\n=== {case['id']} ===")
        print(f"url: {case['url']}")
        entry = {"case": case}
        try:
            groups = audit_groups(case["url"])
        except Exception as e:
            entry["error_audit"] = f"{type(e).__name__}: {e}"
            groups = []
        entry["groups"] = groups
        print(f"groups: {len(groups)}")
        for g in groups[:8]:
            print(f"  - {g['signature']!r} × {g['count']} (median_len={g['median_text_len']})")

        try:
            result = fetch_relevant(case["url"], case["query"])
            pd = to_dict(result)
        except Exception as e:
            entry["error_fetch"] = f"{type(e).__name__}: {e}"
            results.append(entry)
            continue
        entry["fetcher"] = pd.get("fetcher_used")
        entry["n_chunks_total"] = pd.get("n_chunks_total")
        entry["page_chars"] = pd.get("page_chars")
        entry["total_ms"] = pd.get("total_ms")
        entry["top_chunks"] = [
            {
                "score": c.get("score"),
                "heading": c.get("heading"),
                "record": (c.get("record_group_id"), c.get("record_index")),
                "text": (c.get("text") or "")[:160].replace("\n", " ⏎ "),
            }
            for c in (pd.get("chunks") or [])[:5]
        ]
        print(f"fetcher={entry['fetcher']} n_chunks={entry['n_chunks_total']} "
              f"page_chars={entry['page_chars']} total_ms={entry['total_ms']}")
        results.append(entry)

    (out_dir / "raw.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))

    lines = ["# Record detection audit", f"timestamp: {ts}", ""]
    for entry in results:
        c = entry["case"]
        lines.append(f"## {c['id']} ({c['genre']})")
        lines.append(f"URL: {c['url']}")
        lines.append(f"Query: `{c['query']}`")
        if "error_audit" in entry:
            lines.append(f"AUDIT ERROR: {entry['error_audit']}")
        if "error_fetch" in entry:
            lines.append(f"FETCH ERROR: {entry['error_fetch']}")
            lines.append("")
            continue
        lines.append(
            f"fetcher={entry['fetcher']}  n_chunks={entry['n_chunks_total']}  "
            f"page_chars={entry['page_chars']}  total_ms={entry['total_ms']}"
        )
        lines.append(f"groups_detected={len(entry.get('groups', []))}")
        for g in entry.get("groups", [])[:10]:
            lines.append(
                f"  - `{g['signature']}` × {g['count']} "
                f"(median_len={g['median_text_len']}, parent={g['parent_tag']})"
            )
        lines.append("")
        lines.append("top-5 chunks:")
        for i, ch in enumerate(entry.get("top_chunks") or []):
            rec_meta = ""
            if ch["record"][0] is not None:
                rec_meta = f" [rec {ch['record'][0]}/{ch['record'][1]}]"
            score = ch.get("score")
            score_s = f"{score:.2f}" if score is not None else "--"
            lines.append(f"  [{i}] {score_s}{rec_meta}  {ch['text']}")
        lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines))
    print(f"\nWrote {out_dir / 'report.md'}")


if __name__ == "__main__":
    main()
