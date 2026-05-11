"""Optional Scrapling HTML fallback fetcher.

Scrapling is intentionally not a default dependency. This module imports it
only inside ``fetch()`` and only runs when ``TRAWL_SCRAPLING_FALLBACK=1``.
The returned HTML is fed back into trawl's existing extraction/chunking/
retrieval pipeline; Scrapling is only an alternate HTML supplier.
"""

from __future__ import annotations

import importlib
import os
import time
from typing import Any

from .playwright import FetchResult, make_error_result

DEFAULT_TIMEOUT_MS = 30_000


def is_enabled() -> bool:
    """Return True when the optional Scrapling fallback is enabled."""
    return os.environ.get("TRAWL_SCRAPLING_FALLBACK", "0") == "1"


def fetch(url: str, *, mode: str = "auto", reason: str = "") -> FetchResult:
    """Fetch rendered HTML through Scrapling when explicitly enabled.

    ``mode`` accepts ``dynamic``, ``stealthy``, or ``auto``. Auto uses the
    stealthy fetcher only for anti-bot-looking failures; otherwise it uses
    DynamicFetcher as the cheaper browser fallback.
    """
    t0 = time.monotonic()
    if not is_enabled():
        return make_error_result(
            url,
            "scrapling",
            t0,
            "Scrapling fallback disabled; set TRAWL_SCRAPLING_FALLBACK=1",
        )

    chosen_mode = _choose_mode(mode, reason)
    try:
        fetchers = importlib.import_module("scrapling.fetchers")
        fetcher_cls = (
            fetchers.StealthyFetcher if chosen_mode == "stealthy" else fetchers.DynamicFetcher
        )
    except (ImportError, AttributeError) as e:
        return make_error_result(
            url,
            "scrapling",
            t0,
            f"Scrapling unavailable: {type(e).__name__}: {e}",
        )

    try:
        response = fetcher_cls.fetch(url, timeout=_timeout_ms())
    except Exception as e:
        return make_error_result(url, f"scrapling-{chosen_mode}", t0, f"{type(e).__name__}: {e}")

    html = _response_html(response)
    status = int(getattr(response, "status", 0) or 0)
    error = None
    if status >= 400:
        error = f"Scrapling returned HTTP {status}"
    elif not html:
        error = "Scrapling returned empty body"

    return FetchResult(
        url=url,
        html=html,
        markdown="",
        raw_html=html,
        fetcher=f"scrapling-{chosen_mode}",
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        error=error,
        content_type=_header(response, "content-type"),
        etag=_header(response, "etag"),
        last_modified=_header(response, "last-modified"),
    )


def _choose_mode(mode: str, reason: str) -> str:
    mode = (mode or "auto").lower()
    if mode == "stealthy":
        return "stealthy"
    if mode == "dynamic":
        return "dynamic"
    if reason == "anti_bot":
        return "stealthy"
    return "dynamic"


def _timeout_ms() -> int:
    try:
        return int(os.environ.get("TRAWL_SCRAPLING_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    except ValueError:
        return DEFAULT_TIMEOUT_MS


def _response_html(response: Any) -> str:
    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        encoding = str(getattr(response, "encoding", "") or "utf-8")
        return body.decode(encoding, errors="replace")
    if isinstance(body, str):
        return body
    for attr in ("html", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str):
            return value
        if callable(value):
            try:
                called = value()
            except Exception:
                continue
            if isinstance(called, str):
                return called
    return ""


def _header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    if value is None:
        try:
            value = headers.get(name.title())
        except AttributeError:
            return None
    if value is None:
        return None
    return str(value)
