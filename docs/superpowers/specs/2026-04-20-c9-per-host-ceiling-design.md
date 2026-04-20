# C9 — Per-host adaptive content-ready ceiling — design (2026-04-20)

Branch: `feat/c9-per-host-ceiling` (stacked on `docs/architecture-c7-c8-done`)

## 문제

`fetchers/playwright.py::_wait_for_content_ready` 의 상한은 전역 상수
`wait_for_ms=5000ms`. 콘텐츠-ready 감지가 텍스트 변화 안정화를 빠르게
포착하므로 대부분의 host 는 sub-2s 에 종료되지만, 일부 느린 host 는
5000ms 를 다 써서야 안정화된다. 반대로 아주 빠른 host(예: GitHub
docs, Wikipedia mobile) 는 300-800ms 면 충분하다.

현재는 두 부류 모두에 대해 보수적인 5000ms 상한을 적용한다. 빠른
host 에 대해서는 불필요한 감시 cycle 이 발생 (평균 150ms × polling 의
무의미 반복). 그리고 5000ms 를 실제로 다 쓰는 host 가 있을 가능성에
대비해 상한 자체를 줄이지도 못한다.

host 별 p95 fetch 시간을 학습해 host-specific ceiling 으로 치환할 수
있다. `tests/agent_patterns/workflows.yaml` 의 `large_page` 카테고리
(`claude_code_large_wikipedia_article`) 가 이 efficacy 를 측정하는
거울 역할.

ARCHITECTURE.md "Future work" 항목 2 (per-domain adaptive timeout) +
RESEARCH.md C9 에 이미 정리된 후속.

## 접근법

`~/.cache/trawl/host_stats.json` 에 host → rolling window of last K
observations 를 저장. 관측 수 ≥ `MIN_OBSERVATIONS` 이면 p95 × 배율을
ceiling 으로 사용, 그렇지 않으면 호출자가 넘긴 default 그대로 반환.

### 자료 구조

```json
{
  "schema": 1,
  "hosts": {
    "en.wikipedia.org": {
      "samples_ms": [850, 910, 920, 880, 1100, 820, 840, ...],
      "updated_at": 1713571200.123
    },
    "finance.naver.com": {
      "samples_ms": [4500, 4800, 5000, 5000, 4900, ...],
      "updated_at": 1713571180.000
    }
  }
}
```

- `samples_ms` — bounded list (deque semantics) of the last `WINDOW_SIZE`
  observations, newest last. `WINDOW_SIZE=50` by default — enough statistical
  stability without holding onto ancient measurements.
- `updated_at` — last write time per host. The cache file is single-file
  (no per-host rotation) because read/write is infrequent (one append per
  fetch) and full-file rewrite is simpler than incremental journaling.

### 학습 로직

```python
def ceiling_ms(url: str, default: int) -> int:
    """Adaptive ceiling. Returns default when warm-up threshold not yet
    met or the stats file is unavailable."""
    host = _hostname(url)
    if host is None:
        return default
    samples = _load().get(host, {}).get("samples_ms", [])
    if len(samples) < MIN_OBSERVATIONS:
        return default
    p95 = _percentile(samples, 95)
    adaptive = int(p95 * CEILING_MULTIPLIER)
    return max(MIN_CEILING_MS, min(MAX_CEILING_MS, adaptive))
```

### 기록 로직

```python
def record(url: str, fetch_ms: int) -> None:
    """Append one observation to the host's rolling window.

    Silently skipped when disabled (TRAWL_HOST_STATS=0), when the URL
    doesn't have a usable hostname, or when fetch_ms is clearly
    anomalous (< 0 or > MAX_CEILING_MS * 2).
    """
```

### 상한/하한 및 배율 근거

| 상수 | 값 | 이유 |
|---|---|---|
| `WINDOW_SIZE` | 50 | 최근 관측만 보되 spike 한두 건이 p95 를 지배하지 않을 충분한 샘플 |
| `MIN_OBSERVATIONS` | 5 | 적어도 한 세션 분량. 과소 샘플로 공격적인 ceiling 설정 회피 |
| `MIN_CEILING_MS` | 1500 | content-ready 감지 최소 안전 여유 (stableTicks=4 × polling=150ms + 버퍼) |
| `MAX_CEILING_MS` | 15000 | runaway 방지. `_open_context` timeout (30s) 절반. 절대로 이보다 크게 적용 안 함 |
| `CEILING_MULTIPLIER` | 1.5 | p95 보다 여유 있게. 2.0 은 너무 느슨, 1.2 는 frequent 재충돌 관찰 |

### 피드백 루프 리스크

ceiling 이 관측을 bound 한다 → 관측 p95 가 ceiling 근처에 붙으면 다음
ceiling 은 p95 × 1.5 로 오른다 → 자기 충족 예언으로 ceiling 이 계속
밀려올라갈 수 있다. 완화책 3 층:

1. `MAX_CEILING_MS=15000` 상한. 절대로 이보다 큰 ceiling 은 적용되지
   않음.
