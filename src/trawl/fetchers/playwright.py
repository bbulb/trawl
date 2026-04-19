"""Playwright-based HTML fetcher.

Public API:
- `fetch(url, ...) -> FetchResult` — original one-shot HTML fetch, closes the
  browser context before returning. Existing callers (pipeline.fetch_relevant
  non-profile path) use this.
- `render_session(url, ...) -> Iterator[RenderResult]` — context manager
  that keeps the BrowserContext and Page alive for the duration of the
  `with` block, so callers can run `page.evaluate()`, `get_by_text()`,
  and other live-page operations (mapper, profile generation).

Both entry points share a `_open_context()` helper so stealth, viewport,
wait-until fallback, and wait_for_ms stay consistent between them.
"""

from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)
from playwright_stealth import Stealth

from .. import host_stats


@dataclass
class FetchResult:
    url: str
    html: str
    markdown: str
    raw_html: str
    fetcher: str
    elapsed_ms: int
    error: str | None = None
    content_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (bool(self.html) or bool(self.markdown))


def make_error_result(url: str, fetcher: str, t0: float, error: str) -> FetchResult:
    """Build an empty FetchResult with the given error and elapsed time."""
    return FetchResult(
        url=url,
        html="",
        markdown="",
        raw_html="",
        fetcher=fetcher,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        error=error,
    )


@dataclass
class RenderResult:
    """Yielded by `render_session`. `page` is a live Playwright Page that
    stays valid only inside the `with` block."""

    url: str
    page: Page
    html: str
    elapsed_ms: int


