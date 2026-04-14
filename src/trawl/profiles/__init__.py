"""trawl.profiles — per-URL extraction profile layer.

Public API:

- `generate_profile(url, force_refresh=False)` — the entry point the
  MCP server's `profile_page` tool calls. Loads an existing profile or
  runs the full render → VLM → mapper → save flow.
- `load_profile(url)`, `save_profile(profile)`, `profile_path_for(url)`,
  `profile_dir()` — filesystem access.
- `build_profile(...)` — assemble a Profile from raw VLM + mapper output.
- `url_hash(url)` — the 12-char cache key.
- `track_visit(url)`, `get_visit_count(url)` — visit counter for the
  lazy-hint logic in fetch_relevant's fallback path.
- `Profile`, `ProfileVLMSection`, `ProfileMapperSection` — the schema
  dataclasses.
- `VLMError`, `VLMResponse`, `ItemHints` — VLM layer types.
- `MapResult`, `MappedAnchor` — mapper layer types.
- `VLM_BASE_URL`, `VLM_MODEL` — module-level constants the MCP layer uses
  to tag profile_used/profile_hash metadata.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .cache import get_visit_count, track_visit
from .mapper import MappedAnchor, MapResult, find_main_subtree
from .profile import (
    Profile,
    ProfileMapperSection,
    ProfileVLMSection,
    build_profile,
    build_profile_copy,
    copy_profile_for_new_url,
    extract_fresh_anchors,
    list_host_profiles,
    load_profile,
    profile_dir,
    profile_path_for,
    save_profile,
    url_hash,
)
from .vlm import (
    VLM_BASE_URL,
    VLM_MODEL,
    ItemHints,
    VLMError,
    VLMResponse,
    call_vlm,
)

__all__ = [
    # High-level orchestrator
    "generate_profile",
    # Load/save
    "load_profile",
    "save_profile",
    "profile_path_for",
    "profile_dir",
    "build_profile",
    "url_hash",
    # Host-transfer helpers
    "list_host_profiles",
    "extract_fresh_anchors",
    "build_profile_copy",
    "copy_profile_for_new_url",
    # Visit counter
    "track_visit",
    "get_visit_count",
    # Schema
    "Profile",
    "ProfileVLMSection",
    "ProfileMapperSection",
    # VLM layer
    "VLMError",
    "VLMResponse",
    "ItemHints",
    "call_vlm",
    "VLM_BASE_URL",
    "VLM_MODEL",
    # Mapper layer
    "MapResult",
    "MappedAnchor",
    "find_main_subtree",
]


# Max screenshot height, matching the spike's default. Long pages are
# clipped to the top portion so the image stays under the VLM's context
# budget. 3000px is generous; typical pages fit easily.
_MAX_SCREENSHOT_HEIGHT_PX = 3000


def _capture_screenshot(page, out_dir: Path) -> tuple[Path, bool]:
    """Take a full-page (or clipped) screenshot of `page` and save it to
    `out_dir/screenshot.png`. Returns (path, truncated_flag).

    Uses the page's `document.body.scrollHeight` to decide whether to
    full-page or clip. Same behavior as the spike's render.py but moved
    here because it's profile-generation-specific.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "screenshot.png"
    scroll_h = page.evaluate("() => document.body.scrollHeight")
    truncated = scroll_h > _MAX_SCREENSHOT_HEIGHT_PX
    if truncated:
        page.screenshot(
            path=str(path),
            clip={"x": 0, "y": 0, "width": 1280, "height": _MAX_SCREENSHOT_HEIGHT_PX},
        )
    else:
        page.screenshot(path=str(path), full_page=True)
    return path, truncated


def _screenshot_workdir(url: str) -> Path:
    """Return a temp directory dedicated to one profile-generation run.

    Screenshots live under a tmp dir, not under the profile cache, so a
    failed generation leaves no debris next to the persistent profile
    files. Callers should clean up the dir when done (handled by the
    TemporaryDirectory context manager in generate_profile).
    """
    return Path(tempfile.mkdtemp(prefix=f"trawl-profile-{url_hash(url)}-"))


def generate_profile(url: str, *, force_refresh: bool = False) -> dict:
    """Load an existing profile for `url` or generate a new one.

    Returns a dict in the shape the MCP profile_page tool expects:
    on success, a `summary_dict(cached=...)` payload; on failure, an
    `{ok: False, stage, error, notes}` dict.

    This function must not raise on normal failure modes (VLM down,
    mapper returns None, Playwright timeout) — it returns the failure
    as structured data so the MCP layer can surface it to the agent.
    Unexpected exceptions still propagate so the MCP server logs a
    traceback.
    """
    if not force_refresh:
        existing = load_profile(url)
        if existing is not None and existing.mapper.main_selector:
            return existing.summary_dict(cached=True)

    # Lazy import to avoid importing Playwright for callers that only
    # want profile load/save (e.g. the test suite's monkeypatched paths).
    import shutil

    from trawl.fetchers.playwright import render_session

    work_dir = _screenshot_workdir(url)
    try:
        try:
            with render_session(url) as r:
                screenshot_path, _truncated = _capture_screenshot(r.page, work_dir)
                try:
                    vlm_response = call_vlm(screenshot_path)
                except VLMError as e:
                    return {
                        "ok": False,
                        "stage": "vlm",
                        "error": str(e),
                        "notes": [],
                    }
                map_result = find_main_subtree(r.page, vlm_response.content_anchors)
        except Exception as e:
            return {
                "ok": False,
                "stage": "render",
                "error": f"{type(e).__name__}: {e}",
                "notes": [],
            }

        if map_result.selector is None:
            return {
                "ok": False,
                "stage": "mapper",
                "error": "LCA collapsed or all anchors missed",
                "notes": list(map_result.notes),
            }

        profile = build_profile(
            url=url,
            vlm_response=vlm_response,
            map_result=map_result,
            vlm_endpoint=VLM_BASE_URL,
            vlm_model=VLM_MODEL,
            min_chars_used=300,  # matches mapper.DEFAULT_MIN_CHARS
        )
        save_profile(profile)
        return profile.summary_dict(cached=False)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
