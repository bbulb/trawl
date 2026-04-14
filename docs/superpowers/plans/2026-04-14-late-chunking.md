# Late Chunking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** trawl의 임베딩 단계에 Late Chunking을 **옵트인 토글**로 추가하고, `tests/test_pipeline_ranked.py`로 baseline vs late의 recall@k/MRR 을 직접 비교해 스파이크를 Go/No-go로 판정한다.

**Architecture:** 청킹 로직은 그대로 두고 `Chunk`에 `char_start/char_end`를 추가, 새 모듈 `src/trawl/late_chunking.py`가 전용 `:8084` 서버(`--pooling none`)에서 토큰 임베딩을 받아 per-chunk mean pool. 8K 초과 청크는 기존 `:8081` baseline 경로로 silent fallback. 서버 자체가 내려가 있으면 명시적 에러.

**Tech Stack:** Python 3.10+, `httpx` (이미 의존), stdlib `argparse`·`json`. 서버 사이드는 `llama-server` + `bge-m3-Q8_0.gguf` (기존 모델 파일 재사용).

**Spec:** [`docs/superpowers/specs/2026-04-14-late-chunking-design.md`](../specs/2026-04-14-late-chunking-design.md)

**Env rule:** 모든 python/pytest 명령은 `mamba run -n trawl` 접두어 필요 (또는 먼저 `mamba activate trawl`).

**Worktree:** 이 플랜을 실행할 때 subagent-driven-development 전에 `.worktrees/late-chunking/` worktree + `late-chunking` 브랜치 생성 권장 (wcxb 때와 동일 패턴). 스펙·플랜 파일은 이미 `develop`에 커밋되어 있어 worktree가 자동 상속.

---

## File structure

```
src/trawl/
  chunking.py              modified — Chunk에 char_start/char_end 필드 + 오프셋 기록
  late_chunking.py         NEW — embed_chunks_late() + 헬퍼 + LateChunkingServerDown
  retrieval.py             modified — retrieve(..., late_chunking, full_md)
  pipeline.py              modified — TRAWL_LATE_CHUNKING env + kwarg 통과

scripts/
  late_chunking_server.sh  NEW — llama-server :8084 start|stop|status

tests/
  test_chunking.py         NEW — Chunk.char_start/end 단위 테스트 (chunker는 현재 테스트 없음)
  test_late_chunking.py    NEW — httpx 모킹한 late chunking 단위 테스트
  test_pipeline_ranked.py  NEW — baseline/late A/B 러너 (recall@k, MRR)

docs/superpowers/specs/
  2026-04-14-late-chunking-results.md   NEW (Task 7) — 실측 결과 + go/no-go 판정
```

**Why `test_chunking.py` new:** 현 레포는 chunker 전용 유닛 테스트가 없고 `test_pipeline.py`가 통합 검증 담당. `char_start/end` 추가는 청커 내부 작은 변경이라 전용 유닛 테스트가 적합.

---

## Task 0: 서버 API shape 검증 — **controller + user**

**Purpose:** bge-m3 `--pooling none` 엔드포인트의 응답 shape을 실측 확인. 잘못 추정하면 Task 3이 통째로 다시 짜여야 함.

**Who:** 컨트롤러가 사용자 도움으로 수행. 서브에이전트에 맡기지 않음.

**Files:** 없음 (검증만).

- [ ] **Step 1: 사용자가 임시로 `:8084`에 `--pooling none` bge-m3 기동**

사용자가 수동으로:
```bash
llama-server --model <bge-m3-Q8_0.gguf path> --port 8084 \
  --embedding --pooling none --ubatch-size 2048 --ctx-size 8192
```
(VRAM 충돌 시 `:8081`·`:8083` 일시 내리고 진행.)

- [ ] **Step 2: Shape probe 실행 (controller)**

```bash
mamba run -n trawl python - <<'PY'
import httpx, json
base = "http://localhost:8084/v1"
# /tokenize
r = httpx.post(f"{base}/tokenize", json={"content": "hello world"})
print("TOKENIZE:", json.dumps(r.json(), indent=2)[:400])

# /embeddings with pooling=none
r = httpx.post(f"{base}/embeddings", json={
    "model": "bge-m3-Q8_0.gguf",
    "input": ["hello world"],
})
d = r.json()
e = d["data"][0]["embedding"]
print("EMBED type:", type(e).__name__,
      "outer_len:", len(e),
      "inner:", type(e[0]).__name__,
      "inner_len:" , len(e[0]) if isinstance(e[0], list) else "N/A")
PY
```

- [ ] **Step 3: 결과에 따라 분기**

기대되는 shape:
- `/tokenize` → `{"tokens": [int, ...]}` (char offsets는 llama.cpp 버전에 따라 있음/없음)
- `/embeddings` with pooling=none → 입력 토큰 수만큼 per-token 벡터 리스트 (e.g. `embedding: list[list[float]]`, outer_len = tokens, inner_len = hidden dim)

**다른 shape 나오면** → 컨트롤러가 플랜 Task 3 사양을 실측 shape에 맞춰 갱신 후 진행.

- [ ] **Step 4: 사용자는 서버 종료해도 좋음 (Task 7 실측 때 다시 띄울 것)**

컨트롤러가 검증 결과를 이 플랜 하단 "Task 0 findings" 블록에 기록.

**Task 0 findings** (컨트롤러가 Step 2~3 후 채움):

```
<to be filled after Step 2>
```

---

## Task 1: `Chunk`에 `char_start` / `char_end` 필드 추가

**Purpose:** Late chunking이 각 청크의 토큰 span을 찾으려면 원본 `md` 문자열에서 청크의 절대 offset이 필요. 지금은 `chunk.text`만 있고 offset 없음.

**Files:**
- Modify: `src/trawl/chunking.py`
- Create: `tests/test_chunking.py`

- [ ] **Step 1: 실패 테스트 작성**

File: `tests/test_chunking.py`

```python
"""Unit tests for Chunk char offsets added for late chunking support."""

from trawl.chunking import Chunk, chunk_markdown


def test_chunk_dataclass_has_char_offsets():
    c = Chunk(text="hello", char_start=3, char_end=8)
    assert c.char_start == 3
    assert c.char_end == 8


def test_chunk_markdown_records_absolute_char_offsets():
    md = "# Section A\n\nFirst paragraph here.\n\n# Section B\n\nSecond paragraph here."
    chunks = chunk_markdown(md, max_chars=1500)

    # 모든 청크는 md 절대 offset을 가져야 함
    for c in chunks:
        assert 0 <= c.char_start < c.char_end <= len(md)
        # text는 md 절대 범위와 일치(또는 공백만 strip된 부분 집합)해야 한다.
        # chunk_markdown이 text에 strip()을 적용하므로,
        # md[c.char_start:c.char_end] 안에 c.text가 실제로 포함되어야 함.
        assert c.text in md[c.char_start:c.char_end]


def test_chunk_markdown_offsets_are_monotonic_non_overlapping():
    md = "# A\n\nfoo.\n\n# B\n\nbar baz qux.\n\n# C\n\nfinal."
    chunks = chunk_markdown(md, max_chars=1500)
    for prev, cur in zip(chunks, chunks[1:]):
        assert prev.char_end <= cur.char_start


def test_chunk_markdown_empty_input_returns_empty():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
mamba run -n trawl pytest tests/test_chunking.py -v
```
Expected: `TypeError: Chunk() takes no argument 'char_start'` 또는 유사.

