# MutationObserver + LCP Content-Ready Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repo's convention (see auto-memory `feedback_subagent_scope.md`) is to run code changes and reviews from the main agent; the subagent-driven-development pattern is **not** used here.

**Goal:** Rewrite `fetchers/playwright.py:_wait_for_content_ready` from polling-based text-length diffing to an event-driven `MutationObserver` + `PerformanceObserver(largest-contentful-paint)` hybrid, exiting on `(domQuiet OR lcpFiredAndQuiet) AND selOk`, bounded by the existing `wait_for_ms` ceiling.

**Architecture:** Single-file change. `_wait_for_content_ready` still takes `(page, profile_selector, max_wait_ms)` and still swallows `PlaywrightTimeoutError`. The body changes: one `page.evaluate()` installs a `MutationObserver` (updating `lastMutationAt`) and a `PerformanceObserver({type:'largest-contentful-paint', buffered:true})` (flipping `lcpFired`), then returns a `Promise` that resolves when `(now − lastMutationAt ≥ QUIET_MS) OR (lcpFired AND now − lastMutationAt ≥ LCP_POST_PAINT_MS)`, gated by the existing `selOk` placeholder check. Playwright auto-awaits the returned promise.

**Tech Stack:** Python 3.10+, Playwright (`page.evaluate` with async promise return), existing trawl fetcher. No new dependencies. All browser-side state lives on `window.__trawl_ready_v2` — renamed to sidestep any residual `__trawl_ready` state from the previous spike on reused contexts.

**Design doc:** `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-design.md`

**Branch:** `spike/mutation-lcp-wait` (off `develop` @ `27a52bb`)

---

## File Structure

**Files modified (no new files):**
- `src/trawl/fetchers/playwright.py` — rewrite `_wait_for_content_ready` body; add two module-level constants (`QUIET_MS`, `LCP_POST_PAINT_MS`); rename the browser-side global.

**Files read (not modified):**
- `tests/test_pipeline.py` + `tests/test_cases.yaml` — parity gate + source of baseline `fetch_ms`
- `~/.trawl/telemetry.jsonl` — NVIDIA forum URL(s) for the regression smoke test

**Artifacts produced (committed):**
- `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-conclusion.md` — final Adopt/Retune/Reject with measured table

**Artifacts NOT committed:**
- `tests/results/<ts>/` — gitignored per existing convention
- `/tmp/trawl-baseline-*.log`, `/tmp/trawl-spike-*.log` — run logs

---

## Task 1: Create the spike branch and capture baseline

The spike starts from `develop @ 27a52bb` (design doc only, no code changes yet). Running the parity matrix at this head is the baseline we compare against.

**Files:** None.

- [ ] **Step 1: Confirm clean state and create the branch**

```bash
cd /Users/lyla/workspaces/trawl
git status
git log --oneline -3
```
Expected: working tree clean, HEAD is `27a52bb docs: MutationObserver + LCP content-ready 스파이크 설계 문서`. If not, stop.

```bash
git checkout -b spike/mutation-lcp-wait
```

- [ ] **Step 2: Run parity matrix three times, capturing log paths**

```bash
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-1.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-2.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-3.log 2>&1
```

- [ ] **Step 3: Verify all three runs are 12/12 PASS**

```bash
tail -n 3 /tmp/trawl-baseline-1.log /tmp/trawl-baseline-2.log /tmp/trawl-baseline-3.log
```
Expected: `Total: 12/12 cases pass.` on each. If not, stop — baseline is unreliable.

- [ ] **Step 4: Capture the three summary.json paths**

```bash
ls -1dt tests/results/20*/ | head -3
```
Save these three paths to a scratch note outside the repo (the `tests/results/` directory is gitignored). They are the baseline data for Task 5.

- [ ] **Step 5: NVIDIA forum cold-run baseline**

