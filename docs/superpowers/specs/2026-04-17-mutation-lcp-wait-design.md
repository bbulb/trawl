# MutationObserver + LCP Content-Ready Spike — Design

**Date:** 2026-04-17
**Branch:** `spike/mutation-lcp-wait` (local, off `develop` at `bad9cce`)
**Type:** Spike (extends `fetchers/playwright.py:_wait_for_content_ready` from polling-based text-length diffing to an event-driven `MutationObserver` + `PerformanceObserver(largest-contentful-paint)` hybrid)
**Status:** Draft

## Goal

Replace the current `wait_for_function(polling=150)` + `stableTicks >= 4`
text-length heuristic with an **event-driven** content-ready detector:

- `MutationObserver` resets a quiet-period timer on every DOM mutation;
  the page is "DOM-stable" once the timer fires.
- `PerformanceObserver({type: 'largest-contentful-paint'})` supplies an
  **early-exit** signal on Chromium: once LCP has fired, we know the
  browser itself believes the biggest content has been painted.

Exit when `(lcpFired OR textStable) AND selOk`, bounded by the same
`wait_for_ms` ceiling. Pre-LCP pages fall back to the DOM-stable leg,
so behaviour on non-Chromium engines or LCP-suppressed contexts is
strictly no worse than today.

Expected outcome: playwright-path median `fetch_ms` drops further on
fast pages (`very_short_page`, `example.com`, `github_readme`) and
image-heavy pages where LCP fires well before DOM quiescence.

## Why

The networkidle fix (`bad9cce`) cut Discourse-class SPA fetches from
17s to 4.4s. The remaining cost on the playwright path now breaks down
roughly as:

| Stage | Typical contribution |
|---|---|
| `goto()` to first paint | 400–1500 ms |
| DOM quiescence detection | 600 ms stability window + up to 150 ms polling slack |
| (stable / rerank / extraction) | separate stages |

The 600 ms stability window is a **lower bound** of the current
design: we cannot report ready sooner even on a page that's truly
static after first paint. A 150 ms poll interval adds up to one more
tick of slack.

Two known-good signals that could tighten the floor:

1. **MutationObserver** fires synchronously on DOM mutation. A
   quiet-period timer started/reset on every mutation resolves exactly
   `QUIET_MS` after the last change — no polling slack. With the same
   600 ms budget we save up to ~150 ms per fetch; with a tighter
   300–400 ms budget we save another 200–300 ms, using the LCP signal
   as the insurance that the page is actually rendered.

2. **`largest-contentful-paint` PerformanceEntry** is the browser's
   own "biggest thing has been painted" signal. On many image-heavy or
   hero-banner pages LCP fires well before the DOM stops mutating
   (lazy-loaded footer/sidebar can churn for seconds after the article
   is fully readable). Exiting on LCP + brief quiet window gets us the
   user-perceivable content without waiting for the tail.