- [ ] **Step 3: `chunking.py` 수정 — Chunk에 필드 추가**

`src/trawl/chunking.py`의 `Chunk` dataclass(현재 line 25~35 부근)를 다음으로 교체:

```python
@dataclass
class Chunk:
    text: str  # original markdown, shown to user / passed to LLM
    heading_path: list[str] = field(default_factory=list)
    char_count: int = 0
    chunk_index: int = 0
    embed_text: str = ""  # markdown-stripped version used for embedding
    char_start: int = 0   # absolute offset into the original `md` input
    char_end: int = 0     # exclusive end offset

    @property
    def heading(self) -> str:
        return " > ".join(self.heading_path) if self.heading_path else ""
```

- [ ] **Step 4: `chunk_markdown` + `_split_by_headings` + `_split_section` 에서 offset 기록**

`chunking.py`를 다음 순서로 고친다:

(a) `_split_by_headings` 반환 타입을 `list[tuple[list[str], str, int]]` 로 바꿔 섹션 body의 시작 offset을 같이 리턴.

현재:
```python
def _split_by_headings(md: str) -> list[tuple[list[str], str]]:
```
→
```python
def _split_by_headings(md: str) -> list[tuple[list[str], str, int]]:
    """Walk markdown line-by-line, return [(heading_path, body, body_start_offset), ...]."""
    lines = md.split("\n")
    sections: list[tuple[list[str], str, int]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    buf_start = 0
    cursor = 0  # absolute char offset being consumed

    def flush() -> None:
        if buf:
            path = [t for _, t in stack]
            sections.append((path, "\n".join(buf), buf_start))

    for line in lines:
        line_len = len(line) + 1  # +1 for the \n we split on
        m = HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            buf_start = cursor + line_len  # body starts AFTER the heading line
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            if not buf:
                buf_start = cursor
            buf.append(line)
        cursor += line_len

    flush()
    if not sections:
        sections = [([], md, 0)]
    return sections
```

(b) `_split_section`도 시작 offset을 받아 각 piece의 절대 (start, end) 를 리턴:

```python
def _split_section(body: str, *, max_chars: int, body_start: int) -> list[tuple[str, int, int]]:
    """Split `body` (already starting at `body_start` in the original md)
    into pieces ≤ max_chars. Each piece returned as (text, abs_start, abs_end)."""
    if len(body) <= max_chars:
        return [(body, body_start, body_start + len(body))]

    lines = body.split("\n")
    pieces: list[tuple[str, int, int]] = []
    current: list[str] = []
    current_len = 0
    current_start = body_start
    cursor = body_start
    in_table = False

    def push(end_offset: int) -> None:
        nonlocal current, current_len, current_start
        if current:
            joined = "\n".join(current)
            pieces.append((joined.strip(), current_start, end_offset))
            current = []
            current_len = 0
            current_start = end_offset

    for line in lines:
        if TABLE_LINE_RE.match(line):
            in_table = True
        elif in_table and not TABLE_LINE_RE.match(line):
            in_table = False

        line_len = len(line) + 1  # include newline

        # Oversized non-table line → split it via _split_long_line, but we
        # can't meaningfully attribute sub-pieces to individual offsets,
        # so we emit the WHOLE long-line region as one big piece with its
        # overall offsets. (Late chunking's per-chunk pooling still works —
        # it just pools over the long line's tokens.)
        if len(line) > max_chars and not in_table:
            push(cursor)
            sub = _split_long_line(line, max_chars=max_chars)
            # Approximate offsets: distribute proportionally by cumulative char
            # count inside `line`. The sum of sub-piece char counts (+ separator
            # bytes between them in joined form) equals len(line).
            sub_cursor = cursor
            for s in sub:
                s_len = len(s)
                pieces.append((s.strip(), sub_cursor, sub_cursor + s_len))
                sub_cursor += s_len
            cursor += line_len
            current_start = cursor
            continue

        if current_len + line_len > max_chars and not in_table and current:
            push(cursor)
            current_start = cursor

        current.append(line)
        current_len += line_len
        cursor += line_len

    push(cursor)
    return pieces
```

(c) `chunk_markdown`에서 새 시그니처 사용 + `Chunk.char_start/char_end` 기록:

```python
def chunk_markdown(md: str, *, max_chars: int | None = None) -> list[Chunk]:
    if max_chars is None:
        max_chars = 900 if len(md) < 20_000 else 450
    if not md.strip():
        return []

    sections = _split_by_headings(md)
    chunks: list[Chunk] = []
    idx = 0
    for heading_path, body, body_start in sections:
        for text_raw, start, end in _split_section(body, max_chars=max_chars, body_start=body_start):
            text = text_raw.strip()
            if not text:
                continue
            embed = plain_text(text)
            if len(embed) < MIN_PLAIN_CHARS:
                continue
            chunks.append(
                Chunk(
                    text=text,
                    heading_path=heading_path,
                    char_count=len(text),
                    chunk_index=idx,
                    embed_text=embed,
                    char_start=start,
                    char_end=end,
                ),
            )
            idx += 1
    return chunks
```

- [ ] **Step 5: 테스트 재실행**

```bash
mamba run -n trawl pytest tests/test_chunking.py -v
```
Expected: 4/4 pass.

- [ ] **Step 6: 기존 테스트가 깨지지 않는지 확인 (서버 필요 없는 것만)**

```bash
mamba run -n trawl pytest tests/test_profiles.py tests/test_github_fetcher.py -v 2>&1 | tail -5
```
Expected: 기존 테스트 통과 (offset 필드는 추가만이고 default=0이라 회귀 없음).

**NOTE**: parity matrix(`test_pipeline.py`)는 임베딩 서버 필요 → Task 6에서 full 통합 돌림.

- [ ] **Step 7: 커밋**

```bash
git add src/trawl/chunking.py tests/test_chunking.py
git commit -m "feat(chunking): record absolute char offsets on Chunk for late chunking"
```

---

## Task 2: 서버 기동 스크립트 `scripts/late_chunking_server.sh`

