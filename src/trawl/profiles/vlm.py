"""HTTP client for the local llama-server vision endpoint.

POSTs an OpenAI-compatible chat-completions payload with a base64 image
to TRAWL_VLM_URL. Parses the response into a VLMResponse dataclass.

Two Gemma 4 quirks are handled, both following the pattern from
trawl.hyde:
  1. `chat_template_kwargs.enable_thinking=False` is sent so the model
     doesn't burn the token budget on reasoning. Without this, `content`
     comes back empty and only `reasoning_content` is populated.
  2. As a defensive fallback for older llama-server builds that ignore
     the kwarg, if `content` is empty we fall back to `reasoning_content`
     before declaring the response invalid.

One retry on JSON parse / validation failure with a corrective system
message; a second failure raises VLMError so the spike fails loudly
(per the spec, sustained inability to return valid JSON is itself a
no-go signal).
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .prompts import SYSTEM_PROMPT, build_user_prompt

VLM_BASE_URL = os.environ.get(
    "TRAWL_VLM_URL",
    "http://localhost:8080/v1",
)
VLM_MODEL = os.environ.get("TRAWL_VLM_MODEL", "gemma")
VLM_TIMEOUT_S = float(os.environ.get("TRAWL_VLM_TIMEOUT", "120"))
VLM_MAX_TOKENS = int(os.environ.get("TRAWL_VLM_MAX_TOKENS", "2048"))
# Pin requests to a specific llama-server slot for KV-cache reuse.
# Set to an integer slot ID (e.g. "2") to avoid evicting other consumers'
# cached prompts on a shared server. Unset = let the server choose.
VLM_SLOT_ID: int | None = int(v) if (v := os.environ.get("TRAWL_VLM_SLOT")) is not None else None


class VLMError(RuntimeError):
    pass


@dataclass
class ItemHints:
    has_repeating_items: bool = False
    item_description: str | None = None
    example_row_anchors: list[str] = field(default_factory=list)


@dataclass
class VLMResponse:
    page_type: str
    structure_description: str
    content_anchors: list[str]
    noise_labels: list[str]
    item_hints: ItemHints
    raw: str  # the verbatim model output, kept for logging


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


def _validate(parsed: dict) -> VLMResponse:
    required = {
        "page_type",
        "structure_description",
        "content_anchors",
        "noise_labels",
        "item_hints",
    }
    missing = required - set(parsed.keys())
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")

    anchors = parsed.get("content_anchors") or []
    if not isinstance(anchors, list) or len(anchors) < 3:
        raise ValueError(f"content_anchors must be a list of >= 3 strings, got {anchors!r}")
    if not all(isinstance(a, str) and a.strip() for a in anchors):
        raise ValueError(f"content_anchors must contain non-empty strings, got {anchors!r}")

    hints = parsed.get("item_hints") or {}
    if not isinstance(hints, dict):
        raise ValueError(f"item_hints must be a dict, got {hints!r}")

    return VLMResponse(
        page_type=str(parsed.get("page_type") or "other"),
        structure_description=str(parsed.get("structure_description") or ""),
        content_anchors=[a.strip() for a in anchors],
        noise_labels=[str(n) for n in (parsed.get("noise_labels") or [])],
        item_hints=ItemHints(
            has_repeating_items=bool(hints.get("has_repeating_items", False)),
            item_description=hints.get("item_description"),
            example_row_anchors=[str(a) for a in (hints.get("example_row_anchors") or [])],
        ),
        raw="",  # filled by caller
    )


def _build_payload(
    *,
    image_b64: str,
    extra_system: str = "",
) -> dict:
    system_text = SYSTEM_PROMPT
    if extra_system:
        system_text = system_text + "\n\n" + extra_system
    payload = {
        "model": VLM_MODEL,
        "temperature": 0.0,
        "max_tokens": VLM_MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": system_text},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": build_user_prompt()},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            },
        ],
    }
    if VLM_SLOT_ID is not None:
        payload["id_slot"] = VLM_SLOT_ID
    return payload


def call_vlm(screenshot_path: Path) -> VLMResponse:
    with screenshot_path.open("rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = _build_payload(image_b64=image_b64)
    raw1 = _post_and_extract(payload)
    try:
        parsed = json.loads(_strip_code_fences(raw1))
        resp = _validate(parsed)
        resp.raw = raw1
        return resp
    except (json.JSONDecodeError, ValueError) as e:
        # One retry with a corrective hint.
        retry_payload = _build_payload(
            image_b64=image_b64,
            extra_system=(
                f"Your previous response could not be parsed as valid JSON "
                f"matching the schema: {e}. The previous output was:\n\n{raw1}\n\n"
                f"Output ONLY the corrected JSON now, with no surrounding text."
            ),
        )
        raw2 = _post_and_extract(retry_payload)
        try:
            parsed = json.loads(_strip_code_fences(raw2))
            resp = _validate(parsed)
            resp.raw = raw2
            return resp
        except (json.JSONDecodeError, ValueError) as e2:
            raise VLMError(
                f"VLM returned invalid JSON twice. First error: {e}. "
                f"Second error: {e2}. Last raw output:\n{raw2}"
            ) from e2


def _post_and_extract(payload: dict) -> str:
    """POST to VLM endpoint and return message content.

    If `content` is empty (older llama-server builds that ignore the
    enable_thinking kwarg) fall back to `reasoning_content`. Same pattern
    as trawl.hyde:expand.
    """
    resp = httpx.post(f"{VLM_BASE_URL}/chat/completions", json=payload, timeout=VLM_TIMEOUT_S)
    resp.raise_for_status()
    body = resp.json()
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise VLMError(f"unexpected VLM response shape: {body!r}") from e
    content = (message.get("content") or "").strip()
    if not content:
        content = (message.get("reasoning_content") or "").strip()
    if not content:
        raise VLMError(
            f"VLM returned empty content and empty reasoning_content. "
            f"finish_reason={body['choices'][0].get('finish_reason')!r}, "
            f"usage={body.get('usage')!r}"
        )
    return content