Supporting evidence: BrowserStack / web.dev / MDN all document LCP as
the canonical "main content rendered" signal for Core Web Vitals;
MutationObserver-based quiescence is a long-standing open Playwright
feature request (microsoft/playwright#26618). Neither is novel — we
just haven't layered them yet.

## Scope

**In scope:**
- `fetchers/playwright.py:_wait_for_content_ready` — rewrite predicate
  as a `page.evaluate()`-installed MutationObserver + LCP observer
  pair, then await a single `window.__trawl_ready_promise`.
- Retain existing signature (`profile_selector`, `max_wait_ms`) and
  no-op on timeout semantics.
- Tune two new constants: `QUIET_MS` (DOM quiet window) and
  `LCP_POST_PAINT_MS` (grace period after LCP before early exit).

**Out of scope:**
- Changing `wait_for_ms` default (5000) — the ceiling is still the
  safety net.
- Per-domain adaptive timeouts — future work, unchanged.
- Query-aware early stop — separate spike.
- Replacing the `networkidle` budget — already handled in `bad9cce`.
- Firefox/WebKit LCP — we run Chromium only; LCP leg is guarded and
  the DOM-stable leg remains the fallback.

## Success Criteria

Pre-registered so the result has a clear next step.

| Outcome | Parity | Playwright-path median `fetch_ms` reduction vs `bad9cce` | Decision |
|---|---|---|---|
| Adopt | 12 / 12 | ≥ 15 % | Merge to `develop`, update `CLAUDE.md` |
| Retune | 12 / 12 | 5–15 % | One round of `QUIET_MS` / `LCP_POST_PAINT_MS` tuning; reject if still < 15 % |
| Reject | < 12 / 12, or no gain | — | Discard branch, keep for reference |

**Measurement protocol:**

1. Baseline = `develop @ bad9cce`, 3 × `python tests/test_pipeline.py`,
   take per-case median `fetch_ms` from `tests/results/<ts>/summary.json`.
2. Spike branch, same 3 × run.
3. Compare only the six playwright-path cases: `kbo_schedule`,
   `korean_news_ranking`, `pricing_page_ko`, `english_tech_docs`,
   `blog_post_no_heading`, `very_short_page`.
4. Report both median and p90 so tail-latency regressions are visible.
5. Secondary: re-run the NVIDIA forum URL (from `~/.trawl/telemetry.jsonl`)
   3 × cold, confirm ≤ current 4.4 s baseline.

**Independent sanity check:** browser-internal polling cost. A
`page.evaluate()` benchmark measuring CPU time spent in the predicate
per fetch. Not a go/no-go criterion, but recorded in the conclusion.

## Architecture

Single-file change in `fetchers/playwright.py`. The existing helper
signature is preserved; only the body is rewritten.

### New predicate structure

```js
// Installed once via page.evaluate() at the start of _wait_for_content_ready.
// Exposes window.__trawl_ready = { promise, resolve, lcpFired, lastMutationAt }.
(profileSelector, quietMs, lcpPostPaintMs, maxMs) => {
  const state = window.__trawl_ready = {
    lcpFired: false,
    lastMutationAt: performance.now(),
    resolve: null,
    promise: null,
  };
  state.promise = new Promise((res) => { state.resolve = res; });

  const selOk = () => {
    if (!profileSelector) return true;
    const el = document.querySelector(profileSelector);
    if (!el) return false;
    const t = el.innerText.trim();
    if (t.length < 50) return false;
    return !/^(—+|---+|\.{3,}|loading)$/i.test(t);
  };

  // Leg 1: DOM quiescence via MutationObserver + quiet timer.
  const mo = new MutationObserver(() => {
    state.lastMutationAt = performance.now();
  });
  mo.observe(document.body, {
    subtree: true, childList: true, characterData: true, attributes: false,
  });

  // Leg 2: LCP early-exit. Chromium only; silently no-ops elsewhere.
  try {
    const po = new PerformanceObserver((entries) => {
      if (entries.getEntries().length) state.lcpFired = true;
    });
    po.observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (_) { /* LCP unsupported */ }

  const tickStart = performance.now();
  const tick = () => {
    const now = performance.now();
    const quietFor = now - state.lastMutationAt;
    const domStable = quietFor >= quietMs && document.body.innerText.length > 100;
    const lcpReady = state.lcpFired && quietFor >= lcpPostPaintMs;
    if ((domStable || lcpReady) && selOk()) {
      mo.disconnect();
      state.resolve(true);
      return;
    }
    if (now - tickStart >= maxMs) {
      mo.disconnect();
      state.resolve(false);  // timed out; caller proceeds anyway
      return;
    }
    // Coalesce with rAF so we wake on paint, not on a fixed interval.
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  return state.promise;
}
```

Python side collapses to a single `await` equivalent:

```python
def _wait_for_content_ready(page, *, profile_selector, max_wait_ms):
    page.evaluate(
        "([sel, quiet, lcpGrace, maxMs]) => { /* predicate above */ }",
        arg=[profile_selector, QUIET_MS, LCP_POST_PAINT_MS, max_wait_ms],
    )
    # Predicate returns the promise; Playwright auto-awaits it.
```

### Constants

| Name | Value | Rationale |
|---|---|---|
| `QUIET_MS` | `400` | Tightens the current 600 ms stability window. LCP leg picks up the slack when available; for pages without LCP (rare in Chromium), 400 ms is still enough to drop the second XHR wave on the 12-case parity matrix. |
| `LCP_POST_PAINT_MS` | `200` | Short dwell after LCP fires — enough to let the LCP element's sibling lazy-loads settle without waiting for unrelated tail mutations. |

Both are registered in `CLAUDE.md`'s "Things NOT to change" table on
adoption.

### Interaction with existing signals

```
page.goto(..., wait_until="networkidle", timeout=NETWORKIDLE_BUDGET_MS)   # bad9cce, unchanged
      ↓ (or fallback to domcontentloaded)
page.evaluate(/* install MutationObserver + LCP observer, return promise */)
      ↓ resolves on: (DOM quiet 400ms) OR (LCP fired + 200ms quiet), AND selOk
      ↓ or rejects on max_wait_ms=5000 ceiling — swallowed
html = page.content()
```

The `networkidle` budget, `wait_for_ms` ceiling, and `profile_selector`
placeholder check are all preserved verbatim.

## Data flow comparison

| Scenario | Current (`bad9cce`) | Spike |
|---|---|---|
| Static page, fast first paint | ~750 ms (600 stability + 150 poll slack) | ~200 ms (LCP + 200 ms) |
| SPA, second XHR wave at 1 s | 1 s + 600 ms = 1.6 s | 1 s + 400 ms = 1.4 s |
| Discourse long-polling | 4.4 s (networkidle cap + DOM stable) | ≈ same; LCP may fire early but tail mutations reset quiet timer |
| Non-Chromium (hypothetical) | unchanged | DOM-stable leg identical; LCP leg no-ops |

## Error Handling

- `PerformanceObserver` throws on unsupported entry type (older engines)
  → try/catch in JS, LCP leg disabled, DOM-stable leg alone drives exit.
- `MutationObserver` throws if `document.body` is null (extremely rare:
  navigation about:blank) → predicate returns immediately with
  `false`; Python side's `PlaywrightTimeoutError` or normal return
  path takes over.
- `page.evaluate` timeout → same swallowed-timeout semantics as today;
  caller reads whatever HTML is present. No new failure mode.
- `window.__trawl_ready` name collision with site globals → extremely
  unlikely; rename to `__trawl_ready_v2` to sidestep any residual
  state from the `bad9cce` version on reused contexts.

## Testing Plan

Primary: parity matrix — `python tests/test_pipeline.py` must stay
12/12 across 3 × runs on the spike branch.

Secondary smoke tests:

```bash
# Fast-page LCP exit
python -c "
from trawl.fetchers import playwright as pw
import time
t0 = time.monotonic()
r = pw.fetch('https://example.com/')
print('ms:', int((time.monotonic() - t0) * 1000), 'len:', len(r.html))
"
# Expected: ms well below current ~1500, content intact.

# Discourse cold — regression check vs bad9cce
python -c "
from trawl.fetchers import playwright as pwf
import time
url = 'https://forums.developer.nvidia.com/t/moving-ridgebackfranka-robot-with-articulations-does-not-change-the-pose-of-the-robot/242112'
for _ in range(3):
    t0 = time.monotonic()
    r = pwf.fetch(url)
    print(int((time.monotonic()-t0)*1000), 'ms', r.ok, len(r.html))
"
# Expected: each run ≤ current 4.4–4.7 s baseline.
```

Targeted reproduction: profile-enabled call path. Run one of the
profiled benchmark cases (`benchmarks/run_benchmark.py --only kbo_schedule
--profile`) to confirm the `profile_selector` branch still gates
correctly on selector population.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| LCP fires on a splash / skeleton image before the real content is rendered, causing premature exit | `selOk` (placeholder regex + `len ≥ 50`) still gates the exit when a profile is loaded. For non-profile cases, `QUIET_MS=400` still applies — LCP-only early-exit requires a real quiet window. |
| Long-tail mutations (footer lazy-loads, analytics DOM injections) keep resetting `lastMutationAt`, so we never reach `QUIET_MS=400` quiet | LCP leg rescues this: once LCP fired, `LCP_POST_PAINT_MS=200` quiet is sufficient; tail mutations on non-LCP regions no longer block exit. |
| `MutationObserver` itself incurs GC / callback overhead on busy pages | We coalesce only `lastMutationAt` updates; no per-mutation work beyond a single timestamp write. Benchmark the predicate's CPU cost during the spike as a sanity check. |
| `performance.now()` monotonicity across navigation — not a concern since the observer installs *after* `goto` resolves | None needed; note it in conclusion for completeness. |
| `window.__trawl_ready` leaking between reused contexts (`render_session` across multiple pages) | Rename to `__trawl_ready_v2` + reinstall every call. Existing context-close-per-fetch already resets state. |
| `PerformanceObserver({buffered: true})` returning stale LCP from previous navigation in the same context | Same mitigation — context is closed between fetches in the `fetch()` path. For `render_session` multi-nav, document that the caller must reinstall; first-use today is single-nav anyway. |
| Site CSP blocks `new PerformanceObserver({type: 'largest-contentful-paint'})` | Wrapped in try/catch; LCP leg silently disabled. DOM-stable leg unaffected. |

## Deliverables

1. Code change: `fetchers/playwright.py` (`_wait_for_content_ready`
   body + two new constants) — single file.
2. `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-design.md`
   (this document).
3. `docs/superpowers/plans/2026-04-17-mutation-lcp-wait.md`
   (implementation plan — next step).
4. `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-conclusion.md`
   (final decision per success criteria, with baseline vs spike table).
5. If Adopt: `CLAUDE.md` "Things NOT to change" table gains `QUIET_MS`
   and `LCP_POST_PAINT_MS` rows; follow-up PR to `main`.

## Open Questions

Resolved empirically by the spike:

- Is `QUIET_MS=400` too tight for `pricing_page_ko` (the Notion/BS-served
  case) where JS-rendered pricing tables arrive late? If it regresses
  parity, first tuning step is `QUIET_MS=500`.
- Does Chromium's LCP observation survive our `Stealth` patches? The
  stealth library monkey-patches `navigator.webdriver` and similar;
  neither should touch the Performance API, but we verify with a
  one-liner that logs `PerformanceObserver.supportedEntryTypes` from
  inside the context at the start of the spike.
- Does `very_short_page` (example.com, ~70 chars body text) have a
  legitimate LCP entry? If not, the DOM-stable leg with `len > 100`
  would block it; we explicitly keep the `len > 100` floor for
  parity with the current design, and expect the 5 s ceiling to
  catch it — same as today.