**Purpose:** `:8084`에 `--pooling none` bge-m3 인스턴스를 올리고 내리는 스크립트. PID/log 파일로 재시작 후에도 상태 추적.

**Files:**
- Create: `scripts/late_chunking_server.sh`

- [ ] **Step 1: 스크립트 작성**

File: `scripts/late_chunking_server.sh`

```bash
#!/usr/bin/env bash
# Start / stop / status the dedicated bge-m3 llama-server used by late
# chunking. Runs on :8084 with --pooling none so we get per-token vectors.
# See CLAUDE.md "Resource juggling" for when to run this.

set -euo pipefail

PORT="${TRAWL_LATE_PORT:-8084}"
MODEL="${TRAWL_BGE_M3_GGUF:-$HOME/.cache/llama/bge-m3-Q8_0.gguf}"
PID_FILE="/tmp/trawl_late_chunking.pid"
LOG_FILE="/tmp/trawl_late_chunking.log"

cmd="${1:-status}"

case "$cmd" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "already running (pid $(cat "$PID_FILE"))" >&2
      exit 0
    fi
    if [ ! -f "$MODEL" ]; then
      echo "model not found: $MODEL" >&2
      echo "set TRAWL_BGE_M3_GGUF or adjust the default in this script" >&2
      exit 1
    fi
    nohup llama-server \
      --model "$MODEL" \
      --port "$PORT" \
      --embedding \
      --pooling none \
      --ubatch-size 2048 \
      --ctx-size 8192 \
      > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    # Poll /health for up to 10s
    for i in $(seq 1 20); do
      if curl -sf "http://localhost:$PORT/health" > /dev/null; then
        echo "up on :$PORT (pid $(cat "$PID_FILE"))"
        exit 0
      fi
      sleep 0.5
    done
    echo "failed to start — tail of $LOG_FILE:" >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    exit 1
    ;;

  stop)
    if [ ! -f "$PID_FILE" ]; then
      echo "not running (no pid file)" >&2
      exit 0
    fi
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid"
      # Wait up to 5s for graceful shutdown
      for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
          break
        fi
        sleep 0.5
      done
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid"
      fi
    fi
    rm -f "$PID_FILE"
    echo "stopped"
    ;;

  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      pid=$(cat "$PID_FILE")
      if curl -sf "http://localhost:$PORT/health" > /dev/null; then
        echo "up (pid $pid, :$PORT)"
      else
        echo "pid $pid alive but :$PORT not responding"
        exit 1
      fi
    else
      echo "down"
      exit 1
    fi
    ;;

  *)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
```

- [ ] **Step 2: 실행 권한 부여 + 형식 검증**

```bash
chmod +x scripts/late_chunking_server.sh
scripts/late_chunking_server.sh 2>&1 | head -3
```
Expected: `usage: scripts/late_chunking_server.sh {start|stop|status}` 에러 메시지.

```bash
scripts/late_chunking_server.sh status 2>&1
```
Expected: `down` 또는 이전에 켜뒀다면 `up (pid ...)`. exit code 0 또는 1.

- [ ] **Step 3: (선택) 실 기동 테스트 — 사용자 승인 하에**

실제로 서버를 켜보고 싶다면:
```bash
scripts/late_chunking_server.sh start
scripts/late_chunking_server.sh status
scripts/late_chunking_server.sh stop
```
이 단계는 사용자에게 "괜찮으신가요?" 묻고 진행. 실패해도 무시하고 커밋 — Task 6 live run에서 다시 돌림.

- [ ] **Step 4: 커밋**

```bash
git add scripts/late_chunking_server.sh
git commit -m "feat(late-chunking): add llama-server :8084 launcher script"
```

---

## Task 3: `src/trawl/late_chunking.py` — 핵심 알고리즘

**Files:**
- Create: `src/trawl/late_chunking.py`
- Create: `tests/test_late_chunking.py`

**Context:** 이 태스크는 `/tokenize` + `/v1/embeddings` 응답을 httpx-mock으로 흉내낸 단위 테스트만 다룬다. 실 서버 연동 검증은 Task 6에서 수행.

- [ ] **Step 1: 실패 테스트 작성**

File: `tests/test_late_chunking.py`

