# Content-Ready Wait Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repo's convention (see auto-memory `feedback_subagent_scope.md`) is to run code changes and reviews from the main agent; the subagent-driven-development pattern is **not** used here.

**Goal:** Replace `fetchers/playwright.py:_open_context`'s fixed 5 s wait with a content-ready detector that returns as soon as the page's visible text is stable — and, when a profile is available, the profile's main selector holds non-placeholder content. Keep `wait_for_ms` as the hard ceiling so worst-case behavior is identical.

**Architecture:** One new helper `_wait_for_content_ready()` in `fetchers/playwright.py` uses `page.wait_for_function()` to poll a JS predicate inside the browser. The predicate tracks `document.body.innerText.length` stability (600 ms window, 150 ms polling) and optionally inspects a caller-provided `profile_selector` for non-placeholder content. Callers thread the profile selector through `_open_context` → `fetch()` / `render_session()`; `pipeline.py` passes `profile.mapper.main_selector` when a profile is loaded.

**Tech Stack:** Python 3.10+, Playwright (`page.wait_for_function`), existing trawl fetcher + pipeline modules. No new dependencies.

**Design doc:** `docs/superpowers/specs/2026-04-17-content-ready-wait-design.md`

**Branch:** `spike/content-ready-wait` (off `develop` @ `e275f29`)

---

## File Structure

**Files modified (no new files):**
- `src/trawl/fetchers/playwright.py` — add `_wait_for_content_ready()`, replace the fixed `wait_for_timeout` call, add `profile_selector` kwarg to `_open_context` / `fetch()` / `render_session()`
- `src/trawl/pipeline.py` — pass `profile.mapper.main_selector` at three existing call sites (lines 398, 475, 715 approx)

**Files read (not modified):**
- `tests/test_pipeline.py` + `tests/test_cases.yaml` — parity gate + source of baseline fetch_ms
- `src/trawl/profiles/profile.py` — schema for `profile.mapper.main_selector`

**Artifacts produced (committed):**
- `docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md` — final Adopt/Retune/Reject
- Measurement notes inline in the conclusion (no separate benchmark results directory; the parity-matrix summary.json is the data source)

**Artifacts NOT committed:**
- `tests/results/<ts>/` — gitignored per existing convention

---

## Task 1: Capture baseline fetch_ms on spike branch (pre-change)

The spike branch currently has only the design doc committed — no code changes. Running the parity matrix here measures today's behavior, which is the baseline we compare against.

**Files:** None (just runs the existing parity runner).

- [ ] **Step 1: Confirm clean state**

Run:
```bash
cd /Users/lyla/workspaces/trawl
git status
git log --oneline -3
```
Expected: working tree clean, HEAD is `f00abcc docs: content-ready wait 스파이크 설계 문서`, parent is `e275f29`. If not, stop and investigate before proceeding.

- [ ] **Step 2: Run parity matrix, three times, capturing summary paths**

```bash
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-1.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-2.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-baseline-3.log 2>&1
```

Each run prints `Full results: tests/results/<timestamp>` at the end. Extract the three timestamps into a shell variable for the next step.

- [ ] **Step 3: Verify all three runs are 12/12 PASS**

