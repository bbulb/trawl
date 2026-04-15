"""HyDE — generate a hypothetical answer to the user query, then embed it.

A single small llama-server call. The output is fed to retrieval.retrieve()
as `extra_query_texts`, where it's averaged with the original query
embedding before scoring.

Why it helps: when the user asks "오늘 야구 경기 일정", the chunks containing
"두산 vs LG 18:30" share no tokens with the query and cosine similarity is
weak. A HyDE answer like "오늘 KBO 경기: 두산 vs LG 18:30, KIA vs 삼성..."
embeds much closer to those chunks.

**Off by default** in the pipeline (`pipeline.fetch_relevant(use_hyde=False)`).
The baseline retrieval already passes the full 11-case matrix; HyDE is kept
as a safety valve for future query classes that the baseline regresses on.

## Endpoint choice: utility LLM, not main LLM

The default endpoint is `localhost:8082` (a small utility llama-server,
Gemma 4 E4B in the reference setup), NOT the main :8080 llama-server.

Reasoning:
- The main :8080 typically runs a large model with a limited number
  of llama-server slots and is often actively used by another
  consumer (e.g. a chat agent with long tool loops). A trawl HyDE
  call would compete for a slot and potentially evict a live chat's
  KV cache on servers where the model has a known KV-cache-reuse
  issue — expensive and unnecessary for a 2-3 sentence HyDE answer.
- HyDE only needs 2-3 sentences of plausible text; a 4B utility model
  does it fine and is ~5x faster.
- `chat_template_kwargs.enable_thinking=False` is passed to suppress
  Gemma 4's reasoning mode. Without it the model burns the token budget
  on internal reasoning and leaves `content` empty. The reasoning_content
  fallback below stays as a safety net for servers where the kwarg is
  not honoured.

Override with env vars if your setup is different:
    TRAWL_HYDE_URL   (base URL of the /v1 endpoint)
    TRAWL_HYDE_MODEL (model name the endpoint expects)
    TRAWL_HYDE_SLOT  (pin to a specific llama-server slot for KV-cache reuse)
"""

from __future__ import annotations

import os

import httpx

DEFAULT_LLAMA_URL = os.environ.get("TRAWL_HYDE_URL", "http://localhost:8082/v1")
DEFAULT_MODEL = os.environ.get("TRAWL_HYDE_MODEL", "gemma")
# With `enable_thinking=False` the 4B utility model answers directly, no
# reasoning pass. 300 tokens is plenty for a 2-3 sentence hypothetical.
HYDE_MAX_TOKENS = 300
HTTP_TIMEOUT_S = 60.0
# Pin requests to a specific llama-server slot for KV-cache reuse.
# Set to an integer slot ID (e.g. "0") to avoid evicting other consumers'
# cached prompts on a shared server. Unset = let the server choose.
HYDE_SLOT_ID: int | None = int(v) if (v := os.environ.get("TRAWL_HYDE_SLOT")) is not None else None

PROMPT_TEMPLATE = (
    "Answer the following question in 2-3 sentences. "
    "Include specific named entities (people, places, dates, numbers) that "
    "would appear in a real answer. Korean or English both OK.\n\n"
    "Question: {query}\n\nAnswer:"
)


def expand(query: str, *, base_url: str = DEFAULT_LLAMA_URL, model: str = DEFAULT_MODEL) -> str:
    """Return a single-string hypothetical answer, or '' on failure.

    Sends `chat_template_kwargs.enable_thinking=False` to skip Gemma 4's
    reasoning pass. If the server doesn't honour that kwarg (older
    llama.cpp, different model) and `content` comes back empty, we fall
    back to `reasoning_content`, which in practice carries the same named
    entities we need for retrieval.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT_TEMPLATE.format(query=query)}],
        "max_tokens": HYDE_MAX_TOKENS,
        "temperature": 0.7,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if HYDE_SLOT_ID is not None:
        payload["id_slot"] = HYDE_SLOT_ID
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.post(f"{base_url}/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            msg = data["choices"][0]["message"]
            content = (msg.get("content") or "").strip()
            if not content:
                content = (msg.get("reasoning_content") or "").strip()
            return content
    except httpx.HTTPError:
        return ""