```python
"""Unit tests for late_chunking.embed_chunks_late with mocked HTTP."""

from unittest.mock import patch, MagicMock

import pytest

from trawl.chunking import Chunk
from trawl.late_chunking import (
    LateChunkingServerDown,
    embed_chunks_late,
    _map_char_range_to_token_range,
)


def _mk_chunk(text: str, start: int, end: int, idx: int = 0) -> Chunk:
    return Chunk(
        text=text,
        char_count=len(text),
        chunk_index=idx,
        embed_text=text,
        char_start=start,
        char_end=end,
    )


def _mock_client(tokenize_json: dict, embeddings_json: dict):
    """Build a MagicMock httpx.Client context-manager returning the given
    JSON payloads for /tokenize and /v1/embeddings respectively."""
    client = MagicMock()

    def post(url, json=None, **kwargs):
        resp = MagicMock()
        if url.endswith("/tokenize"):
            resp.json.return_value = tokenize_json
        elif url.endswith("/embeddings"):
            resp.json.return_value = embeddings_json
        else:
            raise AssertionError(f"unexpected URL: {url}")
        resp.raise_for_status = MagicMock()
        return resp

    client.post.side_effect = post
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


def test_map_char_range_simple():
    # 3 tokens, each mapped to 4-char span starting at offsets [0, 4, 8]
    offsets = [(0, 4), (4, 8), (8, 12)]
    tok_start, tok_end = _map_char_range_to_token_range(offsets, 4, 12)
    assert tok_start == 1
    assert tok_end == 3   # exclusive


def test_map_char_range_fully_inside_single_token():
    offsets = [(0, 10), (10, 20)]
    tok_start, tok_end = _map_char_range_to_token_range(offsets, 2, 5)
    assert tok_start == 0
    assert tok_end == 1


def test_embed_chunks_late_pools_tokens_in_chunk_range():
    md = "aaaa bbbb cccc"
    chunks = [
        _mk_chunk("aaaa", 0, 4),
        _mk_chunk("bbbb", 5, 9),
        _mk_chunk("cccc", 10, 14),
    ]
    # 3 tokens with offsets matching each chunk
    tokenize_json = {
        "tokens": [1, 2, 3],
        "offsets": [[0, 4], [5, 9], [10, 14]],
    }
    # pooling=none → one vector per token
    embeddings_json = {
        "data": [{
            "embedding": [
                [1.0, 0.0],   # token 0
                [0.0, 1.0],   # token 1
                [1.0, 1.0],   # token 2
            ],
        }],
    }

    fake_client = _mock_client(tokenize_json, embeddings_json)
    with patch("trawl.late_chunking.httpx.Client", return_value=fake_client):
        vecs = embed_chunks_late(
            chunks, md,
            late_base_url="http://x/v1",
            baseline_base_url="http://y/v1",
            model="m",
        )

    assert len(vecs) == 3
    assert vecs[0] == [1.0, 0.0]
    assert vecs[1] == [0.0, 1.0]
    assert vecs[2] == [1.0, 1.0]


def test_embed_chunks_late_raises_when_server_unreachable():
    chunks = [_mk_chunk("hi", 0, 2)]
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.post.side_effect = ConnectionError("refused")
    with patch("trawl.late_chunking.httpx.Client", return_value=client):
        with pytest.raises(LateChunkingServerDown):
            embed_chunks_late(
                chunks, "hi",
                late_base_url="http://x/v1",
                baseline_base_url="http://y/v1",
                model="m",
            )


def test_embed_chunks_late_falls_back_for_chunks_past_8k():
    """Chunks whose char_start exceeds the 8K truncation window get
    silently embedded by the baseline path."""
    # Build synthetic md with one chunk at offset 0 and one at 20000.
    md = "x" * 25000
    chunks = [
        _mk_chunk("head", 0, 4),
        _mk_chunk("tail", 20000, 20004),
    ]
    # /tokenize returns 8192 tokens all within head region (hypothetical)
    tokenize_json = {
        "tokens": list(range(8192)),
        "offsets": [[i, i + 1] for i in range(8192)],
    }
    embeddings_json = {
        "data": [{"embedding": [[0.1, 0.2]] * 8192}],
    }
    # baseline endpoint returns 1 vector for the single tail chunk
    baseline_json = {"data": [{"embedding": [0.9, 0.9]}]}

    late_client = _mock_client(tokenize_json, embeddings_json)

    def client_factory(*args, **kwargs):
        # Return late_client on first call; a baseline mock on second.
        if not hasattr(client_factory, "called"):
            client_factory.called = True
            return late_client
        baseline = MagicMock()
        baseline.__enter__.return_value = baseline
        baseline.__exit__.return_value = False
        resp = MagicMock()
        resp.json.return_value = baseline_json
        resp.raise_for_status = MagicMock()
        baseline.post.return_value = resp
        return baseline

    with patch("trawl.late_chunking.httpx.Client", side_effect=client_factory):
        vecs = embed_chunks_late(
            chunks, md,
            late_base_url="http://x/v1",
            baseline_base_url="http://y/v1",
            model="m",
        )

    assert len(vecs) == 2
    # head was late-chunked (pool of first 4 tokens = same vector)
    assert vecs[0] == [0.1, 0.2]
    # tail used baseline fallback
    assert vecs[1] == [0.9, 0.9]
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

```bash
mamba run -n trawl pytest tests/test_late_chunking.py -v
```
Expected: `ModuleNotFoundError: trawl.late_chunking`.

- [ ] **Step 3: `late_chunking.py` 구현**

File: `src/trawl/late_chunking.py`

```python
"""Late chunking for trawl — experimental embedding path.

Forward-pass the whole document once against a bge-m3 instance running
with --pooling none (per-token hidden states), then mean-pool per chunk
over its token span. Chunks whose char range falls past the 8,192-token
truncation cap fall back silently to the baseline (chunk-solo) embedding
path at TRAWL_EMBED_URL.

Server-level absence (late server unreachable, wrong pooling mode,
/tokenize failure) raises LateChunkingServerDown explicitly — NEVER
silent — to protect measurement integrity.
"""

from __future__ import annotations

import sys

import httpx

from .chunking import Chunk


TOKEN_CAP = 8192
HTTP_TIMEOUT_S = 60.0


class LateChunkingServerDown(RuntimeError):
    """The dedicated late-chunking server (`:8084`) is unreachable or
    misconfigured. Raised once at the top of embed_chunks_late; callers
    should surface this rather than fall back silently."""


def _map_char_range_to_token_range(
    offsets: list[tuple[int, int] | list[int]],
    char_start: int,
    char_end: int,
) -> tuple[int, int]:
    """Return (token_start, token_end_exclusive) for the smallest token
    window that covers [char_start, char_end).

    A token is "in range" if its offset span intersects the char range.
    """
    first: int | None = None
    last: int = -1
    for i, off in enumerate(offsets):
        o0, o1 = off[0], off[1]
        if o1 <= char_start:
            continue
        if o0 >= char_end:
            break
        if first is None:
            first = i
        last = i
    if first is None:
        return (0, 0)
    return (first, last + 1)


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    out = [0.0] * dim
    for v in vectors:
        for j in range(dim):
            out[j] += v[j]
    n = float(len(vectors))
    return [x / n for x in out]


def _baseline_embed_one(
    chunk: Chunk,
    *,
    baseline_base_url: str,
    model: str,
) -> list[float]:
    """Fall back to the baseline (chunk-solo mean-pool) path for a single
    chunk. Mirrors retrieval._embed_batch but for one input."""
    text = chunk.embed_text or chunk.text
    if chunk.heading_path:
        text = " > ".join(chunk.heading_path) + "\n\n" + text
    with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
        r = client.post(
            f"{baseline_base_url}/embeddings",
            json={"model": model, "input": [text]},
        )
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]


