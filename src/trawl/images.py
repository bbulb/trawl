"""Image-dominant page detection helpers."""

from __future__ import annotations

import os
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

MIN_CONTENT_IMAGES = 5
MAX_CHARS_PER_IMAGE = 800
MAX_CONTENT_IMAGES = 40

_URL_ATTRS = ("src", "data-src", "ec-data-src", "data-original", "data-lazy-src")
_SKIP_EXTENSIONS = (".svg", ".gif", ".ico")
_NOISE_TAGS = {"nav", "header", "footer", "aside"}
_NOISE_CLS_RE = re.compile(
    r"\b(nav|navigation|sidebar|toc|table-of-contents|menu|banner|footer|header|"
    r"breadcrumb|site-header|site-footer)\b",
    re.IGNORECASE,
)
_LEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")


def _attr_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value or "")


def _in_noise_region(img) -> bool:
    node = img
    while node is not None:
        name = getattr(node, "name", None)
        if name and str(name).lower() in _NOISE_TAGS:
            return True
        if _NOISE_CLS_RE.search(_attr_text(node.get("id"))):
            return True
        if _NOISE_CLS_RE.search(_attr_text(node.get("class"))):
            return True
        node = getattr(node, "parent", None)
    return False


def _explicitly_tiny(img) -> bool:
    for attr in ("width", "height"):
        if attr not in img.attrs:
            continue
        match = _LEADING_NUMBER_RE.match(_attr_text(img.get(attr)))
        if match and float(match.group(1)) < 100:
            return True
    return False


def _image_url(img) -> str:
    for attr in _URL_ATTRS:
        value = _attr_text(img.get(attr)).strip()
        if value and not value.lower().startswith("data:"):
            return value
    return ""


def scan_content_images(html: str, markdown_chars: int, base_url: str) -> tuple[bool, list[str]]:
    """Return (image_dominant, content_image_urls)."""
    if os.environ.get("TRAWL_IMAGE_SCAN") == "0" or not html:
        return False, []

    try:
        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for img in soup.find_all("img"):
            if _in_noise_region(img) or _explicitly_tiny(img):
                continue
            raw_url = _image_url(img)
            if not raw_url or raw_url.lower().startswith("data:"):
                continue
            if urlsplit(raw_url).path.lower().endswith(_SKIP_EXTENSIONS):
                continue
            absolute_url = urljoin(base_url, raw_url)
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            urls.append(absolute_url)

        image_dominant = (
            len(urls) >= MIN_CONTENT_IMAGES
            and (markdown_chars / max(1, len(urls))) <= MAX_CHARS_PER_IMAGE
        )
        return image_dominant, urls[:MAX_CONTENT_IMAGES] if image_dominant else []
    except Exception:
        return False, []