```bash
mamba run -n trawl python - <<'EOF'
import time
from trawl.fetchers import playwright as pwf
url = 'https://forums.developer.nvidia.com/t/moving-ridgebackfranka-robot-with-articulations-does-not-change-the-pose-of-the-robot/242112'
for i in range(3):
    t0 = time.monotonic()
    r = pwf.fetch(url)
    print(f'run {i+1}: elapsed_ms={r.elapsed_ms} ok={r.ok} html_len={len(r.html)}')
EOF
```
Expected: ~4.4 s per run with `html_len=1468438`. Save the three numbers as the "Discourse regression baseline" reference.

- [ ] **Step 6: No commit for this task**

Baseline measurement produces no tracked files.

---

## Task 2: Rewrite `_wait_for_content_ready` with MutationObserver + LCP

**Files:**
- Modify: `src/trawl/fetchers/playwright.py` (body of `_wait_for_content_ready`, plus two new module constants)

- [ ] **Step 1: Add two module-level constants**

Insert immediately after the existing `NETWORKIDLE_BUDGET_MS = 3000` line (currently line 143):

```python
# DOM quiescence window: time with no MutationObserver callbacks before
# the page is considered stable. Tighter than the previous 600 ms
# polling floor; LCP leg below is the rescue when this is too aggressive
# on tail-loading pages.
QUIET_MS = 400

# Grace period after the `largest-contentful-paint` entry fires before
# we early-exit on the LCP leg. Short dwell lets the LCP element's
# siblings settle without waiting for unrelated tail mutations.
LCP_POST_PAINT_MS = 200
```

- [ ] **Step 2: Replace the `_wait_for_content_ready` body**