def embed_chunks_late(
    chunks: list[Chunk],
    full_md: str,
    *,
    late_base_url: str,
    baseline_base_url: str,
    model: str,
) -> list[list[float]]:
    """Embed chunks via late chunking. See module docstring."""
    if not chunks:
        return []

    # Single forward pass for the whole doc — server returns per-token vectors.
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            tok_resp = client.post(
                f"{late_base_url}/tokenize",
                json={"content": full_md, "with_pieces": True},
            )
            tok_resp.raise_for_status()
            tok_json = tok_resp.json()

            tokens = tok_json.get("tokens") or []
            offsets = tok_json.get("offsets")
            if offsets is None:
                raise LateChunkingServerDown(
                    "late-chunking /tokenize did not return offsets; "
                    "need a llama-server build with `with_pieces` support"
                )

            # Truncate to TOKEN_CAP. We keep the prefix md up to the last
            # char covered by the last retained token.
            if len(tokens) > TOKEN_CAP:
                capped_offsets = offsets[:TOKEN_CAP]
                cap_char = capped_offsets[-1][1]
                input_text = full_md[:cap_char]
                active_offsets = capped_offsets
            else:
                input_text = full_md
                active_offsets = offsets

            emb_resp = client.post(
                f"{late_base_url}/embeddings",
                json={"model": model, "input": [input_text]},
            )
            emb_resp.raise_for_status()
            emb_data = emb_resp.json()["data"][0]["embedding"]

    except (httpx.HTTPError, ConnectionError, OSError) as exc:
        raise LateChunkingServerDown(f"late server unreachable: {exc}") from exc

    # Validate: per-token vectors (list of lists), one per token we sent.
    if not isinstance(emb_data, list) or not emb_data or not isinstance(emb_data[0], list):
        raise LateChunkingServerDown(
            "late /embeddings did not return per-token vectors; is the server "
            "running with --pooling none?"
        )
    if len(emb_data) != len(active_offsets):
        # Servers sometimes prepend BOS/EOS tokens not included in /tokenize.
        # Accept small mismatch by trimming.
        if abs(len(emb_data) - len(active_offsets)) <= 2 and len(emb_data) >= len(active_offsets):
            emb_data = emb_data[: len(active_offsets)]
        else:
            raise LateChunkingServerDown(
                f"token/embedding count mismatch: tokens={len(active_offsets)} "
                f"embeddings={len(emb_data)}"
            )

    cap_char_end = active_offsets[-1][1] if active_offsets else 0

    vectors: list[list[float]] = []
    fallback_count = 0
    for chunk in chunks:
        # If chunk range is fully within cap → pool.
        if chunk.char_end <= cap_char_end:
            ts, te = _map_char_range_to_token_range(
                active_offsets, chunk.char_start, chunk.char_end
            )
            if te > ts:
                vectors.append(_mean_pool(emb_data[ts:te]))
                continue
        # Otherwise fall back to baseline embedding for this chunk.
        vectors.append(
            _baseline_embed_one(
                chunk, baseline_base_url=baseline_base_url, model=model
            )
        )
        fallback_count += 1

    if fallback_count:
        print(
            f"[late_chunking] {fallback_count}/{len(chunks)} chunks fell back "
            f"to baseline (past 8K token cap)",
            file=sys.stderr,
        )
    return vectors
```

- [ ] **Step 4: 테스트 통과 확인**

```bash
mamba run -n trawl pytest tests/test_late_chunking.py -v
```
Expected: 5/5 pass. 실패 케이스 분석:
- `offsets` 필드가 없는 응답을 가정한 경우 테스트가 통과해야 함. mock이 `offsets`를 주도록 만들었으니 통과해야 함.
- "fall-back" 테스트가 예상 vector를 정확히 반환하는지 확인.

실패 시 mock 설정 문제 가능성 — 실제 서버가 어떻게 응답할지는 Task 6에서 확정.

- [ ] **Step 5: 커밋**

```bash
git add src/trawl/late_chunking.py tests/test_late_chunking.py
git commit -m "feat(late-chunking): add late_chunking module with token-range pooling"
```

---

## Task 4: `retrieval.py` + `pipeline.py` 토글 배선

**Files:**
- Modify: `src/trawl/retrieval.py`
- Modify: `src/trawl/pipeline.py`

- [ ] **Step 1: 실패 테스트 작성 (retrieval 토글)**

`tests/test_late_chunking.py` 하단에 추가:

```python
from trawl.retrieval import retrieve


def test_retrieve_late_chunking_requires_full_md():
    import pytest
    with pytest.raises(ValueError, match="full_md"):
        retrieve("q", [_mk_chunk("hi", 0, 2)], late_chunking=True, full_md="")
```

```bash
mamba run -n trawl pytest tests/test_late_chunking.py::test_retrieve_late_chunking_requires_full_md -v
```
Expected: `TypeError: unexpected keyword 'late_chunking'`.

- [ ] **Step 2: `retrieval.py` 수정 — 시그니처 + 분기**

`src/trawl/retrieval.py`의 `retrieve()` 함수 교체 (전체 함수를 아래로):

```python
def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
    late_chunking: bool = False,
    late_base_url: str | None = None,
    full_md: str = "",
) -> RetrievalResult:
    """Embed query + chunks, return the top-k chunks by cosine similarity.

    When `late_chunking=True`, chunks are embedded via late_chunking module
    against `late_base_url` (default: TRAWL_LATE_EMBED_URL env or
    localhost:8084/v1). `full_md` MUST be provided so the module can do a
    whole-doc forward pass.
    """
    if late_chunking and not full_md:
        raise ValueError(
            "retrieve() needs full_md when late_chunking=True"
        )
    if not chunks:
        return RetrievalResult(scored=[], elapsed_ms=0, embed_calls=0)

    t0 = time.monotonic()
    embed_calls = 0
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            query_inputs = [query]
            if extra_query_texts:
                query_inputs.extend(extra_query_texts)
            q_embs = _embed_batch(client, base_url, model, query_inputs)
            embed_calls += 1

            if late_chunking:
                from .late_chunking import embed_chunks_late
                late_url = late_base_url or os.environ.get(
                    "TRAWL_LATE_EMBED_URL", "http://localhost:8084/v1"
                )
                chunk_embs = embed_chunks_late(
                    chunks, full_md,
                    late_base_url=late_url,
                    baseline_base_url=base_url,
                    model=model,
                )
                embed_calls += 1   # approximate: one big call
            else:
                chunk_texts = [
                    (c.heading + "\n\n" + (c.embed_text or c.text))
                    if c.heading
                    else (c.embed_text or c.text)
                    for c in chunks
                ]
                chunk_embs = []
                for start in range(0, len(chunk_texts), EMBEDDING_BATCH):
                    batch = chunk_texts[start : start + EMBEDDING_BATCH]
                    chunk_embs.extend(_embed_batch(client, base_url, model, batch))
                    embed_calls += 1
    except httpx.HTTPError as e:
        return RetrievalResult(
            scored=[],
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            embed_calls=embed_calls,
            error=f"{type(e).__name__}: {e}",
        )

    avg_q = [sum(col) / len(col) for col in zip(*q_embs, strict=True)]
    scored = [
        ScoredChunk(chunk=c, score=cosine(avg_q, ce))
        for c, ce in zip(chunks, chunk_embs, strict=True)
    ]
    scored.sort(key=lambda s: -s.score)

    return RetrievalResult(
        scored=scored[:k],
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        embed_calls=embed_calls,
    )
```

- [ ] **Step 3: `pipeline.py` 수정 — env + kwarg 통과**

먼저 `pipeline.py`의 `fetch_relevant` 시그니처와 `retrieve()` 호출 지점을 확인:
```bash
grep -n "def fetch_relevant\|retrieve(" src/trawl/pipeline.py
```

`fetch_relevant(...)` 시그니처에 다음 매개변수 추가:
```python
    late_chunking: bool | None = None,
```
함수 본문 상단에서 기본값 해석:
```python
    if late_chunking is None:
        env = os.environ.get("TRAWL_LATE_CHUNKING", "").lower()
        late_chunking = env in ("1", "true", "yes", "on")
```
`retrieve(...)` 호출에 다음 두 키워드 추가:
```python
        late_chunking=late_chunking,
        full_md=markdown,   # or whatever variable holds the extracted md