2. `fetch_ms` 가 `MAX_CEILING_MS × 2` 를 초과하는 관측은 기록 시
   버림 (네트워크 이상치 보호).
3. ceiling 계산은 p95 × 1.5 이므로 p95 가 ceiling 이하라면 새 ceiling
   은 현재보다 작음 — 단조 증가는 아님.

### 환경 변수

- `TRAWL_HOST_STATS` — `0` 이면 비활성 (`record` 는 no-op, `ceiling_ms` 는
  항상 default 반환). 기본 `1`.
- `TRAWL_HOST_STATS_PATH` — default `~/.cache/trawl/host_stats.json`.

다른 상수 (`WINDOW_SIZE`, `MIN_OBSERVATIONS`, `CEILING_MULTIPLIER`,
`MIN_CEILING_MS`, `MAX_CEILING_MS`) 는 env var 로 노출하지 않음. 튜닝
가치가 향후 데이터로 입증되기 전까지는 inside-house 상수로 유지.

### 동시성

단일 프로세스 단일 워커 (MCP 서버 패치 기준) 에서는 race 없음. 다중
프로세스가 같은 파일을 쓰는 경우:

- `record()` 는 read-modify-write. 마지막 writer 가 승리 — 타 writer 의
  최근 관측 손실 가능.
- 관측 손실은 손해가 적음 (window 에 최근 샘플 몇 개 빠져도 p95 크게
  흔들리지 않음). complex file-lock 대신 **soft-rate-limit** 적용:
  1초 이내 연속 쓰기는 합쳐서 한 번만 flush.

## 통합 지점

`fetchers/playwright.py::fetch` 와 `render_session` 진입부:

```python
def fetch(url: str, *, wait_for_ms: int = 5000, ...) -> FetchResult:
    effective_wait_ms = host_stats.ceiling_ms(url, default=wait_for_ms)
    t0 = time.monotonic()
    with _lock:
        try:
            with _open_context(url, wait_for_ms=effective_wait_ms, ...) as ...:
                ...
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                host_stats.record(url, elapsed_ms)
                return FetchResult(..., elapsed_ms=elapsed_ms, ...)
        except ...:
            # 에러 경로는 기록하지 않음 — 타임아웃 흉터가 학습을 왜곡
```

`render_session` 은 `yield` 이후 caller 가 얼마나 오래 세션을 쥐고 있을지
알 수 없으므로, 세션 진입까지의 시간 (`goto + content_ready` wait 만)만
기록. 구현은 `_open_context` 내부에서 wait 종료 시점의 elapsed 를 반환
하게 고치는 방식이 이상적이나, 범위 관리를 위해 MVP 는 `fetch()` 경로만
기록. `render_session` 은 ceiling 은 consult 하되 record 는 안 함.

## PipelineResult 노출 여부

`cache_hit` 처럼 `PipelineResult` 필드로 ceiling 정보를 노출할지: **아직
노출 안 함**. 이유:

- agent-patterns DSL 에 이미 `total_ms_p95` 가 있어 end-to-end 으로 측정
  가능.
- 필요 시 후속 PR 에서 `host_ceiling_ms: int = 0` 필드 추가.

## 예상 효과

`tests/agent_patterns/workflows.yaml` 의 large_page 패턴
`claude_code_large_wikipedia_article` (한국전쟁 Wikipedia 기사) 에서:

- 초기 방문 5 회: 현재와 동일 (5000ms ceiling).
- 6 회 이상: Wikipedia 가 통상 800-1200ms 에 settle → ceiling 이 1500ms
  근처로 학습. 같은 host 다른 URL 호출 시 ≈ 3500ms 절감 가능.

news/schedule 같은 dynamic-heavy host 는 현재와 동일하거나 오히려 높은
ceiling 을 자동 학습 (단, 15000ms 상한 이내).

## 롤백 경로

`TRAWL_HOST_STATS=0` 으로 즉시 off. stats 파일은 다음 enable 까지 존치.

## 테스트 계획

### 유닛 (`tests/test_host_stats.py`)
- put (record) 후 load round-trip
- `MIN_OBSERVATIONS` 미만 → default 반환
- `MIN_OBSERVATIONS` 이상 → p95 × 1.5 반환
- 상한/하한 클램프
- `TRAWL_HOST_STATS=0` → 모두 no-op
- 깨진 JSON → 무시하고 default 반환
- 이상치 (음수, 과도한 값) → 기록 안 됨
- hostname 추출 실패 (ill-formed URL) → no-op
- rolling window (51 개 push → 50 개 유지)

### 통합 (playwright.py 의 fetch())
- 실제 live run 은 본 PR 에서 하지 않음 — 수동 검증용 노트만 CHANGELOG 에.

## scope 밖 (후속)

- HyDE / reranker 같은 network round-trip 에도 host-specific timeout.
- goto vs content-ready 단계별 분리 측정 (현재는 fetch() 전체 elapsed 만).
- Session cookie / user-agent 별 서브 통계.
- ceiling 자동 재평가 알고리즘 (현재는 매 호출 p95 재계산; cache 가능).
