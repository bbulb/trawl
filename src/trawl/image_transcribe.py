"""Opt-in VLM transcription for text-bearing content images."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
PROMPT = (
    "Transcribe all text visible in this image exactly as written, preserving the original "
    "language. Output only the transcribed text with no commentary. If the image contains "
    "no text, output exactly NO_TEXT."
)

DEFAULT_MAX_IMAGES = 5
DEFAULT_MAX_BYTES = 4_194_304
DEFAULT_CACHE_TTL_SECONDS = 604_800
DEFAULT_CACHE_DIR = "~/.cache/trawl/image_transcripts"
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 20.0

# Tall marketing banners carry text; square gallery photos do not.
MIN_IMAGE_HEIGHT_PX = 800
MIN_ASPECT_RATIO = 1.5


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _max_images() -> int:
    return max(0, _env_int("TRAWL_IMAGE_TRANSCRIBE_MAX_IMAGES", DEFAULT_MAX_IMAGES))


def _max_bytes() -> int:
    return max(0, _env_int("TRAWL_IMAGE_TRANSCRIBE_MAX_BYTES", DEFAULT_MAX_BYTES))


def _cache_ttl_seconds() -> int:
    return _env_int("TRAWL_IMAGE_TRANSCRIBE_CACHE_TTL", DEFAULT_CACHE_TTL_SECONDS)


def _cache_dir() -> Path:
    return Path(os.environ.get("TRAWL_IMAGE_TRANSCRIBE_CACHE_PATH", DEFAULT_CACHE_DIR)).expanduser()


def _vlm_base_url() -> str:
    return os.environ.get("TRAWL_VLM_URL", "http://localhost:8080/v1")


def _vlm_model() -> str:
    return os.environ.get("TRAWL_VLM_MODEL", "gemma")


def _vlm_timeout_s() -> float:
    return _env_float("TRAWL_VLM_TIMEOUT", 120.0)


def _vlm_max_tokens() -> int:
    return _env_int("TRAWL_VLM_MAX_TOKENS", 2048)


def _vlm_slot_id() -> int | None:
    raw = os.environ.get("TRAWL_VLM_SLOT")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def is_enabled(override: bool | None) -> bool:
    """Return whether image transcription should run for this call."""
    if override is not None:
        return bool(override)
    return os.environ.get("TRAWL_IMAGE_TRANSCRIBE", "0") == "1"


def _cache_enabled() -> bool:
    return _cache_ttl_seconds() > 0


def _image_sha256(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _cache_key(*, model: str, base_url: str, image_sha256: str) -> str:
    payload = {
        "schema": 1,
        "model": model,
        "base_url": base_url,
        "image_sha256": image_sha256,
        "prompt_version": PROMPT_VERSION,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    return _cache_dir() / f"{key}.json"


def _cache_get(*, model: str, base_url: str, image_sha256: str) -> str | None:
    if not _cache_enabled():
        return None

    path = _cache_path(_cache_key(model=model, base_url=base_url, image_sha256=image_sha256))
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _safe_unlink(path)
        return None

    now_ts = time.time()
    created_at = float(raw.get("created_at") or 0)
    if created_at + _cache_ttl_seconds() < now_ts:
        _safe_unlink(path)
        return None

    transcript = raw.get("transcript")
    return transcript if isinstance(transcript, str) else None


def _cache_put(
    *,
    model: str,
    base_url: str,
    image_sha256: str,
    transcript: str,
) -> None:
    if not _cache_enabled():
        return

    cache_dir = _cache_dir()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("image_transcribe: cannot create %s: %s", cache_dir, e)
        return

    payload = {
        "transcript": transcript,
        "created_at": time.time(),
    }
    target = _cache_path(_cache_key(model=model, base_url=base_url, image_sha256=image_sha256))
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".tmp",
            dir=cache_dir,
            delete=False,
            encoding="utf-8",
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, target)
    except OSError as e:
        logger.warning("image_transcribe: write failed: %s", e)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("image_transcribe: unlink %s failed: %s", path, e)


def _download_image(url: str) -> tuple[bytes, str | None] | None:
    cap = _max_bytes()
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as resp:
        if resp.status_code >= 400:
            return None
        buf = bytearray()
        for chunk in resp.iter_bytes():
            if len(buf) + len(chunk) > cap:
                return None
            buf.extend(chunk)
        if not buf:
            return None
        return bytes(buf), resp.headers.get("content-type")


def _image_info(image_bytes: bytes, content_type: str | None) -> tuple[int, int, str]:
    try:
        from PIL import Image
    except ModuleNotFoundError:
        width, height, image_format = _image_info_without_pillow(image_bytes)
        return width, height, _normalized_content_type(content_type, image_format)

    with Image.open(BytesIO(image_bytes)) as im:
        width, height = im.size
        image_type = _normalized_content_type(content_type, im.format)
    return width, height, image_type


def _image_info_without_pillow(image_bytes: bytes) -> tuple[int, int, str | None]:
    try:
        import fitz

        pix = fitz.Pixmap(image_bytes)
    except Exception:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
            return (
                int.from_bytes(image_bytes[16:20], "big"),
                int.from_bytes(image_bytes[20:24], "big"),
                "png",
            )
        raise ValueError("Pillow unavailable and image dimensions could not be parsed") from None
    width, height = pix.width, pix.height
    pix = None
    return width, height, None


def _normalized_content_type(content_type: str | None, image_format: str | None) -> str:
    if content_type:
        base = content_type.split(";", 1)[0].strip()
        if base:
            return base
    fmt = (image_format or "").lower()
    if fmt in {"jpeg", "jpg"}:
        return "image/jpeg"
    if fmt:
        return f"image/{fmt}"
    return "image/jpeg"


def _selected_image(url: str) -> tuple[str, bytes, str] | None:
    downloaded = _download_image(url)
    if downloaded is None:
        return None
    image_bytes, content_type = downloaded
    width, height, image_type = _image_info(image_bytes, content_type)
    if width <= 0 or height < MIN_IMAGE_HEIGHT_PX or (height / width) < MIN_ASPECT_RATIO:
        return None
    return url, image_bytes, image_type


def _transcribe_bytes(image_bytes: bytes, content_type: str) -> str:
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    base_url = _vlm_base_url().rstrip("/")
    payload = {
        "model": _vlm_model(),
        "temperature": 0.0,
        "max_tokens": _vlm_max_tokens(),
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{content_type};base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    slot_id = _vlm_slot_id()
    if slot_id is not None:
        payload["id_slot"] = slot_id

    resp = httpx.post(f"{base_url}/chat/completions", json=payload, timeout=_vlm_timeout_s())
    resp.raise_for_status()
    body = resp.json()
    message = body["choices"][0]["message"]
    content = (message.get("content") or "").strip()
    if not content:
        content = (message.get("reasoning_content") or "").strip()
    return content


def transcribe_content_images(urls: list[str]) -> list[tuple[str, str]]:
    """Download, select, transcribe, and cache text-bearing content images."""
    results: list[tuple[str, str]] = []
    try:
        selected: list[tuple[str, bytes, str]] = []
        for url in urls:
            if len(selected) >= _max_images():
                break
            try:
                image = _selected_image(url)
            except Exception as e:  # noqa: BLE001
                logger.debug("image_transcribe: skipping %s: %s", url, e)
                continue
            if image is not None:
                selected.append(image)

        for url, image_bytes, content_type in selected:
            try:
                model = _vlm_model()
                base_url = _vlm_base_url()
                image_hash = _image_sha256(image_bytes)
                transcript = _cache_get(
                    model=model,
                    base_url=base_url,
                    image_sha256=image_hash,
                )
                from_vlm = transcript is None
                if from_vlm:
                    transcript = _transcribe_bytes(image_bytes, content_type)
                transcript = transcript.strip()
                if transcript == "NO_TEXT" or len(transcript) < 20:
                    continue
                if from_vlm:
                    _cache_put(
                        model=model,
                        base_url=base_url,
                        image_sha256=image_hash,
                        transcript=transcript,
                    )
                results.append((url, transcript))
            except Exception as e:  # noqa: BLE001
                logger.debug("image_transcribe: transcription failed for %s: %s", url, e)
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug("image_transcribe: failed: %s", e)
    return results