```
변수명은 실제 pipeline.py 내부에서 확인(추출된 마크다운을 담는 변수).

- [ ] **Step 4: 테스트 통과 확인**

```bash
mamba run -n trawl pytest tests/test_late_chunking.py -v
```
Expected: 6/6 pass (기존 5 + 새 1).

기존 테스트 회귀 없는지 간단 확인:
```bash
mamba run -n trawl pytest tests/test_profiles.py -v 2>&1 | tail -3
```
Expected: 통과.

- [ ] **Step 5: 커밋**

```bash
git add src/trawl/retrieval.py src/trawl/pipeline.py tests/test_late_chunking.py
git commit -m "feat(retrieval): wire late_chunking toggle through pipeline"
```

---

## Task 5: 랭크 측정 러너 `tests/test_pipeline_ranked.py`

**Purpose:** 기존 `test_pipeline.py`(parity, pass/fail)를 대체하지 않고 **옆에** 추가. 같은 `test_cases.yaml`을 읽어 rule별 첫 매칭 rank를 기록, MRR과 recall@k 계산. `--both` 모드가 baseline/late 두 번 돌려 A/B 출력.

**Files:**
- Create: `tests/test_pipeline_ranked.py`

- [ ] **Step 1: 기존 파이프라인 인터페이스 확인**

```bash
grep -n "def fetch_relevant\|chunks\|ScoredChunk" src/trawl/pipeline.py src/trawl/retrieval.py | head -20
```

`fetch_relevant()`의 반환 형태(`chunks` 필드에 top-k 청크 리스트)를 확인. 러너는 이 결과에서 rule별로 어느 청크에 정답이 나오는지 스캔.

- [ ] **Step 2: 러너 작성 (실행 가능한 전체 파일)**

File: `tests/test_pipeline_ranked.py`

```python
"""Ranked A/B runner for the trawl parity matrix.

Unlike test_pipeline.py (binary pass/fail), this runner scores each case's
ground-truth rules by the rank of the chunk that first matches them. Useful
for measuring small ranking shifts between embedding-path variants
(specifically: late chunking vs baseline).

Usage:
    python tests/test_pipeline_ranked.py                # baseline only
    python tests/test_pipeline_ranked.py --late         # late only
    python tests/test_pipeline_ranked.py --both         # A/B
    python tests/test_pipeline_ranked.py --only <id>    # restrict to one case
    python tests/test_pipeline_ranked.py --out <dir>    # override output dir
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path

import yaml  # already a trawl dependency

from trawl import fetch_relevant


ROOT = Path(__file__).parent
CASES_PATH = ROOT / "test_cases.yaml"


def _load_cases() -> list[dict]:
    return yaml.safe_load(CASES_PATH.read_text())["cases"]


def _first_hit_rank(chunks: list[str], needle: str | None = None,
                    pattern: str | None = None) -> int | None:
    """Return 1-based rank of the first chunk matching `needle` or `pattern`.
    Returns None if no match in top-k."""
    for i, c in enumerate(chunks, start=1):
        if needle is not None and needle in c:
            return i
        if pattern is not None and re.search(pattern, c):
            return i
    return None


def _score_case(case: dict, chunk_texts: list[str]) -> dict:
    """Score one case's ground truth rules against returned chunks."""
    gt = case.get("ground_truth") or {}
    rules: list[dict] = []

    # must_contain_all → each string is its own rule (all must hit)
    for s in gt.get("must_contain_all", []) or []:
        rules.append({
            "rule": f"must_contain_all '{s}'",
            "first_hit_rank": _first_hit_rank(chunk_texts, needle=s),
        })
    # must_contain_any → rank of any hit
    any_strs = gt.get("must_contain_any") or []
    if any_strs:
        ranks = [_first_hit_rank(chunk_texts, needle=s) for s in any_strs]
        present = [r for r in ranks if r is not None]
        rules.append({
            "rule": f"must_contain_any ({len(any_strs)} options)",
            "first_hit_rank": min(present) if present else None,
        })
    # must_contain_any_2 (same semantics, different key in yaml)
    any2 = gt.get("must_contain_any_2") or []
    if any2:
        ranks = [_first_hit_rank(chunk_texts, needle=s) for s in any2]
        present = [r for r in ranks if r is not None]
        rules.append({
            "rule": f"must_contain_any_2 ({len(any2)} options)",
            "first_hit_rank": min(present) if present else None,
        })
    # must_contain_pattern → regex
    for p in ([gt["must_contain_pattern"]] if "must_contain_pattern" in gt else []):
        rules.append({
            "rule": f"must_contain_pattern r'{p}'",
            "first_hit_rank": _first_hit_rank(chunk_texts, pattern=p),
        })

    # Case-level metrics. Ignore rules with no hit for MRR (counted in recall).
    ranks = [r["first_hit_rank"] for r in rules]
    hits_at = {k: sum(1 for r in ranks if r is not None and r <= k) for k in (1, 3, 5)}
    mrr = sum(1.0 / r for r in ranks if r) / len(ranks) if ranks else 0.0

    return {
        "id": case["id"],
        "rules": rules,
        "recall_at": hits_at,
        "rule_count": len(ranks),
        "mrr": mrr,
    }