class _BrowserHolder:
    """Lazy-initialised, process-wide Chromium browser + Playwright runtime.

    Single instance `_browser_holder` owns the global state. `ensure()`
    brings the browser up on first use and registers teardown at exit;
    `teardown()` closes it. The module-level `_lock` serialises fetch
    calls because playwright contexts are not thread-safe.
    """

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._browser: Browser | None = None

    def ensure(self) -> Browser:
        if self._browser is not None:
            return self._browser
        pw = Stealth().use_sync(sync_playwright()).__enter__()
        try:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        except Exception:
            # launch() failing (missing browser binary, libs, sandbox flags)
            # leaves the Playwright context half-initialised. Stop it here
            # so the next ensure() call starts clean; otherwise the leftover
            # greenlet dispatcher and event loop poison the worker thread,
            # causing every subsequent sync_playwright() call to mis-report
            # "Sync API inside asyncio loop" instead of the real error.
            try:
                pw.stop()
            except Exception:
                pass
            raise
        self._pw = pw
        self._browser = browser
        atexit.register(self.teardown)
        return self._browser

    def teardown(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None


_browser_holder = _BrowserHolder()
_lock = threading.Lock()

# Short cap on the initial `networkidle` wait. Discourse/chat-like SPAs
# keep websockets open so networkidle never fires; falling back to
# `domcontentloaded` + the content-ready detector yields the same HTML
# much faster. Empirically tuned on telemetry (see commit body).
NETWORKIDLE_BUDGET_MS = 3000


def _wait_for_content_ready(page: Page, *, profile_selector: str | None, max_wait_ms: int) -> None:
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


@contextmanager
def _open_context(
    url: str,
    *,
    wait_for_ms: int,
    timeout_s: float,
    user_agent: str | None,
    profile_selector: str | None = None,
) -> Iterator[tuple[BrowserContext, Page, str, str | None]]:
    """Internal helper: open a stealth BrowserContext, navigate to `url`,
    yield (context, page, html, content_type). The context is closed in this
    generator's finally block when the caller exits the `with` block.

    Tries `networkidle` with a short `NETWORKIDLE_BUDGET_MS` cap so
    SPAs that hold long-polling connections (Discourse, chat UIs)
    don't burn the full timeout budget; falls back to
    `domcontentloaded` on PlaywrightTimeoutError. The content-ready
    detector (below) takes over from there.

    After navigation, `_wait_for_content_ready` watches for text-content
    stability (and `profile_selector` population when provided) with
    `wait_for_ms` as a hard ceiling. This replaces the old fixed
    `wait_for_timeout(wait_for_ms)` so fast pages return sub-second.
    """
    browser = _browser_holder.ensure()
    context = browser.new_context(
        user_agent=user_agent
        or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 900},
        locale="ko-KR",
    )
    try:
        page = context.new_page()
        goto_timeout_ms = int(timeout_s * 1000)
        networkidle_budget_ms = min(NETWORKIDLE_BUDGET_MS, goto_timeout_ms // 2)
        response = None
        try:
            response = page.goto(url, wait_until="networkidle", timeout=networkidle_budget_ms)
        except PlaywrightTimeoutError:
            response = page.goto(url, wait_until="domcontentloaded", timeout=goto_timeout_ms)
        if wait_for_ms > 0:
            _wait_for_content_ready(
                page, profile_selector=profile_selector, max_wait_ms=wait_for_ms
            )
        html = page.content()
        content_type = None
        if response is not None:
            try:
                content_type = response.header_value("content-type")
            except Exception:
                content_type = None
        yield context, page, html, content_type
    finally:
        try:
            context.close()
        except Exception:
            pass


def fetch(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
    profile_selector: str | None = None,
) -> FetchResult:
    """Fetch a URL's rendered HTML using headless Chromium.

    `wait_for_ms` is the ceiling for the post-navigation content-ready
    wait; fast pages exit well before it. `timeout_s` is the hard
    page-load ceiling. `profile_selector` — when provided — is used by
    the content-ready detector to verify the main content region holds
    non-placeholder text.

    The `wait_for_ms` caller default is refined through
    ``host_stats.ceiling_ms`` so repeat visits to the same host
    converge on that host's observed p95 rather than the static
    5000 ms budget. See C9 spec.
    """
    effective_wait_ms = host_stats.ceiling_ms(url, default=wait_for_ms)
    t0 = time.monotonic()
    with _lock:
        try:
            with _open_context(
                url,
                wait_for_ms=effective_wait_ms,
                timeout_s=timeout_s,
                user_agent=user_agent,
                profile_selector=profile_selector,
            ) as (_ctx, _page, html, content_type):
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                host_stats.record(url, elapsed_ms)
                return FetchResult(
                    url=url,
                    html=html,
                    markdown="",
                    raw_html=html,
                    fetcher="playwright",
                    elapsed_ms=elapsed_ms,
                    content_type=content_type,
                )
        except PlaywrightTimeoutError as e:
            return FetchResult(
                url=url,
                html="",
                markdown="",
                raw_html="",
                fetcher="playwright",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=f"PlaywrightTimeoutError: {e}",
            )
        except Exception as e:
            return FetchResult(
                url=url,
                html="",
                markdown="",
                raw_html="",
                fetcher="playwright",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=f"{type(e).__name__}: {e}",
            )


@contextmanager
def render_session(
    url: str,
    *,
    wait_for_ms: int = 5000,
    timeout_s: float = 30.0,
    user_agent: str | None = None,
    profile_selector: str | None = None,
) -> Iterator[RenderResult]:
    """Open `url`, yield a live RenderResult (with Page handle). The Page
    is only valid inside the `with` block. On exit the BrowserContext is
    closed; the caller must not retain the page reference afterwards.

    Callers use this when they need to run live Playwright operations
    (query_selector_all, page.evaluate, screenshot) that `fetch()` cannot
    support because it closes the context before returning.

    `profile_selector` — when provided — lets the content-ready wait
    verify the main content region has non-placeholder text before the
    session yields.

    Like `fetch()`, the wait ceiling is refined through
    ``host_stats.ceiling_ms``. Session runtime isn't recorded back into
    host_stats — the caller holds the session arbitrarily long, so the
    elapsed time would be a poor signal of host responsiveness.
    """
    effective_wait_ms = host_stats.ceiling_ms(url, default=wait_for_ms)
    t0 = time.monotonic()
    with _lock:
        with _open_context(
            url,
            wait_for_ms=effective_wait_ms,
            timeout_s=timeout_s,
            user_agent=user_agent,
            profile_selector=profile_selector,
        ) as (_ctx, page, html, _content_type):
            yield RenderResult(
                url=url,
                page=page,
                html=html,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )
