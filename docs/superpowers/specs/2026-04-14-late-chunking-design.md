# Late chunking — design

Date: 2026-04-14
Source: `RESEARCH.md` 후보 C1.
Status: brainstorm approved, awaiting implementation plan.

## Goal

Jina의 **Late Chunking** (arXiv:2409.04701) 을 trawl 임베딩 단계에
도입하고, 현재 "청크-단위 독립 임베딩" 대비 recall/MRR 이득이 있는지
**정량 측정**한다. 스파이크 — 결과에 따라 기본값 채택 / 토글 유지 / 폐기
중 택일.

## Non-goals

- 청킹 로직(`src/trawl/chunking.py`) 변경 — 청크 경계는 현재 그대로.
- 8K 토큰 초과 페이지 완전 처리 (슬라이딩 윈도우). 스파이크 범위 밖,
  결과 좋으면 Phase 2.
- 다른 retrieval 최적화(HyDE, reranker 등) 동시 튜닝.
- 새 의존성 추가 (`transformers`, `sentence-transformers` 등).

## Background

현재 `retrieval.py`는 각 청크를 독립적으로 bge-m3(`:8081`, default pooling=mean)에
보내 벡터를 받는다. 이 방식은 **청크 경계에서 사라지는 문맥**에 약하다:
표 안의 값, 연속 문단의 대명사 참조, 헤딩 없이 이어지는 리스트 등.

Late Chunking은 전체 문서를 한 번 forward pass 해 **토큰별 hidden state**
를 얻은 뒤, 각 청크의 토큰 span에 대해 mean pool 한다. 결과적으로 청크
임베딩이 주변 문맥 정보를 담는다.

### 측정 분리 원칙
Late chunking은 **retrieval** 를 바꾼다. WCXB(extraction 벤치)로는
측정 불가. 측정 장치는 기존 parity matrix(쿼리 있음)를 확장.

## Design decisions

| 결정 | 선택 | 이유 |
|---|---|---|
| 스파이크 성격 | 정량 실험 후 판단 | 지금 깨진 케이스 없음, 이론 근거는 있음 |
| 측정 장치 | 12-case parity runner를 recall@k + MRR로 확장 | 재사용, 측정 세분화. 새 벤치 구축은 과잉 |
| 스코프 | **옵트인 토글** (`TRAWL_LATE_CHUNKING=1` / kwarg) | A/B 직접 비교 필수, 기본값 유지로 회귀 방지 |
| 서버 | `:8084`에 `--pooling none`으로 bge-m3 **전용 인스턴스** | `:8081` 설정 변경 회피. 포트 분리는 trawl 관례 |
| 긴 문서 | 앞 **8K 토큰만 late-chunk, 뒤쪽 청크는 baseline fallback** | 스파이크 단계에선 단순함이 답. 효과 보고 Phase 2에서 sliding window |
| Fallback 정책 | 서버 down 시 **silent fallback 금지** → 명시적 에러 | 측정 오염 방지 |
| 토크나이저 | llama-server `/tokenize` 엔드포인트 | transformers 의존성 불필요, offset 일관성 |

## Architecture

### 경로 비교
```
baseline:      chunks → /v1/embeddings at :8081 (mean pool per chunk) → vectors → top-k
late-chunking: full_md → /tokenize at :8084 (offsets)
               → /v1/embeddings at :8084 (--pooling none) (token vectors)
               → for each chunk: mean-pool tokens in its char span
                 (8K 초과 청크는 :8081 baseline 경로로 개별 임베딩 fallback)
               → vectors → top-k
```

### 파일 레이아웃
```
src/trawl/
  late_chunking.py         NEW — embed_chunks_late(), token pooling, 8K truncate
  retrieval.py             modified — late_chunking kwarg로 경로 분기
  pipeline.py              modified — 환경변수 파싱 + kwarg 통과

scripts/
  late_chunking_server.sh  NEW — :8084 기동/종료/상태 도우미

tests/
  test_pipeline_ranked.py  NEW — baseline/late A/B 러너, recall@k/MRR 출력
                                 기존 test_pipeline.py (parity)는 건드리지 않음
```

`chunking.py`는 변경 없음. 청크 경계 동일, 임베딩만 변경 — A/B 비교의 핵심
공정성 조건.

## Components

### `late_chunking.py`

```python
def embed_chunks_late(
    chunks: list[Chunk],
    full_md: str,
    *,
    late_base_url: str,   # default http://localhost:8084/v1
    baseline_base_url: str,  # default http://localhost:8081/v1, for fallback
    model: str,
    http_timeout_s: float = 60.0,
) -> list[list[float]]:
    """Return one vector per chunk via late chunking, with baseline fallback
    for chunks outside the 8K token cap.

    Raises LateChunkingServerDown if the late server is unreachable on
    startup. Per-chunk fallback is silent (to baseline server at :8081);
    server-level absence is not.
    """
```

단계:
1. `POST {late_base_url}/tokenize` on `full_md` → `{tokens, offsets}`. 실패 시 `LateChunkingServerDown`.
2. 토큰 수 > 8192 → 앞 8192에서 자른 문자열 `md_head`, 나머지는 `chunks_past_cap`에 기록.
3. `POST {late_base_url}/v1/embeddings` on `md_head` with `--pooling none` 결과 수신(서버 측 설정이 pooling=none이어야 per-token 반환).
4. 각 청크에 대해:
   - 청크의 (char_start, char_end) in full_md 계산. `chunk.text`는 `md`에서 찾되, `chunking.py`가 이미 leading/trailing strip을 하므로 1차 근사는 `md.find(chunk.text)` 가능. 실패 케이스(중복·정규화 차이) 처리를 위해 **chunk 생성 시 `char_start`/`char_end`를 Chunk에 저장하도록 `chunking.py` 확장**.
   - 청크 범위가 캡 내부 → token offset → tokens_in_range의 hidden state mean pool → 청크 벡터.
   - 청크 범위가 캡을 걸침/벗어남 → `baseline_base_url`에 `[heading + "\n\n" + embed_text]` 를 POST (기존 retrieval.py 로직 재사용) → 벡터.
5. 입력 `chunks` 순서에 맞춰 `list[list[float]]` 반환.

**청크 범위 저장 (chunking.py 보강)**: `Chunk` dataclass에 `char_start: int`, `char_end: int` 추가. `chunk_markdown()`이 원본 `md`에서 각 청크의 절대 offset을 같이 기록. 기존 소비자(pipeline 결과)는 영향 없음 (필드 추가만).

### `retrieval.py` 수정

- `retrieve(..., late_chunking: bool = False, full_md: str = "")` 매개변수 추가.
- `late_chunking=True`일 때 `embed_chunks_late()` 사용, 아니면 기존 경로 유지.
- `full_md`는 pipeline에서 전달. 비면 `late_chunking=True`여도 명시적 에러.

### `pipeline.py` 수정

- `fetch_relevant(..., late_chunking: bool | None = None)` 매개변수.
- None일 때 `os.environ.get("TRAWL_LATE_CHUNKING")` 체크 ("1"/"true" → True).
- `retrieve(..., late_chunking=..., full_md=md_after_extraction)` 전달.

### `scripts/late_chunking_server.sh`

```bash
late_chunking_server.sh start|stop|status
```
- PID: `/tmp/trawl_late_chunking.pid`, 로그: `/tmp/trawl_late_chunking.log`.
- 모델: `$TRAWL_BGE_M3_GGUF` (스크립트 상단 default, env로 override).
- 커맨드 (검증 대상):
  ```
  llama-server --model "$TRAWL_BGE_M3_GGUF" --port 8084 \
    --embedding --pooling none --ubatch-size 2048 --ctx-size 8192
  ```
- `start` 이후 10초간 `/health` polling. 실패 시 exit 1 + 로그 tail.

### `tests/test_pipeline_ranked.py`

현재 `test_pipeline.py`를 수정하지 않고 **별도 러너** 추가.

```bash
python tests/test_pipeline_ranked.py                # baseline only
python tests/test_pipeline_ranked.py --late         # late only
python tests/test_pipeline_ranked.py --both         # A/B compare
```

각 케이스에 대해 `must_contain_*` 룰 별로 **첫 매칭된 청크의 rank** 를
기록, case MRR 및 recall@1/@3/@5 계산. `--both`는 두 모드를 순차 실행해
per-case Δ 와 overall Δ 를 같은 콘솔 출력에 정리 + JSON 저장.

결과: `tests/results/ranked_<timestamp>/{baseline.json, late.json, summary.md}` (gitignored).

## Data flow

```
URL → fetcher → extraction (unchanged)
    → chunking (unchanged, but now with char_start/char_end on Chunk)
    → IF late_chunking:
         late_chunking.embed_chunks_late(chunks, full_md)
       ELSE:
         retrieval._embed_batch(...) on chunk_texts        (current)
    → cosine top-k (unchanged)
    → ScoredChunk[]
```

## Error handling

- Late server 전혀 안 떠 있음 → `LateChunkingServerDown`, pipeline이
  `RetrievalResult.error`로 전파, 러너 exit 2.
- `/tokenize` 응답이 offsets 없음/형식 이상 → 같은 에러.
- `/v1/embeddings`(--pooling none) 응답이 per-token 구조 아님 → 같은 에러.
  (서버가 `--pooling mean`으로 떠 있으면 여기서 검출됨.)
- 개별 청크의 token span 매핑 실패 → 그 청크만 baseline fallback, 경고
  stderr.
- 8K 초과로 fallback된 청크 수 → 러너 리포트에 counter 표시.

## Success criteria (go / no-go)

스파이크 종료 시 아래 기준으로 판정. 결과를 `docs/superpowers/specs/2026-04-14-late-chunking-results.md`에 기록.

- **Go (기본 채택 검토):**
  - 12/12 parity matrix 유지 AND
  - overall MRR Δ ≥ **+0.03** AND
  - 어떤 케이스에서도 recall@5 감소 없음 AND
  - latency p50 증가 < 2× baseline.
- **No-go (토글 유지 or 폐기):**
  - MRR Δ 절댓값 ≤ 0.01 (노이즈 수준) OR
  - recall@5 회귀 있음 OR
  - 12/12 parity 깨짐.
- **Mixed:**
  - 일부 케이스 이익, 일부 손실 — 페이지 타입/길이별 분해 후 조건부 채택
    검토 (예: "긴 산문 페이지에서만 ON").

## Repository integration

- `.gitignore`: 변경 없음 (ranked 결과는 `tests/results/` 하위라 기존
  glob에 포함됨).
- `environment.yml`: 변경 없음.
- `CLAUDE.md`:
  - Endpoint map에 `:8084` 한 줄 추가 (down by default, `scripts/…` 참고).
  - "Resource juggling" 짧은 섹션: late chunking 서버는 옵션, 타이트하면
    내릴 것.
- `README.md`: 스파이크 완료 후 결과에 따라 반영 (Go → "late chunking
  옵션 지원", Mixed → 조건부 설명, No-go → 미반영).

## Out of scope / future

- **슬라이딩 윈도우 + 오버랩 처리** (긴 문서 >8K): 현재 truncate + fallback.
  스파이크 결과 Go일 때, Phase 2에서 확장.
- **기본값 전환 + baseline 경로 제거**: 스파이크 Go일 때 별도 커밋.
- **Contextual retrieval (Anthropic 방식)** 비교: 별도 스파이크로 분리.

## Open risks

1. **bge-m3 `--pooling none` 엔드포인트 shape** — llama.cpp가 정확히 무슨
   모양으로 per-token 벡터를 반환하는지 구현 시점에 실측으로 확인 필요.
   문서와 다르면 파싱 로직 조정.
2. **chunk char offset 안정성** — `chunking.py`에 `char_start`/`char_end`
   추가 후 기존 12-case가 binary로는 그대로 통과하는지 확인 (단순 필드
   추가라 회귀 없어야 정상).
3. **`/tokenize` 결과와 `/v1/embeddings` 토큰 수 일치** — 공백/BOS/EOS
   토큰 처리 방식이 달라 오프 바이 원 발생 가능. 토큰 수 assert 추가.