```bash
tail -n 3 /tmp/trawl-baseline-1.log /tmp/trawl-baseline-2.log /tmp/trawl-baseline-3.log
```
Expected: the printed summary lines show `12 / 12 passed` (or the runner's equivalent phrasing — the script exits 0 on full pass). If any run has a fail, stop — the baseline itself is unreliable.

- [ ] **Step 4: Capture the three summary.json paths**

```bash
ls -1dt tests/results/20*/ | head -3
```
Save these three paths; they are the baseline data for Task 6's median calculation. Do NOT commit `tests/results/` (gitignored).

- [ ] **Step 5: No commit for this task**

Baseline measurement produces no code changes. Just mental-note the three paths (or save them to a scratch file outside the repo).

---

## Task 2: Add `_wait_for_content_ready()` helper

**Files:**
- Modify: `src/trawl/fetchers/playwright.py` (add helper function before `_open_context`)

- [ ] **Step 1: Add the helper**

Insert this function in `src/trawl/fetchers/playwright.py`, immediately **before** the `_open_context` definition (around current line 140):

```python
def _wait_for_content_ready(
    page: Page, *, profile_selector: str | None, max_wait_ms: int
) -> None:
    """Block until the page's visible text is stable and — when a
    profile selector is provided — that selector's content is no
    longer a placeholder. On timeout, swallow the error and return so
    the caller reads whatever HTML is present. Worst-case behavior
    matches the old fixed `wait_for_timeout`.

    Polls inside the browser via `page.wait_for_function` to avoid
    Python↔JS round trips on every tick.
    """
    predicate = """(sel) => {
        const s = window.__trawl_ready ??= { lastLen: 0, stableTicks: 0 };
        const len = document.body.innerText.length;
        const textStable = len === s.lastLen && len > 100;
        s.lastLen = len;
        s.stableTicks = textStable ? s.stableTicks + 1 : 0;

        let selOk = true;
        if (sel) {
            const el = document.querySelector(sel);
            if (!el) return false;
            const t = el.innerText.trim();
            const placeholder = /^(—+|---+|\\.{3,}|loading)$/i;
            if (t.length < 50 || placeholder.test(t)) selOk = false;
        }

        return s.stableTicks >= 4 && selOk;
    }"""
    try:
        page.wait_for_function(
            predicate,
            arg=profile_selector,
            timeout=max_wait_ms,
            polling=150,
        )
    except PlaywrightTimeoutError:
        pass
```

- [ ] **Step 2: Verify the module still imports cleanly**

Run:
```bash
mamba run -n trawl python -c "from trawl.fetchers import playwright; print('ok')"
```
Expected: `ok`. The function is unused at this point — just wired in the module.

- [ ] **Step 3: Commit**

```bash
git add src/trawl/fetchers/playwright.py
git commit -m "feat(playwright): content-ready wait helper"
```

---

## Task 3: Wire helper into `_open_context` and add `profile_selector` kwarg

**Files:**
- Modify: `src/trawl/fetchers/playwright.py` (`_open_context` signature + body)

- [ ] **Step 1: Update `_open_context` signature**

Locate `_open_context` (currently around line 140). Change its signature from:

```python
@contextmanager
def _open_context(
    url: str,
    *,
    wait_for_ms: int,
    timeout_s: float,
    user_agent: str | None,
) -> Iterator[tuple[BrowserContext, Page, str, str | None]]:
```

to:

```python
@contextmanager
def _open_context(
    url: str,
    *,
    wait_for_ms: int,
    timeout_s: float,
    user_agent: str | None,
    profile_selector: str | None = None,
) -> Iterator[tuple[BrowserContext, Page, str, str | None]]:
```

- [ ] **Step 2: Replace the fixed wait with the helper call**

In the same function, find these lines (currently 173–174):

```python
        if wait_for_ms > 0:
            page.wait_for_timeout(wait_for_ms)
```

Replace them with:

```python
        if wait_for_ms > 0:
            _wait_for_content_ready(
                page, profile_selector=profile_selector, max_wait_ms=wait_for_ms
            )
```

- [ ] **Step 3: Update the `_open_context` docstring**

Append a note to the existing docstring explaining the new kwarg. Replace:

```python
    """Internal helper: open a stealth BrowserContext, navigate to `url`,
    yield (context, page, html, content_type). The context is closed in this
    generator's finally block when the caller exits the `with` block.

    Uses `networkidle` with half the total timeout, falling back to
    `domcontentloaded` on PlaywrightTimeoutError so Cloudflare-protected
    long-polling sites still complete navigation.
    """
```

with:

```python
    """Internal helper: open a stealth BrowserContext, navigate to `url`,
    yield (context, page, html, content_type). The context is closed in this
    generator's finally block when the caller exits the `with` block.

    Uses `networkidle` with half the total timeout, falling back to
    `domcontentloaded` on PlaywrightTimeoutError so Cloudflare-protected
    long-polling sites still complete navigation.

    After navigation, `_wait_for_content_ready` watches for text-content
    stability (and `profile_selector` population when provided) with
    `wait_for_ms` as a hard ceiling. This replaces the old fixed
    `wait_for_timeout(wait_for_ms)` so fast pages return sub-second.
    """
```

- [ ] **Step 4: Smoke-test end-to-end fetch against example.com**

Run:
```bash
mamba run -n trawl python -c "
from trawl.fetchers import playwright as pw
import time
t0 = time.monotonic()
r = pw.fetch('https://example.com/')
elapsed_ms = int((time.monotonic() - t0) * 1000)
print('ok:', r.ok, 'elapsed_ms:', elapsed_ms, 'html_len:', len(r.html))
assert r.ok, r.error
assert 'Example Domain' in r.html
"
```
Expected: `ok: True`. `elapsed_ms` likely 1500–5500 ms (example.com is short enough that text may not hit the `len > 100` bar, falling back to the 5 s ceiling — that's acceptable, see spec Open Questions). No exception.

- [ ] **Step 5: Commit**

```bash
git add src/trawl/fetchers/playwright.py
git commit -m "feat(playwright): replace fixed wait with content-ready detector"
```

---

## Task 4: Add `profile_selector` kwarg to `fetch()` and `render_session()`

**Files:**
- Modify: `src/trawl/fetchers/playwright.py` (public API wrappers)

- [ ] **Step 1: Update `fetch()` signature and body**

Locate `fetch()` (currently around line 190). Change its signature from:

```python
def fetch(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
) -> FetchResult:
```

to:

```python
def fetch(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
    profile_selector: str | None = None,
) -> FetchResult:
```

Then, inside the function, locate the `_open_context` call (around current line 205):

```python
            with _open_context(
                url,
                wait_for_ms=wait_for_ms,
                timeout_s=timeout_s,
                user_agent=user_agent,
            ) as (_ctx, _page, html, content_type):
```

Change to:

```python
            with _open_context(
                url,
                wait_for_ms=wait_for_ms,
                timeout_s=timeout_s,
                user_agent=user_agent,
                profile_selector=profile_selector,
            ) as (_ctx, _page, html, content_type):
```

- [ ] **Step 2: Update `render_session()` signature and body**

Locate `render_session()` (currently around line 242). Apply the same two changes: add `profile_selector: str | None = None` to the kwargs, and pass it through in the `_open_context` call inside the function.

Change signature from:

```python
@contextmanager
def render_session(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
) -> Iterator[RenderResult]:
```

to:

```python
@contextmanager
def render_session(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
    profile_selector: str | None = None,
) -> Iterator[RenderResult]:
```

And change the `_open_context` call inside (currently around line 260):

```python
        with _open_context(
            url,
            wait_for_ms=wait_for_ms,
            timeout_s=timeout_s,
            user_agent=user_agent,
        ) as (_ctx, page, html, _content_type):
```

to:

```python
        with _open_context(
            url,
            wait_for_ms=wait_for_ms,
            timeout_s=timeout_s,
            user_agent=user_agent,
            profile_selector=profile_selector,
        ) as (_ctx, page, html, _content_type):
```

- [ ] **Step 3: Smoke-test both wrappers still work with default (no selector)**

Run:
```bash
mamba run -n trawl python -c "
from trawl.fetchers import playwright as pw
r = pw.fetch('https://example.com/')
assert r.ok
print('fetch ok')
with pw.render_session('https://example.com/') as s:
    assert 'Example Domain' in s.html
print('render_session ok')
"
```
Expected: `fetch ok` then `render_session ok`. No exception.

- [ ] **Step 4: Commit**

```bash
git add src/trawl/fetchers/playwright.py
git commit -m "feat(playwright): profile_selector kwarg on fetch/render_session"
```

---

## Task 5: Thread `profile_selector` through `pipeline.py` call sites

Three call sites must pass `profile.mapper.main_selector` when a profile is in scope. Sites without a profile in scope (the fallback full pipeline) keep passing `None` implicitly.

**Files:**
- Modify: `src/trawl/pipeline.py` (three call sites)

- [ ] **Step 1: Find the three call sites and their enclosing function names**

Run:
```bash
mamba run -n trawl python -c "
import re
src = open('src/trawl/pipeline.py').read().splitlines()
for i, line in enumerate(src, 1):
    if 'render_session(url)' in line or 'playwright.fetch(url)' in line:
        # print the line and the surrounding def line to identify which function
        for j in range(i - 1, max(0, i - 50), -1):
            if src[j - 1].startswith('def '):
                print(f'{i:4}: {line.strip()}  <-- inside {src[j - 1].strip()}')
                break
"
```
Expected output: three hits, something like:
```
 398: with playwright.render_session(url) as r:  <-- inside def _profile_fast_path(...)
 475: with playwright.render_session(url) as r:  <-- inside def _profile_transfer_path(...)
 715: fetched = playwright.fetch(url)  <-- inside def _fetch_html(url) (or similar)
```
Note these three precise line numbers and enclosing function names for the next steps. **If the line numbers differ substantially from 398/475/715 after earlier refactors**, use the actual numbers from this step.

- [ ] **Step 2: Update the profile_fast_path call site (first hit, ~398)**

The enclosing function takes `profile: Profile` as a parameter. Change:

```python
    with playwright.render_session(url) as r:
```

to:

```python
    with playwright.render_session(
        url,
        profile_selector=profile.mapper.main_selector,
    ) as r:
```

- [ ] **Step 3: Update the profile_transfer_path call site (second hit, ~475)**

The enclosing function `_profile_transfer_path` transfers a donor profile — it has a `profile` or similarly named variable that holds the donor `Profile`. Identify the exact variable name from the function body (read the 20 lines above the call site). Typical naming is `profile` or `donor_profile`.

Change:

```python
    with playwright.render_session(url) as r:
```

to (assuming the local variable is `profile`):

```python
    with playwright.render_session(
        url,
        profile_selector=profile.mapper.main_selector,
    ) as r:
```

If the variable is named differently (e.g. `donor_profile`), use that name instead. Do NOT introduce a new variable — use what is already in scope.

- [ ] **Step 4: Update the `_fetch_html` call site (third hit, ~715)**

`_fetch_html(url)` takes no profile argument today; it is called from `_run_full_pipeline` which is invoked when the profile path falls through (i.e. no profile is loaded). At this site we pass `profile_selector=None` explicitly to make intent clear, or we rely on the default.

**Preferred: rely on the default.** Leave this call site unchanged — `playwright.fetch(url)` will pass `profile_selector=None` via the new default. Confirm no edit is needed by reading the line as-is and proceeding.

If reviewing later you feel the explicitness helps future readers, a zero-behavior change comment can be added:

```python
    # profile_selector stays None on this path — _run_full_pipeline is the
    # no-profile fallback; selector-aware waiting is handled by
    # _profile_fast_path / _profile_transfer_path above.
    fetched = playwright.fetch(url)
```

The comment is optional; leaving the line untouched is equally fine.

- [ ] **Step 5: Run the parity matrix to verify nothing broke**

```bash
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-verify.log 2>&1
tail -n 20 /tmp/trawl-spike-verify.log
```
Expected: 12 / 12 PASS. If any case fails, **do not commit**. The failure classifies as:
- Parity regression from the content-ready change → tune the predicate constants (see Risks in the spec) or revert Task 3
- Unrelated flake → retry once; if still failing, stop and investigate

- [ ] **Step 6: Commit**

```bash
git add src/trawl/pipeline.py
git commit -m "feat(pipeline): thread profile_selector into playwright fetchers"
```

---

## Task 6: Measure spike branch and compute comparison vs baseline

**Files:** None modified; produces measurement data for the conclusion doc.

- [ ] **Step 1: Run the parity matrix three times on the new branch state**

```bash
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-1.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-2.log 2>&1
mamba run -n trawl python tests/test_pipeline.py > /tmp/trawl-spike-3.log 2>&1
```

- [ ] **Step 2: Verify all three are 12/12 PASS**

```bash
tail -n 3 /tmp/trawl-spike-1.log /tmp/trawl-spike-2.log /tmp/trawl-spike-3.log
```
Expected: all three runs show the 12/12 summary line. A regression here means Task 5 Step 5 missed something — stop and diagnose.

- [ ] **Step 3: Capture the three new summary.json paths**

```bash
ls -1dt tests/results/20*/ | head -3
```
These are the post-change runs. Save the paths.

- [ ] **Step 4: Compute per-case median fetch_ms, baseline vs new**

Run this Python snippet, substituting the six paths (three baseline, three new) collected in Task 1 Step 4 and Task 6 Step 3:

```bash
mamba run -n trawl python - <<'EOF'
import json, statistics
from pathlib import Path

BASELINE = [
    # paste the three baseline paths from Task 1 Step 4 here, e.g.:
    # "tests/results/20260417-HHMMSS",
]
SPIKE = [
    # paste the three spike paths from Task 6 Step 3 here
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
    return {case_id: statistics.median(ms) for case_id, ms in by_case.items()}

base = collect(BASELINE)
new = collect(SPIKE)

print(f"{'case':<24} {'baseline':>10} {'new':>10} {'delta':>10} {'pct':>8}")
print("-" * 70)
base_total = new_total = 0
n = 0
for case_id in sorted(set(base) | set(new)):
    if case_id not in PLAYWRIGHT_PATH_IDS:
        continue
    b, s = base.get(case_id, 0), new.get(case_id, 0)
    pct = ((s - b) / b * 100) if b else 0
    print(f"{case_id:<24} {b:>10.0f} {s:>10.0f} {s - b:>+10.0f} {pct:>+7.1f}%")
    base_total += b
    new_total += s
    n += 1

print("-" * 70)
avg_pct = ((new_total - base_total) / base_total * 100) if base_total else 0
print(f"{'AVG (playwright-path)':<24} {base_total/n:>10.0f} {new_total/n:>10.0f} "
      f"{(new_total - base_total)/n:>+10.0f} {avg_pct:>+7.1f}%")
EOF
```

Expected format (numbers will vary):
```
case                       baseline        new      delta      pct
----------------------------------------------------------------------
kbo_schedule                    6490       3200      -3290    -50.7%
...
----------------------------------------------------------------------
AVG (playwright-path)           6400       3100      -3300    -51.6%
```

Save this full table output — it is the data for Task 7's conclusion doc.

- [ ] **Step 5: No commit for this task**

The measurement itself produces no tracked files. The computed numbers go into Task 7's conclusion doc.

---

## Task 7: Write conclusion doc applying the decision matrix

**Files:**
- Create: `docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md`

- [ ] **Step 1: Create the conclusion file**

Create `docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md` with the following template. Replace every `<...>` with the real numbers from Task 6 Step 4 and the decision derived from the matrix:

```markdown
# Content-Ready Wait Spike — Conclusion

**Date:** 2026-04-17
**Branch:** `spike/content-ready-wait`
**Design:** `docs/superpowers/specs/2026-04-17-content-ready-wait-design.md`
**Plan:** `docs/superpowers/plans/2026-04-17-content-ready-wait.md`

## Result

**Decision:** <ADOPT | RETUNE | REJECT>

## Numbers (playwright-path cases only, median of 3 runs)

| case | baseline fetch_ms | new fetch_ms | delta | pct |
|---|---|---|---|---|
| kbo_schedule | <b> | <s> | <Δ> | <pct>% |
| korean_news_ranking | <b> | <s> | <Δ> | <pct>% |
| pricing_page_ko | <b> | <s> | <Δ> | <pct>% |
| english_tech_docs | <b> | <s> | <Δ> | <pct>% |
| blog_post_no_heading | <b> | <s> | <Δ> | <pct>% |
| very_short_page | <b> | <s> | <Δ> | <pct>% |
| **AVG** | <b_avg> | <s_avg> | <Δ_avg> | **<pct_avg>%** |

Parity: baseline 12 / 12 PASS, new 12 / 12 PASS.

## Decision Reasoning

<Apply the success criteria from the design doc:

| Threshold | Observed | Met? |
|---|---|---|
| Parity 12/12 | <yes/no> | <yes/no> |
| Avg reduction ≥ 20% (Adopt) | <X%> | <yes/no> |

Short paragraph (2-4 sentences) on why the decision follows. Call out
any case-specific surprises — e.g. one case regressed while the
average improved, or the ceiling was hit on a page we expected to
fast-exit.>

## Failure / Oddities

<One bullet per case that behaved unexpectedly. If no surprises, write
"No oddities — predicate fired on all six cases within the expected
window." If Decision = REJECT or RETUNE, classify each miss:
- "stable window too tight on <case>, hit ceiling"
- "placeholder regex missed marker X on <case>"
- "len > 100 threshold excluded <case> from fast-exit"
etc.>

## Next Steps

<If ADOPT:
- Update `CLAUDE.md` "Things NOT to change" entry for `wait_for_ms` to
  reflect its new ceiling-not-fixed semantics
- Open PR from `spike/content-ready-wait` to `main`

If RETUNE:
- One round of tuning (stable window, min text, placeholder regex);
  document each change and re-measure; if still < 20 %, REJECT

If REJECT:
- Keep branch for reference, no further work>
```

- [ ] **Step 2: Commit the conclusion**

```bash
git add docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md
git commit -m "docs: content-ready wait 스파이크 결론"
```

- [ ] **Step 3: Report the decision to the user**

Show the user:
- The decision (ADOPT / RETUNE / REJECT)
- Avg fetch_ms reduction percentage
- Link to the conclusion file

---

## Task 8 (conditional — ADOPT only): CLAUDE.md update + PR to main

Execute only if Task 7's decision is ADOPT.

**Files:**
- Modify: `CLAUDE.md` (update the `wait_for_ms` row in "Things NOT to change")

- [ ] **Step 1: Update the CLAUDE.md load-bearing-values table**

Find the row in the table that reads:

```markdown
| `fetchers/playwright.py wait_for_ms` | `5000` | Naver Sports SPA needs this much |
```

Replace with:

```markdown
| `fetchers/playwright.py wait_for_ms` | `5000` | Ceiling for content-ready wait. Fast pages exit sub-second; SPAs that never stabilize fall back to this. Changing requires re-running the parity matrix. |
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude-md): update wait_for_ms semantics (content-ready ceiling)"
```

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin spike/content-ready-wait
gh pr create --base main --head spike/content-ready-wait \
  --title "feat: content-ready wait replaces fixed 5 s padding" \
  --body "$(cat <<'EOF'
## Summary

- Replaces the fixed `page.wait_for_timeout(wait_for_ms=5000)` in
  `fetchers/playwright.py` with a content-ready detector.
- Helper `_wait_for_content_ready` polls a JS predicate that watches
  `document.body.innerText.length` for 600 ms stability and — when a
  profile is in scope — checks that the profile `main_selector` holds
  non-placeholder content.
- `wait_for_ms=5000` is retained as the hard ceiling; worst-case
  behavior is identical to before.
- `profile_selector` is threaded from `pipeline.py` at the profile
  fast-path and transfer-path call sites; full-pipeline fallback stays
  selector-less by design.

Spec: `docs/superpowers/specs/2026-04-17-content-ready-wait-design.md`
Plan: `docs/superpowers/plans/2026-04-17-content-ready-wait.md`
Conclusion (with numbers): `docs/superpowers/specs/2026-04-17-content-ready-wait-conclusion.md`

## Test plan

- [x] Parity matrix: 12 / 12 PASS (3 runs, spike branch)
- [x] Baseline comparison: avg `fetch_ms` reduction on playwright-path
  cases recorded in the conclusion doc
- [ ] Smoke check post-merge: `python tests/test_pipeline.py` from a
  fresh checkout

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Report the resulting PR URL to the user.
