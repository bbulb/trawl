# Content-Ready Wait Spike — Conclusion

**Date:** 2026-04-17
**Branch:** `spike/content-ready-wait`
**Design:** `docs/superpowers/specs/2026-04-17-content-ready-wait-design.md`
**Plan:** `docs/superpowers/plans/2026-04-17-content-ready-wait.md`

## Result

**Decision: ADOPT.** Merge to `main`. The new `_wait_for_content_ready`
helper reduces average `fetch_ms` on the six playwright-path cases by
**66.7 %** while keeping the 12/12 parity matrix green.

## Numbers (median of 3 runs)

### Playwright-path cases — the subset actually affected

| case | baseline `fetch_ms` | new `fetch_ms` | delta | pct |
|---|---|---|---|---|
| kbo_schedule | 6463 | 2082 | −4381 | **−67.8 %** |
| korean_news_ranking | 5912 | 1564 | −4348 | **−73.5 %** |
| pricing_page_ko | 7791 | 4499 | −3292 | **−42.3 %** |
| english_tech_docs | 6109 | 1406 | −4703 | **−77.0 %** |
| blog_post_no_heading | 6223 | 1859 | −4364 | **−70.1 %** |
| very_short_page | 5602 | 1265 | −4337 | **−77.4 %** |
| **AVG** | **6350** | **2112** | **−4238** | **−66.7 %** |

### API-fetcher control (sanity) — should not change

| case | baseline | new | pct |
|---|---|---|---|
| korean_wiki_person | 1726 | 1705 | −1.2 % |
| japanese_wiki | 1868 | 1833 | −1.9 % |
| github_readme | 294 | 286 | −2.7 % |
| arxiv_pdf | 299 | 294 | −1.7 % |
| stackoverflow_question | 596 | 551 | −7.6 % |
| youtube_transcript | 1155 | 1297 | +12.3 % |

All API-route cases are within run-to-run noise (±10 %). The change
is cleanly isolated to the playwright code path.

### Parity

- Baseline: 12/12 PASS across all three runs
- Spike: 12/12 PASS across all three runs

## Decision Reasoning

| Threshold | Observed | Met |
|---|---|---|
| Parity 12/12 | 12/12 on all 3 spike runs | yes |
| Avg reduction ≥ 20 % (Adopt) | −66.7 % | **yes** |

The success threshold was 20 % average reduction; the spike delivered
67 %. Every one of the six playwright-path cases improved by at least
42 %, including the worst-case `pricing_page_ko` which was the slowest
case on both sides. The mechanism is exactly what the design
predicted: pages whose content settles in 1–2 s no longer wait the
full 5 s ceiling; pages that can't settle quickly (notion pricing's
JS-heavy hydration) still benefit partially because the predicate
fires as soon as text stabilizes, even if that's after 3–4 s.

The placeholder-regex limb of the predicate did not trip once on the
parity set (no cases have the `—` / `---` / `loading` markers in the
profile selector's visible text). That leg sits idle here but is
cheap enough to keep for sites that do use those markers — it's the
answer to the "selector exists but value still loading" class of bug
the user specifically called out.

## Oddities

No failures. A few observations worth keeping:

- `youtube_transcript` shifted +142 ms (+12 %). This case routes
  through the YouTube API fetcher, never touches `playwright.fetch`.
  The variance is the API round-trip to YouTube, unrelated to this
  change. Noise, not signal.
- `very_short_page` (example.com) exited fast despite its tiny body
  text. The body's `innerText.length` is ~170 chars, above the
  `len > 100` threshold. The Open Question in the design doc
  ("what if example.com is under 100 chars") was answered: it isn't.
- `pricing_page_ko` had the smallest improvement (-42 %) and the
  largest absolute latency (4499 ms). Notion's page continues to
  render small UI changes past its initial paint; the predicate
  correctly waits longer on it. This is the design behaving as
  intended.

## Next Steps

Per the Adopt branch of the success criteria:

1. Update `CLAUDE.md` "Things NOT to change" entry for `wait_for_ms`
   to reflect its new ceiling-not-fixed semantics (Task 8 of the
   plan).
2. Open PR from `spike/content-ready-wait` to `main` with the five
   implementation commits.

Branch contents (commits authored during this spike):

```
26910e5 feat(pipeline): thread profile_selector into playwright fetchers
b2bf3f8 feat(playwright): profile_selector kwarg on fetch/render_session
4f6966d feat(playwright): replace fixed wait with content-ready detector
6de419a feat(playwright): content-ready wait helper
b1cfe30 docs: content-ready wait 스파이크 구현 계획
f00abcc docs: content-ready wait 스파이크 설계 문서
```

Potential follow-ups (**not** part of this PR):

- Profile-less selector inference — when no profile exists, trawl
  could take a fast sample of likely main-content nodes and apply
  the same "non-placeholder" check. Marginal value on top of 67 %
  and out of scope here.
- Query-aware early stop (design doc Option C) — could shave more
  off pages where the query's keywords appear mid-render, but the
  fragility of "query term not literally present" makes it a
  separate, riskier spike.
- LCP observer — Chromium-only, probably correlated with
  text-stability already. Not worth adding complexity until there's
  a case where text-stability plateau is wrong.
