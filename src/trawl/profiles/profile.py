"""Profile schema, filesystem cache, save/load.

A profile is a per-URL JSON file at
$TRAWL_PROFILE_DIR/<url_hash>.json (default ~/.cache/trawl/profiles).
Each file captures the VLM output (page_type, anchors, structure
description) and the mapper output (main_selector, verification
anchors) so subsequent fetch_page calls on the same URL can skip the
full render+extract+embed pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .mapper import MapResult
from .vlm import VLMResponse

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Number of verification anchors saved on a profile. Used both when
# building a fresh profile (`build_profile`) and when seeding fresh
# anchors on a copied profile (`extract_fresh_anchors`).
MAX_VERIFICATION_ANCHORS = 3

DEFAULT_PROFILE_DIR = (
    Path(
        os.environ.get(
            "TRAWL_PROFILE_DIR",
            str(Path.home() / ".cache" / "trawl" / "profiles"),
        )
    )
    .expanduser()
    .resolve()
)


def profile_dir() -> Path:
    """Return the profile cache directory, creating it if missing."""
    DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_PROFILE_DIR


def url_hash(url: str) -> str:
    """Deterministic 12-char hex digest of a URL. Matches the spike's
    hashing so pre-existing cache files remain loadable after rebase
    (though in practice, no such files exist for the library install)."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def profile_path_for(url: str) -> Path:
    return profile_dir() / f"{url_hash(url)}.json"


@dataclass
class ProfileVLMSection:
    endpoint: str
    model: str
    page_type: str
    structure_description: str
    anchors_requested: list[str]
    anchors_found: list[str]
    anchors_missed: list[str]
    noise_labels: list[str]
    item_hints: dict


@dataclass
class ProfileMapperSection:
    main_selector: str | None
    lca_tag: str
    lca_path: list[str]
    subtree_char_count: int
    min_chars_used: int
    notes: list[str] = field(default_factory=list)
    verification_anchors: list[str] = field(default_factory=list)


@dataclass
class Profile:
    schema_version: int
    url: str
    url_hash: str
    generated_at: str
    vlm: ProfileVLMSection
    mapper: ProfileMapperSection

    def summary_dict(self, *, cached: bool) -> dict:
        """Shape returned to MCP callers of profile_page."""
        return {
            "ok": True,
            "url": self.url,
            "url_hash": self.url_hash,
            "cached": cached,
            "main_selector": self.mapper.main_selector,
            "lca_tag": self.mapper.lca_tag,
            "subtree_char_count": self.mapper.subtree_char_count,
            "verification_anchors": list(self.mapper.verification_anchors),
            "page_type": self.vlm.page_type,
            "structure_description": self.vlm.structure_description,
        }


def build_profile(
    *,
    url: str,
    vlm_response: VLMResponse,
    map_result: MapResult,
    vlm_endpoint: str,
    vlm_model: str,
    min_chars_used: int,
) -> Profile:
    """Assemble a Profile from the raw VLM + mapper outputs.

    Verification anchors are the confirmed-inside-subtree anchors,
    minus the outlier ones that were dropped from the LCA because they
    live in nav/header, not in the main subtree.
    """
    found_anchors = [ma.anchor for ma in map_result.anchors_found]
    outlier_set = set(map_result.outlier_anchors)
    verification = [a for a in found_anchors if a not in outlier_set][:MAX_VERIFICATION_ANCHORS]
    return Profile(
        schema_version=SCHEMA_VERSION,
        url=url,
        url_hash=url_hash(url),
        generated_at=datetime.now(timezone.utc).isoformat(),
        vlm=ProfileVLMSection(
            endpoint=vlm_endpoint,
            model=vlm_model,
            page_type=vlm_response.page_type,
            structure_description=vlm_response.structure_description,
            anchors_requested=list(vlm_response.content_anchors),
            anchors_found=found_anchors,
            anchors_missed=list(map_result.anchors_missed),
            noise_labels=list(vlm_response.noise_labels),
            item_hints=asdict(vlm_response.item_hints),
        ),
        mapper=ProfileMapperSection(
            main_selector=map_result.selector,
            lca_tag=map_result.lca_tag,
            lca_path=list(map_result.lca_path),
            subtree_char_count=map_result.subtree_chars,
            min_chars_used=min_chars_used,
            notes=list(map_result.notes),
            verification_anchors=verification,
        ),
    )


def save_profile(profile: Profile) -> Path:
    path = profile_path_for(profile.url)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(asdict(profile), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def _profile_from_dict(data: dict) -> Profile:
    """Reconstruct a Profile from a parsed JSON dict.

    Raises KeyError / TypeError on schema mismatch; callers decide how
    to handle (load_profile treats it as missing; list_host_profiles
    logs and skips).
    """
    return Profile(
        schema_version=data["schema_version"],
        url=data["url"],
        url_hash=data["url_hash"],
        generated_at=data["generated_at"],
        vlm=ProfileVLMSection(**data["vlm"]),
        mapper=ProfileMapperSection(**data["mapper"]),
    )


def load_profile(url: str) -> Profile | None:
    path = profile_path_for(url)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _profile_from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        # Corrupt or schema-incompatible profile file: treat as missing.
        # Caller will fall through to regeneration.
        return None


def list_host_profiles(host: str) -> list[Profile]:
    """Return all saved profiles whose URL's netloc matches `host`,
    sorted by generated_at descending (most recent first).

    Corrupt or schema-incompatible profile files are skipped with a
    warning log, matching load_profile's policy. An empty result is
    fine — callers short-circuit before rendering.
    """
    out: list[Profile] = []
    for path in profile_dir().glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("list_host_profiles: skipping %s: %s", path.name, e)
            continue
        try:
            if urlsplit(data["url"]).netloc.lower() != host.lower():
                continue
            profile = _profile_from_dict(data)
        except (KeyError, TypeError) as e:
            logger.warning("list_host_profiles: schema mismatch in %s: %s", path.name, e)
            continue
        out.append(profile)
    out.sort(key=lambda p: p.generated_at, reverse=True)
    return out


def extract_fresh_anchors(text: str, n: int = MAX_VERIFICATION_ANCHORS) -> list[str]:
    """Pick up to `n` non-whitespace lines of stripped length 5..50
    from `text`, in document order.

    Used to seed verification_anchors on a copied profile so future
    drift detection against the new URL works on the new URL's own
    content. Returns fewer than `n` items if the input has fewer
    qualifying lines; returns an empty list on empty / whitespace input.
    """
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if 5 <= len(s) <= 50:
            out.append(s)
            if len(out) >= n:
                break
    return out


def build_profile_copy(
    parent: Profile,
    new_url: str,
    new_anchors: list[str],
) -> Profile:
    """Build an in-memory Profile that clones `parent` under `new_url`
    with fresh verification anchors. Does NOT persist — caller is
    responsible for calling save_profile() if persistence is desired.

    Split from copy_profile_for_new_url so transfer-path callers can
    keep a correctly-constructed Profile reference (with
    url_hash(new_url)) even when the save step fails.
    """
    new_mapper = replace(
        parent.mapper,
        verification_anchors=list(new_anchors),
    )
    new_vlm = replace(parent.vlm)  # shallow copy of the section
    return Profile(
        schema_version=parent.schema_version,
        url=new_url,
        url_hash=url_hash(new_url),
        generated_at=datetime.now(timezone.utc).isoformat(),
        vlm=new_vlm,
        mapper=new_mapper,
    )


def copy_profile_for_new_url(
    parent: Profile,
    new_url: str,
    new_anchors: list[str],
) -> Profile:
    """Clone `parent` with url / url_hash / verification_anchors /
    generated_at updated for `new_url`, then persist it.

    Structural fields (main_selector, lca_tag, lca_path,
    subtree_char_count, min_chars_used, notes, vlm section) are
    inherited verbatim because they describe the shared page
    template, not instance content.

    The verification anchors are replaced with `new_anchors` — these
    should come from the NEW URL's matched subtree so future drift
    detection against this URL is meaningful.
    """
    copy = build_profile_copy(parent, new_url, new_anchors)
    save_profile(copy)
    return copy
