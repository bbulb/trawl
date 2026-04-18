# MutationObserver + LCP Content-Ready Spike — Conclusion

**Date:** 2026-04-17
**Branch:** `spike/mutation-lcp-wait`
**Design:** `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-design.md`
**Plan:** `docs/superpowers/plans/2026-04-17-mutation-lcp-wait.md`

## Result

**Decision:** REJECT

The spike reduces average playwright-path median `fetch_ms` by 13.0 %
after one retune round — short of the 15 % Adopt threshold — and
regresses `pricing_page_ko` by 12.1 %. Per the pre-registered
decision matrix (design doc § Success Criteria: "Retune … reject if
still < 15 %"), we reject.

## Numbers (playwright-path cases, median of 3 runs)

Baseline = `develop @ bad9cce` (networkidle budget cap only).

**Initial spike** — `QUIET_MS=400`, `LCP_POST_PAINT_MS=200`:

| case | base median | new median | Δ | pct |
|---|---|---|---|---|
| blog_post_no_heading | 1893 | 1446 | −447 | **−23.6 %** |
| english_tech_docs | 1791 | 1175 | −616 | **−34.4 %** |
| kbo_schedule | 2207 | 2202 | −5 | −0.2 % |
| korean_news_ranking | 1609 | 1188 | −421 | **−26.2 %** |
| pricing_page_ko | 3457 | 4032 | +575 | **+16.6 %** |
| very_short_page | 1260 | 883 | −377 | **−29.9 %** |
| **AVG** | **2036** | **1821** | **−215** | **−10.6 %** |

**Retune** — `QUIET_MS=500`, `LCP_POST_PAINT_MS=200`:

| case | base median | new median | Δ | pct |
|---|---|---|---|---|
| blog_post_no_heading | 1893 | 1461 | −432 | −22.8 % |
| english_tech_docs | 1791 | 1352 | −439 | −24.5 % |
| kbo_schedule | 2207 | 1921 | −286 | −13.0 % |
| korean_news_ranking | 1609 | 1180 | −429 | −26.7 % |
| pricing_page_ko | 3457 | 3876 | +419 | **+12.1 %** |
| very_short_page | 1260 | 837 | −423 | −33.6 % |
| **AVG** | **2036** | **1771** | **−265** | **−13.0 %** |

Parity: baseline 12 / 12 PASS, initial spike 12 / 12 PASS, retune 12 / 12 PASS.

## NVIDIA forum regression smoke (3 × cold)

| run | baseline (`bad9cce`) | spike (`QUIET_MS=400`) |
|---|---|---|
| 1 | 4,968 ms | 5,013 ms |
| 2 | 4,442 ms | 3,870 ms |
| 3 | 4,406 ms | 3,848 ms |
| **median** | **4,442 ms** | **3,870 ms** (−12.9 %) |

HTML length identical across all runs: 1,469,473 bytes. No regression
on the Discourse class that motivated the `bad9cce` fix — the LCP +
MutationObserver combination handled it at least as well as the
polling predicate.

## LCP support in stealth context

`PerformanceObserver.supportedEntryTypes` inside the Stealth-patched
Chromium includes `largest-contentful-paint`: **yes**.

Full list observed:
`['element', 'event', 'first-input', 'largest-contentful-paint', 'layout-shift', 'long-animation-frame', 'longtask', 'mark', 'measure', 'navigation', 'paint', 'resource', 'visibility-state']`

The LCP leg is therefore genuinely active — the outcome is not
confounded by silent fallback to the DOM-stable leg.

## Decision reasoning

| Threshold | Observed | Met? |
|---|---|---|
| Parity 12/12 (every run) | 3 × 3 green | yes |
| Avg median reduction ≥ 15 % (Adopt) | 13.0 % after retune | **no** |
| NVIDIA forum ≤ baseline (~4.4 s) | 3.87 s median | yes |

Five of six playwright-path cases improved meaningfully (−13 % to
−34 %), but one case (`pricing_page_ko`) regressed by ~12 % and did
not recover under the retune. The average therefore lands below the
15 % Adopt threshold on both attempts, and the design doc's
decision matrix explicitly routes 5–15 % retune to REJECT after one
round.

## Failure / oddities

- **`pricing_page_ko` regression (+12 %, +17 % pre-retune)**
  Baseline runs are tight (3345 / 3457 / 3539 ms); spike runs are
  bimodal (~3.1 s fast branch, ~4.0 s slow branch). Hypothesis:
  the page has perpetual low-frequency DOM churn (pricing table
  re-renders, analytics badges) that keeps resetting `lastMutationAt`
  on the slow branch. The LCP leg fires, but its 200 ms grace runs
  into another mutation, resetting the LCP-quiet timer and falling
  back to the `QUIET_MS` leg. The old polling predicate's
  `stableTicks` counter was more tolerant: a single brief mutation
  only dropped one tick, not the full window.

- **`kbo_schedule` nearly flat (−0.2 % / −13 % after retune)**
  Expected — this case takes the profile fast path and the content-
  ready wait is a small fraction of its already-short `fetch_ms`.

- **rAF-driven tick under heavy paint**
  Potentially part of the `pricing_page_ko` story. `requestAnimationFrame`
  can be delayed on pages with heavy layout/paint even in headless
  Chromium. A fixed-interval poll (e.g. `setInterval(33)`) would
  wake more predictably, but that is beyond the "one round of
  QUIET_MS / LCP_POST_PAINT_MS tuning" the design doc committed to
  and was not attempted.

## Why this doesn't warrant a further round

The `bad9cce` networkidle cap already delivered the lion's share
of the win (−67 % on the full parity matrix in the previous spike;
Discourse class 17 s → 4.4 s). This spike is residual-fat work.
Widening the effort to change polling strategy (rAF → setInterval)
would break the pre-registered scope and, given the one case is a
structural regression rather than a tuning miss, the expected gain
does not justify the added complexity.

## Next steps

- Keep `spike/mutation-lcp-wait` branch for reference; do not merge
  to `main` or `develop`.
- Leave the existing `bad9cce` polling predicate in place as the
  current best state.
- If a future session motivates revisiting this:
  1. Start from `pricing_page_ko` as a dedicated repro — identify the
     exact mutation pattern that defeats `QUIET_MS`.
  2. Consider a hybrid predicate that counts sustained high-rate
     mutation bursts separately from isolated late mutations
     (essentially the `stableTicks` semantics but event-driven).
  3. Consider `setInterval` instead of `requestAnimationFrame` to
     remove paint-throttling as a variable.