Find the existing function (currently lines 146–184). Replace its body (keep the signature and the docstring's first paragraph) with the event-driven version:

```python
def _wait_for_content_ready(
    page: Page, *, profile_selector: str | None, max_wait_ms: int
) -> None:
    """Block until the page is content-ready or `max_wait_ms` elapses.

    Readiness is `(domQuiet OR lcpFiredAndQuiet) AND selOk`, where:
      - domQuiet: no MutationObserver callback for `QUIET_MS`
      - lcpFiredAndQuiet: `largest-contentful-paint` fired AND no
        mutation for `LCP_POST_PAINT_MS`
      - selOk: if `profile_selector` is given, that element exists and
        holds non-placeholder text (≥ 50 chars, not `—`/`---`/`...`/
        `loading`); otherwise always true.

    All browser-side state lives on `window.__trawl_ready_v2`, isolated
    from the previous spike's `__trawl_ready` so reused contexts don't
    leak stale data. On timeout, swallow the error and return so the
    caller reads whatever HTML is present.
    """
    script = """
    ([sel, quietMs, lcpGraceMs, maxMs]) => new Promise((resolve) => {
        const state = window.__trawl_ready_v2 = {
            lcpFired: false,
            lastMutationAt: performance.now(),
        };

        const selOk = () => {
            if (!sel) return true;
            const el = document.querySelector(sel);
            if (!el) return false;
            const t = (el.innerText || '').trim();
            if (t.length < 50) return false;
            return !/^(—+|---+|\\.{3,}|loading)$/i.test(t);
        };

        let mo = null;
        try {
            mo = new MutationObserver(() => {
                state.lastMutationAt = performance.now();
            });
            mo.observe(document.body, {
                subtree: true,
                childList: true,
                characterData: true,
                attributes: false,
            });
        } catch (_) { /* no document.body yet */ }

        let po = null;
        try {
            po = new PerformanceObserver((entries) => {
                if (entries.getEntries().length) state.lcpFired = true;
            });
            po.observe({ type: 'largest-contentful-paint', buffered: true });
        } catch (_) { /* LCP unsupported — DOM leg still works */ }

        const start = performance.now();
        const done = (ok) => {
            if (mo) mo.disconnect();
            if (po) po.disconnect();
            resolve(ok);
        };

        const tick = () => {
            const now = performance.now();
            const quietFor = now - state.lastMutationAt;
            const bodyLen = document.body ? document.body.innerText.length : 0;
            const domStable = quietFor >= quietMs && bodyLen > 100;
            const lcpReady = state.lcpFired && quietFor >= lcpGraceMs;
            if ((domStable || lcpReady) && selOk()) return done(true);
            if (now - start >= maxMs) return done(false);
            requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    })
    """
    try:
        page.evaluate(
            script,
            arg=[profile_selector, QUIET_MS, LCP_POST_PAINT_MS, max_wait_ms],
        )
    except PlaywrightTimeoutError:
        pass
```

Notes on the change:

- The outer function no longer uses `wait_for_function`; `page.evaluate` of an async arrow returns a `Promise` and Playwright auto-awaits it.
- `requestAnimationFrame(tick)` replaces the fixed 150 ms polling, so the exit check runs on paint frames — typically 16 ms cadence — at negligible CPU cost.
- `quietMs` / `lcpGraceMs` / `maxMs` are passed in as arguments so the constants stay Python-side and are easy to tune without editing JS.
- `PlaywrightTimeoutError` catch is defensive (evaluate itself does not time out based on the inner promise, but we preserve the existing swallow contract in case Playwright's defaults change).

- [ ] **Step 3: Verify the module still imports cleanly**

```bash
mamba run -n trawl python -c "from trawl.fetchers import playwright; print('ok')"
```
Expected: `ok`.

- [ ] **Step 4: Fast-page smoke test (example.com)**

```bash
mamba run -n trawl python -c "
from trawl.fetchers import playwright as pw
import time
t0 = time.monotonic()
r = pw.fetch('https://example.com/')
ms = int((time.monotonic() - t0) * 1000)
print(f'ok={r.ok} elapsed_ms={ms} html_len={len(r.html)}')
assert r.ok, r.error
assert 'Example Domain' in r.html
"
```
Expected: `ok=True`, `elapsed_ms` likely < 2000 (was ~1500 ms baseline; LCP leg may not fire on a trivially small page but DOM-stable leg should exit quickly). `html_len` 1256 or similar.

If the smoke test hangs to the 5 s ceiling, that is still acceptable (same worst case as today) — the parity-matrix numbers in Task 5 are the real decision data.

- [ ] **Step 5: Verify `PerformanceObserver` supports LCP inside the stealth context**

```bash
mamba run -n trawl python - <<'EOF'
from trawl.fetchers import playwright as pw
with pw.render_session('https://example.com/', wait_for_ms=0) as s:
    types = s.page.evaluate('() => PerformanceObserver.supportedEntryTypes')
    print('supported:', types)
    print('lcp supported:', 'largest-contentful-paint' in types)
EOF
```
Expected: `lcp supported: True`. If False, record it in the conclusion's Open Questions — the LCP leg is silently disabled and the DOM-stable leg still drives exit; parity should still pass, but the expected speedup shrinks.

- [ ] **Step 6: NVIDIA forum regression smoke**

```bash
mamba run -n trawl python - <<'EOF'
import time
from trawl.fetchers import playwright as pwf
url = 'https://forums.developer.nvidia.com/t/moving-ridgebackfranka-robot-with-articulations-does-not-change-the-pose-of-the-robot/242112'
for i in range(3):
    t0 = time.monotonic()
    r = pwf.fetch(url)
    print(f'run {i+1}: elapsed_ms={r.elapsed_ms} ok={r.ok}')
EOF
```
Expected: each run ≤ 5500 ms. The `bad9cce` baseline was ~4.4 s; some tail-mutation chattiness on Discourse may push slightly higher, but a result above 7 s is a red flag — stop and diagnose before Task 3.

- [ ] **Step 7: Commit**

```bash
git add src/trawl/fetchers/playwright.py
git commit -m "feat(playwright): MutationObserver + LCP content-ready detector"
```

---

## Task 3: Run parity matrix on the spike branch

**Files:** None modified; produces measurement data.

- [ ] **Step 1: Run three parity rounds**

```bash
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-1.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-2.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-3.log 2>&1
```

- [ ] **Step 2: Verify all three are 12/12 PASS**

```bash
tail -n 3 /tmp/trawl-spike-1.log /tmp/trawl-spike-2.log /tmp/trawl-spike-3.log
```
Expected: `Total: 12/12 cases pass.` on each.

If any case fails, classify:
- `very_short_page` fails → `len > 100` floor kicked in as expected and the 5 s ceiling didn't save it: either case-specific tuning or raise a concern. Do NOT commit a fudge fix to `test_cases.yaml` (CLAUDE.md hard rule).
- `pricing_page_ko` fails → `QUIET_MS=400` is likely too tight for the BS-served pricing table. First retune step is `QUIET_MS=500`, re-measure.
- Unrelated flake → retry the single case with `--only <id> --verbose`; if reproducible it is not flake.

Only proceed to Task 4 if all three runs are green.

- [ ] **Step 3: Capture the three new summary.json paths**

```bash
ls -1dt tests/results/20*/ | head -3
```
Save these three paths.

- [ ] **Step 4: No commit for this task**

---

## Task 4: MCP smoke check

**Files:** None modified.

- [ ] **Step 1: Run MCP stdio smoke**

```bash
mamba run -n trawl python tests/test_mcp_server.py 2>&1 | tail -15
```
Expected: `OK: trawl-mcp stdio server smoke test passed.`

If the MCP test fails, the change has side-effected the public fetch path. Do not proceed to Task 5 — diagnose first.

- [ ] **Step 2: No commit for this task**

---

## Task 5: Compute baseline-vs-spike table

**Files:** None modified; produces the numbers for Task 6's conclusion.

- [ ] **Step 1: Compute per-case median `fetch_ms` for the six playwright-path cases**

Paste the three baseline paths (Task 1 Step 4) and three spike paths (Task 3 Step 3) into the snippet:

```bash
mamba run -n trawl python - <<'EOF'
import json, statistics
from pathlib import Path

BASELINE = [
    # three baseline paths from Task 1 Step 4
]
SPIKE = [
    # three spike paths from Task 3 Step 3
]

PLAYWRIGHT_PATH_IDS = {
    "kbo_schedule", "korean_news_ranking", "pricing_page_ko",
    "english_tech_docs", "blog_post_no_heading", "very_short_page",
}

def collect(paths):
    by_case = {}
    for p in paths:
        rows = json.loads(Path(p, "summary.json").read_text())["rows"]
        for r in rows:
            by_case.setdefault(r["id"], []).append(r["result"]["fetch_ms"])
    return {cid: {"median": statistics.median(ms),
                   "p90": statistics.quantiles(ms, n=10)[-1] if len(ms) >= 3 else max(ms)}
            for cid, ms in by_case.items()}

base = collect(BASELINE)
new  = collect(SPIKE)

print(f"{'case':<24} {'base_med':>9} {'new_med':>9} {'Δ':>8} {'pct':>7}  {'base_p90':>9} {'new_p90':>9}")
print("-" * 80)
b_tot = s_tot = 0
n = 0
for cid in sorted(set(base) | set(new)):
    if cid not in PLAYWRIGHT_PATH_IDS:
        continue
    b = base.get(cid, {}).get("median", 0)
    s = new.get(cid,  {}).get("median", 0)
    bp = base.get(cid, {}).get("p90", 0)
    sp = new.get(cid,  {}).get("p90", 0)
    pct = ((s - b) / b * 100) if b else 0
    print(f"{cid:<24} {b:>9.0f} {s:>9.0f} {s-b:>+8.0f} {pct:>+6.1f}%  {bp:>9.0f} {sp:>9.0f}")
    b_tot += b; s_tot += s; n += 1

print("-" * 80)
avg_pct = ((s_tot - b_tot) / b_tot * 100) if b_tot else 0
print(f"{'AVG (playwright-path)':<24} {b_tot/n:>9.0f} {s_tot/n:>9.0f} "
      f"{(s_tot - b_tot)/n:>+8.0f} {avg_pct:>+6.1f}%")
EOF
```

Save the full stdout — this is the data for the conclusion doc.

- [ ] **Step 2: No commit for this task**

---

## Task 6: Write conclusion doc applying the decision matrix

**Files:**
- Create: `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-conclusion.md`

- [ ] **Step 1: Create the conclusion file**

Template (replace every `<...>` with the numbers from Task 5):

```markdown
# MutationObserver + LCP Content-Ready Spike — Conclusion

**Date:** 2026-04-17
**Branch:** `spike/mutation-lcp-wait`
**Design:** `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-design.md`
**Plan:** `docs/superpowers/plans/2026-04-17-mutation-lcp-wait.md`

## Result

**Decision:** <ADOPT | RETUNE | REJECT>

## Numbers (playwright-path cases, median of 3 runs)

| case | base median | new median | Δ | pct | base p90 | new p90 |
|---|---|---|---|---|---|---|
| kbo_schedule | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| korean_news_ranking | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| pricing_page_ko | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| english_tech_docs | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| blog_post_no_heading | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| very_short_page | <b> | <s> | <Δ> | <pct>% | <bp> | <sp> |
| **AVG** | <b_avg> | <s_avg> | <Δ_avg> | **<pct_avg>%** | — | — |

Parity: baseline 12/12 PASS, new 12/12 PASS.

## NVIDIA forum regression smoke (3 × cold)

| run | baseline (`bad9cce`) | spike |
|---|---|---|
| 1 | <b1> ms | <s1> ms |
| 2 | <b2> ms | <s2> ms |
| 3 | <b3> ms | <s3> ms |
| median | <bm> ms | <sm> ms |

HTML length identical across runs: <N> bytes.

## LCP support in stealth context

`PerformanceObserver.supportedEntryTypes` inside the Stealth-patched
browser includes `largest-contentful-paint`: <yes | no>.

## Decision Reasoning

<Apply the success criteria from the design doc:

| Threshold | Observed | Met? |
|---|---|---|
| Parity 12/12 | <yes/no> | <yes/no> |
| Avg median reduction ≥ 15% (Adopt) | <X%> | <yes/no> |
| NVIDIA forum ≤ 4.4 s baseline | <Xs> | <yes/no> |

Short paragraph (2–4 sentences) on why the decision follows. If any
case regressed, explain whether the LCP rescue leg was expected to
kick in and why it didn't.>

## Failure / Oddities

<One bullet per unexpected behaviour. If none, write:
"No oddities — predicate fired on all six cases within the expected
window; LCP leg contributed <N of 6> early-exits." If Decision =
RETUNE or REJECT, classify each miss:
- "QUIET_MS=400 too tight on <case>; raising to 500 would recover"
- "LCP leg never fired on <case> (non-Blink path? CSP block?)"
- "selOk gated too aggressively on <case>, forcing ceiling fallback"
etc.>

## Next Steps

<If ADOPT:
- Update `CLAUDE.md` "Things NOT to change" table with rows for
  `QUIET_MS` and `LCP_POST_PAINT_MS`, plus note that the predicate
  is now event-driven (no polling interval to tune).
- Remove the `polling=150` / `stableTicks >= 4` row since the
  implementation no longer uses those constants.
- Open PR from `spike/mutation-lcp-wait` to `main` via
  `gh pr merge --admin` (solo-dev workflow, see auto-memory
  `feedback_merge_workflow.md`).

If RETUNE:
- One round: raise `QUIET_MS` to 500 and/or `LCP_POST_PAINT_MS` to
  300; re-run Task 3 and Task 5; if still < 15%, REJECT.

If REJECT:
- Keep branch for reference, no further work; the `bad9cce`
  networkidle cap stands as the current best state.>
```

- [ ] **Step 2: Commit the conclusion**

```bash
git add docs/superpowers/specs/2026-04-17-mutation-lcp-wait-conclusion.md
git commit -m "docs: MutationObserver + LCP 스파이크 결론"
```

- [ ] **Step 3: Report the decision to the user**

Show the user:
- The decision (ADOPT / RETUNE / REJECT)
- Avg median `fetch_ms` reduction percentage and the NVIDIA-forum regression line
- Link to the conclusion file

---

## Task 7 (conditional — ADOPT only): CLAUDE.md update + PR to main

Execute only if Task 6's decision is ADOPT.

**Files:**
- Modify: `CLAUDE.md` ("Things NOT to change" table — two new rows, one edited row)

- [ ] **Step 1: Update the load-bearing-values table**

Find the current content-ready predicate row:

```markdown
| `fetchers/playwright.py` content-ready predicate | `stableTicks >= 4`, `polling=150ms`, `len > 100`, placeholder regex | Empirically tuned on the 12-case parity matrix for a 67% avg fetch_ms reduction. Tightening the window or raising `len` can regress fast/short pages. |
```

Replace with:

```markdown
| `fetchers/playwright.py QUIET_MS` | `400` | MutationObserver quiet-window before DOM is considered stable. Raising helps pages with staggered XHR waves; lowering risks premature exit on SPAs with a second XHR burst. Empirically validated on the 12-case parity matrix. |
| `fetchers/playwright.py LCP_POST_PAINT_MS` | `200` | Grace period after `largest-contentful-paint` fires before early-exit. Short dwell lets LCP siblings settle without waiting for unrelated tail mutations. |
| `fetchers/playwright.py` content-ready predicate | `len > 100`, placeholder regex, requestAnimationFrame driven | Event-driven via MutationObserver + PerformanceObserver; no polling interval. `len > 100` still filters empty shells. Placeholder regex (`—`/`---`/`...`/`loading`) gates `profile_selector` readiness. |
```

- [ ] **Step 2: Commit the CLAUDE.md change**

```bash
git add CLAUDE.md
git commit -m "docs(claude-md): MutationObserver+LCP 상수 추가, predicate 엔트리 업데이트"
```

- [ ] **Step 3: Push branch and open PR to main**

Per `feedback_merge_workflow.md`, merge via `gh pr merge --admin` after PR review, and per `feedback_commit_coauthor.md`, no `Co-Authored-By` trailer.

```bash
git push -u origin spike/mutation-lcp-wait
gh pr create --base main --head spike/mutation-lcp-wait \
  --title "feat(playwright): MutationObserver + LCP content-ready detector" \
  --body "$(cat <<'EOF'
## Summary

- Replaces the polling-based content-ready predicate (`wait_for_function`
  + `stableTicks >= 4`, 150 ms polling) with an event-driven
  `MutationObserver` + `PerformanceObserver(largest-contentful-paint)`
  hybrid.
- Exits on `(domQuiet OR lcpFiredAndQuiet) AND selOk`, bounded by the
  existing `wait_for_ms=5000` ceiling.
- `QUIET_MS=400` / `LCP_POST_PAINT_MS=200` module-level constants;
  registered in `CLAUDE.md` "Things NOT to change".
- `window.__trawl_ready_v2` isolates browser-side state from the
  previous spike's `__trawl_ready` on reused contexts.

Spec:       `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-design.md`
Plan:       `docs/superpowers/plans/2026-04-17-mutation-lcp-wait.md`
Conclusion: `docs/superpowers/specs/2026-04-17-mutation-lcp-wait-conclusion.md`

## Test plan

- [x] Parity matrix: 12/12 PASS (3 runs, spike branch)
- [x] MCP stdio smoke: pass
- [x] Playwright-path median `fetch_ms` vs `bad9cce` baseline recorded
  in the conclusion doc (≥ 15 % reduction met for ADOPT)
- [x] NVIDIA forum cold-run regression: ≤ `bad9cce` baseline (~4.4 s)
- [ ] Smoke check post-merge: `python tests/test_pipeline.py` from a
  fresh `main` checkout
EOF
)"
```

Report the resulting PR URL to the user; do not auto-merge — wait for explicit approval per `feedback_merge_workflow.md`.