def _run_one_mode(cases: list[dict], late: bool, only: str | None) -> list[dict]:
    results = []
    for case in cases:
        if only and case["id"] != only:
            continue
        print(f"  [{case['id']}] {'late' if late else 'base'} ...",
              file=sys.stderr, end="", flush=True)
        t0 = time.monotonic()
        try:
            r = fetch_relevant(case["url"], case["query"], late_chunking=late)
            chunk_texts = [c.text for c in r.chunks]
            scored = _score_case(case, chunk_texts)
            scored["error"] = None
            scored["latency_ms"] = int((time.monotonic() - t0) * 1000)
        except Exception as exc:
            scored = {
                "id": case["id"],
                "rules": [],
                "recall_at": {1: 0, 3: 0, 5: 0},
                "rule_count": 0,
                "mrr": 0.0,
                "error": f"{type(exc).__name__}: {exc}",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        print(f" mrr={scored['mrr']:.3f} ({scored['latency_ms']}ms)", file=sys.stderr)
        results.append(scored)
    return results


def _overall(results: list[dict]) -> dict:
    usable = [r for r in results if r["error"] is None and r["rule_count"]]
    if not usable:
        return {"n": 0, "mrr": 0.0, "recall_at_1": 0, "recall_at_3": 0, "recall_at_5": 0}
    all_ranks = [rule["first_hit_rank"] for r in usable for rule in r["rules"]]
    hits = {k: sum(1 for x in all_ranks if x is not None and x <= k) for k in (1, 3, 5)}
    denom = len(all_ranks)
    mrr = sum(1.0 / r for r in all_ranks if r) / denom if denom else 0.0
    return {
        "n_cases": len(usable),
        "n_rules": denom,
        "mrr": mrr,
        "recall_at_1": hits[1] / denom,
        "recall_at_3": hits[3] / denom,
        "recall_at_5": hits[5] / denom,
        "median_latency_ms": sorted(r["latency_ms"] for r in usable)[len(usable) // 2],
    }


def _render_summary(base: list[dict] | None, late: list[dict] | None) -> str:
    lines = ["# Ranked runner — A/B summary", ""]
    if base is not None and late is not None:
        bo, lo = _overall(base), _overall(late)
        lines += [
            f"N cases: {bo['n_cases']}, N rules: {bo['n_rules']}",
            "",
            "| metric         | base   | late   | Δ       |",
            "|----------------|--------|--------|---------|",
            f"| MRR            | {bo['mrr']:.3f}  | {lo['mrr']:.3f}  | {lo['mrr']-bo['mrr']:+.3f} |",
            f"| recall@1       | {bo['recall_at_1']:.3f}  | {lo['recall_at_1']:.3f}  | {lo['recall_at_1']-bo['recall_at_1']:+.3f} |",
            f"| recall@3       | {bo['recall_at_3']:.3f}  | {lo['recall_at_3']:.3f}  | {lo['recall_at_3']-bo['recall_at_3']:+.3f} |",
            f"| recall@5       | {bo['recall_at_5']:.3f}  | {lo['recall_at_5']:.3f}  | {lo['recall_at_5']-bo['recall_at_5']:+.3f} |",
            f"| median latency | {bo['median_latency_ms']}ms | {lo['median_latency_ms']}ms | {lo['median_latency_ms']-bo['median_latency_ms']:+d}ms |",
            "",
            "## Per-case",
            "",
            "| id | base MRR | late MRR | Δ | base rec@5 | late rec@5 |",
            "|----|----------|----------|---|------------|------------|",
        ]
        by_id_base = {r["id"]: r for r in base}
        by_id_late = {r["id"]: r for r in late}
        for cid in sorted(set(by_id_base) | set(by_id_late)):
            b = by_id_base.get(cid, {})
            l = by_id_late.get(cid, {})
            def r5(r):
                return f"{r.get('recall_at',{}).get(5,0)}/{r.get('rule_count',0)}"
            lines.append(
                f"| {cid} | {b.get('mrr',0):.3f} | {l.get('mrr',0):.3f} "
                f"| {l.get('mrr',0)-b.get('mrr',0):+.3f} "
                f"| {r5(b)} | {r5(l)} |"
            )
    else:
        only = base if base is not None else late
        label = "base" if base is not None else "late"
        o = _overall(only)
        lines += [
            f"Mode: {label}, N cases: {o['n_cases']}, N rules: {o['n_rules']}",
            f"MRR={o['mrr']:.3f}, recall@1={o['recall_at_1']:.3f}, "
            f"recall@3={o['recall_at_3']:.3f}, recall@5={o['recall_at_5']:.3f}",
            f"median_latency={o['median_latency_ms']}ms",
        ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--late", action="store_true", help="Run late mode only")
    p.add_argument("--both", action="store_true", help="Run baseline and late, compare")
    p.add_argument("--only", help="Restrict to a single case id")
    p.add_argument("--out", help="Override output directory")
    args = p.parse_args()

    cases = _load_cases()
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        ROOT / "results" / f"ranked_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    base = late = None
    if args.both:
        print("=== baseline ===", file=sys.stderr)
        base = _run_one_mode(cases, late=False, only=args.only)
        print("=== late ===", file=sys.stderr)
        late = _run_one_mode(cases, late=True, only=args.only)
    elif args.late:
        late = _run_one_mode(cases, late=True, only=args.only)
    else:
        base = _run_one_mode(cases, late=False, only=args.only)

    if base is not None:
        (out_dir / "baseline.json").write_text(json.dumps(base, indent=2, ensure_ascii=False))
    if late is not None:
        (out_dir / "late.json").write_text(json.dumps(late, indent=2, ensure_ascii=False))

    summary = _render_summary(base, late)
    (out_dir / "summary.md").write_text(summary)
    print(f"\nwrote {out_dir}/summary.md", file=sys.stderr)
    print(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: 스모크 실행 (baseline only, `--only` 하나)**

임베딩 서버(`:8081`)가 떠 있어야 함. 사용자가 이미 `:8081`을 운영 중이라고 가정.

```bash
mamba run -n trawl python tests/test_pipeline_ranked.py --only kbo_schedule 2>&1 | tail -15
```
Expected:
- stderr에 `[kbo_schedule] base ... mrr=X.XXX (NNNms)`
- stdout에 summary.md 내용 출력 (overall MRR 등)
- exit 0

실패 시:
- 서버 미기동 → `ConnectionError`. 사용자에게 `:8081` 기동 요청 후 재시도.
- 그 외 → 구조적 버그. BLOCKED.

- [ ] **Step 4: 커밋**

```bash
git add tests/test_pipeline_ranked.py
git commit -m "test(late-chunking): add ranked A/B runner (MRR + recall@k)"
```

---

## Task 6: 문서 업데이트 (CLAUDE.md)

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Endpoint map에 `:8084` 한 줄 추가**

`CLAUDE.md`의 "llama-server endpoint map" 섹션(현재 4개 엔트리)에 아래를 `:8080` 아래 또는 `Slot pinning` 위에 추가:

```
  - `:8084` — bge-m3 with **--pooling none**. Late chunking *only*.
    Down by default. Start/stop with
    `scripts/late_chunking_server.sh {start|stop|status}`. **Never** change
    `:8081` to `--pooling none` — that would regress the parity matrix.
```

- [ ] **Step 2: "Resource juggling" 섹션 추가**

"llama-server endpoint map" 블록 바로 아래 (Quick Reference 위)에 새 subsection:

```markdown
### Resource juggling

Late chunking is **opt-in**. The `:8084` server is **not** started by the
standard dev setup — only when you're measuring or using late chunking.

- Start before a ranked run: `scripts/late_chunking_server.sh start`
- Stop afterwards: `scripts/late_chunking_server.sh stop`
- VRAM tight? Stop the reranker (`:8083`) and/or the utility LLM (`:8082`)
  first; late chunking only needs bge-m3 and any of those can come back
  after the measurement.
```

- [ ] **Step 3: Quick Reference에 ranked 러너 한 줄 추가**

현재 Quick Reference 펜스 블록 마지막 (WCXB 라인 아래)에 추가:

```bash
# Ranked A/B runner (baseline vs late chunking)
scripts/late_chunking_server.sh start
python tests/test_pipeline_ranked.py --both
scripts/late_chunking_server.sh stop
```

- [ ] **Step 4: Code layout에 새 파일 등록**

`## Code layout` 블록 안의 `src/trawl/` 섹션에서 `extraction.py` 아래 다음 한 줄 추가:

```
  late_chunking.py               bge-m3 per-token → per-chunk mean pool (opt-in)
```

그리고 `tests/` 섹션에는:
```
  test_pipeline_ranked.py        MRR / recall@k A/B runner (baseline vs late)
  test_chunking.py               chunk offset unit tests
  test_late_chunking.py          late chunking unit tests (httpx-mocked)
```

`scripts/` 디렉터리는 현재 Code layout에 없음 → 새 최상위 블록 추가:
```
scripts/
  late_chunking_server.sh        llama-server :8084 launcher (opt-in)
```

- [ ] **Step 5: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs(late-chunking): register :8084 endpoint and ranked runner in CLAUDE.md"
```

---

## Task 7: 실측 + go/no-go 판정 — **controller + user**

**Purpose:** 전체 파이프라인을 실 서버에 연결해 12-case ranked A/B를 돌리고, 결과를 `results.md`에 기록 + 스펙 §Success criteria로 go/no-go 판정.

**Who:** 컨트롤러 + 사용자 (`:8084` 서버 기동 필요).

**Files:**
- Generated: `tests/results/ranked_<ts>/{baseline.json, late.json, summary.md}` (gitignored)
- Create: `docs/superpowers/specs/2026-04-14-late-chunking-results.md` (committed)
- Possibly modify: `README.md` (결과에 따라)

- [ ] **Step 1: 사전 점검**

```bash
# :8081 baseline 필수
curl -sf http://localhost:8081/health && echo "baseline OK"
# :8084 late 필수
scripts/late_chunking_server.sh status || scripts/late_chunking_server.sh start
curl -sf http://localhost:8084/health && echo "late OK"
```
둘 다 OK여야 Step 2로.

- [ ] **Step 2: A/B 실행**

```bash
mamba run -n trawl python tests/test_pipeline_ranked.py --both 2>&1 | tee /tmp/ranked_run.log
```
Expected:
- stderr에 per-case 진행 로그 (`[<id>] base ... / late ...`)
- stdout에 summary.md 전체 출력
- exit 0

실패 시 원인별 대응:
- `LateChunkingServerDown` → `:8084` 설정 재확인.
- 어떤 케이스 fetch 실패 → 네트워크 문제. 해당 케이스 제외 후 재실행(`--only` 루프).

- [ ] **Step 3: parity 보장 확인**

기존 12/12 parity는 ranked 러너의 `recall@5`와 독립. 별도 검증:
```bash
mamba run -n trawl python tests/test_pipeline.py 2>&1 | tail -5
```
Expected: `12/12` 통과. 이게 깨지면 **Task 1~4 어딘가에 회귀** — 즉시 다른 태스크보다 우선 fix.

- [ ] **Step 4: go/no-go 판정**

스펙의 success criteria 4가지를 `summary.md` 수치와 대조:
- Go: parity 12/12 AND overall MRR Δ ≥ +0.03 AND per-case recall@5 감소 없음 AND p50 latency < 2× baseline.
- No-go: MRR Δ |≤0.01| OR recall@5 회귀 OR parity 회귀.
- Mixed: 그 외.

- [ ] **Step 5: 결과 문서 작성**

File: `docs/superpowers/specs/2026-04-14-late-chunking-results.md`

```markdown
# Late chunking — results

Date: <실행일>
Commit: <HEAD SHA>
Spec: docs/superpowers/specs/2026-04-14-late-chunking-design.md

## Setup
- Baseline: bge-m3 on :8081, default pooling
- Late: bge-m3 on :8084, --pooling none
- Cases: tests/test_cases.yaml (N cases)

## Overall
<paste summary.md의 overall 표>

## Per-case
<paste summary.md의 per-case 표>

## Fallback stats
- Chunks falling back to baseline (past 8K cap): X / Y total chunks
- Cases where fallback occurred: [list of case ids]

## Verdict
**GO / NO-GO / MIXED**

Reasoning: <수치 기반 요약. 어느 criterion을 어떻게 만족/미달했는지.>

## Follow-up
- GO: 다음 커밋에서 기본값을 late로 전환하고 baseline 경로 제거 — 별도 PR.
- MIXED: 조건부 켜기 규칙 (예: 페이지 길이 > X) 탐색 — 별도 스파이크.
- NO-GO: 토글 유지하되 README에 미반영. 현재 구현은 살려둠 (차후 실험 기반).
```

- [ ] **Step 6: 결과 커밋**

```bash
git add docs/superpowers/specs/2026-04-14-late-chunking-results.md
git commit -m "docs(late-chunking): record A/B results and go/no-go verdict"
```

- [ ] **Step 7: (Go일 경우) README 반영**

스파이크가 Go이면 `README.md`에 짧은 섹션 추가:

```markdown
### Late chunking (experimental)

trawl supports Jina-style late chunking as an opt-in embedding path. Set
`TRAWL_LATE_CHUNKING=1` or pass `late_chunking=True` to `fetch_relevant()`.
Requires a separate `llama-server --pooling none` instance; see
[`scripts/late_chunking_server.sh`](scripts/late_chunking_server.sh).

Measured improvement on the 12-case parity matrix:
MRR +X.XXX, recall@5 Y% → Z%. Full results:
[`docs/superpowers/specs/2026-04-14-late-chunking-results.md`](docs/superpowers/specs/2026-04-14-late-chunking-results.md).
```

Mixed/No-go면 README 미수정.

- [ ] **Step 8: 서버 정리**

```bash
scripts/late_chunking_server.sh stop
```

---

## Plan self-review notes

- **Spec coverage**: §Goal(Task 7 verdict), §Architecture(Tasks 1+3+4), §Components(Tasks 2,3,4,5), §Data flow(Task 4), §Error handling(Task 3), §Success criteria(Task 7 Step 4), §Repository integration(Task 6). `docs/...-results.md` 는 Task 7.
- **Placeholder scan**: `<실행일>`, `<HEAD SHA>`, `<paste ...>`, `<수치 기반 요약>` 는 Task 7 Step 5 결과물 채우는 항목 — 실측 이전에 값을 넣을 수 없어 의도적 남김. Task 0 findings도 유사.
- **Type consistency**: `Chunk.char_start/char_end`(Task 1), `embed_chunks_late(chunks, full_md, ...)`(Task 3), `retrieve(..., late_chunking, full_md)`(Task 4), `fetch_relevant(..., late_chunking=None)`(Task 4) — 일관.
- **File paths**: 모두 구체적.

---

## Execution choice

Plan complete and saved to `docs/superpowers/plans/2026-04-14-late-chunking.md`.
