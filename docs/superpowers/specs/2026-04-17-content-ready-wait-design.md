# Content-Ready Wait Spike — Design

**Date:** 2026-04-17
**Branch:** `spike/content-ready-wait` (local, off `develop` at `e275f29`)
**Type:** Spike (replaces `fetchers/playwright.py:_open_context`'s fixed 5 s wait with content-ready detection)
**Status:** Draft

## Goal

Replace the fixed `page.wait_for_timeout(wait_for_ms=5000)` inside
`fetchers/playwright.py:_open_context` with a content-ready detector
that exits as soon as the page's visible content is stable (and, when
a profile is available, as soon as the profile's main selector holds
non-placeholder content). Keep the 5 s ceiling as a hard upper bound
so worst-case behavior is identical to today's fixed wait.

Expected outcome: average fetch latency drops meaningfully while the
12-case parity matrix stays 12/12.

## Why

`wait_for_ms=5000` is a coarse conservative wait. It is load-bearing
for SPAs that fire a second XHR wave after `networkidle` (documented
as "Things NOT to change" in `CLAUDE.md`). On pages that finish
rendering in 400–1500 ms, the remaining 3–4 s is pure padding. Across
the 12-case parity matrix, the playwright-path subset spends roughly
60–90 % of `fetch_ms` inside this wait.

The **fundamental limitation** of any "content-ready" heuristic is
that the browser cannot tell us when the content the *user* cares
about is ready. We approximate with two signals:

1. **Text stability** — `document.body.innerText.length` is stable
   for 600 ms. Works for most pages: any DOM mutation that appends or
   replaces text breaks stability until it settles.
2. **Profile selector content check** — when a profile is loaded for
   the URL, the `main_selector` must contain non-placeholder text
   (`≥ 50 chars`, not `—` / `---` / `...` / `loading`). This catches
   the case where the skeleton DOM is rendered before JS populates the
   actual data — a limit of plain `wait_for_selector`.

Neither signal is perfect; the 5 s ceiling is the safety net.

## Scope

**In scope:**
- `fetchers/playwright.py:_open_context` — replace fixed wait with
  content-ready detector
- Add kwarg `profile_selector: str | None = None` to `_open_context`,
  `fetch()`, `render_session()`
- Thread the selector from `pipeline.py` call sites that already hold
  a loaded `Profile` object

**Out of scope:**
- Query-aware early stop (cheaper signal but fragile on queries whose
  terms aren't literally on the page) — follow-up spike if warranted
- LCP PerformanceEntry observation — separate experiment
- Per-domain adaptive timeouts — listed as future work in
  `ARCHITECTURE.md` # 2
- Profile-less selector inference — trivially impossible without
  profile

## Success Criteria

Pre-registered so the result has a clear next step.

| Outcome | Parity | Avg `fetch_ms` reduction (playwright-path cases) | Decision |
|---|---|---|---|
| Adopt | 12 / 12 | ≥ 20 % | Write follow-up PR to merge to `main` |
| Retune | 12 / 12 | < 20 % | One round of parameter tuning (stability window, min text, placeholder regex); if still < 20 %, reject |
| Reject | < 12 / 12 | — | Discard branch, keep for reference |

**Measurement protocol:** run `python tests/test_pipeline.py` three
times on `develop` (baseline) and three times on the spike branch
after the change, take the median `fetch_ms` per case from
`tests/results/<ts>/summary.json`. Only the six playwright-path cases
matter: kbo_schedule, korean_news_ranking, pricing_page_ko,
english_tech_docs, blog_post_no_heading, very_short_page. The six
API-fetcher cases never hit `playwright.fetch` and are reported as
control.

## Architecture

Single-file change in `fetchers/playwright.py`, plus a 3-site edit in
`pipeline.py` to pass the selector through.

### New helper

```python
def _wait_for_content_ready(
    page: Page, *, profile_selector: str | None, max_wait_ms: int
) -> None:
    """Block until text-content is stable and (if profile) selector has
    non-placeholder content, or until `max_wait_ms` elapses. On
    timeout, swallow PlaywrightTimeoutError and return — caller reads
    whatever HTML is present, same semantics as the old fixed wait.
    """
```

It uses Playwright's `page.wait_for_function()` which polls the
predicate inside the browser, eliminating Python↔JS round-trip
overhead. Browser-side state lives on `window.__trawl_ready` across
polls.

Predicate pseudocode (exact JS in the implementation plan):

```js
(profileSelector) => {
  const s = window.__trawl_ready ??= { lastLen: 0, stableTicks: 0 };
  const len = document.body.innerText.length;
  const textStable = len === s.lastLen && len > 100;
  s.lastLen = len;
  s.stableTicks = textStable ? s.stableTicks + 1 : 0;

  let selOk = true;
  if (profileSelector) {
    const el = document.querySelector(profileSelector);
    if (!el) return false;
    const t = el.innerText.trim();
    const placeholder = /^(—+|---+|\.{3,}|loading)$/i;
    if (t.length < 50 || placeholder.test(t)) selOk = false;
  }

  return s.stableTicks >= 4 && selOk;
}
```

Parameters:
- `polling=150` ms — fine-grained enough to catch the gap between
  second-wave XHRs; coarse enough not to thrash
- `stableTicks >= 4` → 600 ms cumulative stability requirement
- `len > 100` — filters out empty shells / "Loading…" initial states
- placeholder regex: `—`, `---`, `...`, `loading` (case-insensitive)

### Call-site changes

Three existing `pipeline.py` call sites (previously identified):

- `pipeline.py:398` — profile fast path (`render_session`)
- `pipeline.py:475` — profile transfer path (`render_session`)
- `pipeline.py:715` — full pipeline (`playwright.fetch`)

Each gains `profile_selector=(profile.mapper.main_selector if profile else None)`.

### Semantics of `wait_for_ms`

Today: fixed additional wait after `networkidle`.
After: maximum wait ceiling — content-ready may return much sooner.

Default stays `5000` (ms). Worst-case behavior is preserved.

`CLAUDE.md`'s "Things NOT to change" table must be updated on adoption
to reflect the new semantics.

## Data flow

```
page.goto(..., wait_until="networkidle")      # unchanged
      │
      ▼
_wait_for_content_ready(                      # NEW — replaces wait_for_timeout
    page,
    profile_selector=<from pipeline>,
    max_wait_ms=wait_for_ms,
)
      │   on ready: ~200–2500 ms (measured)
      │   on timeout: 5000 ms (= old behavior)
      ▼
html = page.content()                          # unchanged
```

## Error Handling

- `PlaywrightTimeoutError` from `wait_for_function` → swallowed,
  proceed with current HTML. This matches the current behavior's
  worst case.
- `page.evaluate` failure mid-poll (page crashed, navigation) →
  surfaced as ordinary Playwright exception and caught by the
  existing `fetch()` outer `except Exception` handler → returns
  `FetchResult` with error. No new failure mode.
- Empty `profile_selector` string → treated as "no profile" (regex
  `if (sel)` guard in JS). The pipeline passes `None` explicitly
  but a defensive empty-string check protects against config drift.

## Testing Plan

Primary: existing parity matrix — `python tests/test_pipeline.py`.
12 / 12 must stay green.

Additional smoke test during implementation:

```bash
# Sanity: page that finishes fast should exit well under 5 s
python -c "
from trawl.fetchers import playwright as pw
import time
t0 = time.monotonic()
r = pw.fetch('https://example.com/')
print('ok:', r.ok, 'ms:', int((time.monotonic() - t0) * 1000), 'len:', len(r.html))
assert r.ok
"
```
Expected: `ms` well below 5000 (likely 1000–2000), content intact.

Comparative measurement: 3 × baseline + 3 × new, median per case, as
described in Success Criteria.

## Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Some SPA fires a tiny XHR every 300 ms forever (live scores, chat) — text-stability never stabilizes within 600 ms | Hard ceiling at `wait_for_ms=5000` preserves worst-case behavior. Case would be flagged in parity matrix if content is incomplete at timeout. |
| Profile selector has very short legitimate content (e.g. a score cell with "3-0") that trips the `len < 50` filter | Predicate requires *both* text stability AND selector OK; the text-stability leg will still fire independently once the rest of the page is done. Worst case: 600 ms of extra wait on such pages, not a parity regression. |
| Placeholder regex misses a site-specific marker (e.g. "Please wait…") | Acceptable — hard ceiling catches it. Widening the regex is a tuning step in the Retune outcome. |
| `window.__trawl_ready` collides with a site's own global | Highly unlikely (double-underscore prefix + specific name). No mitigation planned for the spike; if observed, rename to `window.__trawl_ready_v1` with brief note. |
| `networkidle` times out and falls back to `domcontentloaded` (existing behavior) — is content-ready still safe? | Yes. `_wait_for_content_ready` runs after whichever `goto` branch succeeded; nothing in the predicate assumes a specific wait-until state. |

## Deliverables

1. Single code change: `fetchers/playwright.py` + three call-sites in
   `pipeline.py`
2. `docs/superpowers/specs/2026-04-17-content-ready-wait-design.md`
   (this document)
3. `docs/superpowers/plans/2026-04-17-content-ready-wait.md`
   (implementation plan — next step)
4. `docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md`
   (final decision per success criteria)
5. If Adopt: `CLAUDE.md` "Things NOT to change" table update + follow-up
   PR to `main`

## Open Questions

None blocking. These will be resolved empirically by the spike:
- Is 600 ms stability window too tight for pages with staggered
  image/iframe loads?
- Is `len > 100` too strict for very-short-page (example.com has
  ~70 chars of body text)? If it breaks that case, drop to `len > 30`.
