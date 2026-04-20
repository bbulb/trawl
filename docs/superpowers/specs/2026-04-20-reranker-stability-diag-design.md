# Reranker `:8083` stability diagnostic — design (2026-04-20)

Branch: `spike/reranker-stability-diag` (off `develop` at `012a49f`,
post-0.4.0).

## 관찰된 이슈

2026-04-20 세션 중 4개 spike 측정 (PR #31/#32/#33/#34) 에서
`bge-reranker-v2-m3` at `localhost:8083` 가 **간헐적 HTTP 500** 반환:

```
reranker unavailable, falling back to cosine: Server error '500 Internal Server Error'
for url 'http://localhost:8083/v1/rerank'
```

- 모든 경우 `reranking.py` 의 `except (httpx.HTTPError, ...)` 캐치 →
  **cosine fallback** 으로 정상 동작 유지.
- Assertion 은 fallback 시에도 대부분 PASS.
- 실제 실패 패턴: 주로 MDN 관련 iteration 에서 관측. 규칙성 불분명.
- 이 세션 시작 시점 health check 는 200. 즉 완전히 죽은 게 아니라
  간헐.

## 비-목표

- **현재 `reranking.py` 수정 금지.** 이 spike 는 진단. 수정은 별도
  spike 에서 pre-registered gate 통과 후.
- **llama-server 재시작 / 설정 변경 금지.** 사용자 인프라 영향.
  관찰만.
- **reranker 모델 교체 검토 안 함.** 본 spike 범위 밖.
- **`code_heavy_query` 재측정 안 함.** 이미 16/16 확정. 회귀 없으면
  본 spike 의 fix 가치 제한적.

## 접근법

독립 diagnostic script: `benchmarks/reranker_stability_diag.py`.
`trawl.reranking.rerank()` 를 우회 하고 `:8083` 직접 HTTP 호출.
여러 payload 크기 × 반복 횟수 조합 으로 failure mode 관찰.

### 측정 차원

1. **Sequential bursts** — N=50 requests back-to-back, varying doc
   count (5, 10, 20, 50 docs per request).
2. **Payload size** — doc 길이 (short 100자, medium 500자, long
   2000자) x 3.
3. **Reranker-stable "canary" request** — 매 10 requests 마다 고정
   입력 1 request 삽입. 실패율 추이 관찰.
4. **`/health` poll** — 각 burst 전후 health 체크. 서버 가 죽었는지
   간헐인지 구분.

총 scenario: ~200-300 HTTP calls. 5-10분 소요.

### 기록할 데이터

각 request 마다:
- `index` (순번)
- `http_status`
- `elapsed_ms`
- `n_docs`
- `doc_char_total` (query + docs 합산 문자 수)
- `response_len` (성공 시)
- `error_message` (실패 시)

출력:
- `benchmarks/results/reranker-stability-diag/<ts>/diag.json` —
  raw per-request data.
- `diag.md` — 사람이 읽는 리포트 (failure 분포, payload size vs
  failure rate scatter, canary 결과).

## Pre-registered decision gates

본 spike 는 코드 변경 없음. 대신 **4 가지 결정 분기** 중 하나로
다음 spike 방향 확정.

| 결과 | 해석 | 다음 spike |
|---|---|---|
| **(D1) 실패율 < 1%** | 이 세션 중 운이 나빴음. 현 시점에선 stable. | 후속 spike 불필요. 실사용 중 재발 시 재조사. 이 진단 자체만 보관. |
| **(D2) payload 크기 와 failure rate 강한 상관** (예: >10 docs 또는 >5000 chars → 50%+ 실패) | OOM / 토큰 한계 의심. | **reranker chunk window 축소 spike** — `rerank(scored, k=?)` 호출 시 input 을 N 이하로 cap. `TRAWL_RERANK_MAX_DOCS` env var. |
| **(D3) 시간적 cluster** (처음 M 번은 OK, 이후 급증) | 서버 상태 drift / KV cache / memory leak. | **reranker retry / back-off spike** — `reranking.py` 에 exponential back-off + 서버 health check 기반 recovery. 또는 request 간 throttle. |
| **(D4) 무작위 분포** | 투명 실패 (네트워크 / keep-alive / SSL 등 환경 이슈). | **reranker retry + jitter spike** — simple retry 가 충분할 수 있음. 단 최소 변경 으로 안정화. |

## 파일 변경

- `docs/superpowers/specs/2026-04-20-reranker-stability-diag-design.md`
  — 본 문서 (신규, PR 포함).
- `benchmarks/reranker_stability_diag.py` — diagnostic script (신규, PR
  포함).
- `notes/reranker-stability-diag-outcome.md` — 결과 + 분기 결정
  (gitignored).

`src/trawl/` 변경 **없음**.

## 측정 계획

1. `curl :8083/health` 이 200 임을 전제.
2. Warmup: 5 canary requests 를 저부하로 먼저 실행.
3. Burst 실험:
   - Burst 1: 50 × (5 docs × 100 chars)
   - Burst 2: 50 × (20 docs × 500 chars)
   - Burst 3: 50 × (50 docs × 2000 chars)
4. Canary: 각 burst 사이 에 5 canary requests.
5. 분석:
   - Failure rate 총계, burst 별 breakdown.
   - Payload feature 별 failure rate (n_docs, char_total).
   - 실패 timing (index 분포, consecutive vs scattered).
   - Canary 결과 로 server state drift 판단.

### Exit code

- 0 — 측정 성공.
- 2 — 서버 unreachable (초기 health check 실패).

## 리스크

1. **관측 중 실패 안 일어남.** 500 이 이 세션 시점 에서는 재현 안
   될 수 있음. D1 결론. 이 경우 도 "현재는 안정" 이라는 신호로
   충분히 가치 있음.
2. **서버 과부하 로 인한 다른 consumer 영향.** 200-300 request 는
   모델 추론 workload — 다른 트랩 사용자 (Claude chat agent 등) 의
   slot 를 잠시 차지. 사용자 환경 설명 메모리 (`CLAUDE.md` llama-
   server endpoint map) 에 쓰여있듯 :8083 은 reranker 전용이라
   영향 낮음. 단 burst 3 는 50 × 50 docs 이므로 조심.
3. **일관된 재현 요구 X**. 본 spike 는 failure mode 특성 파악 이
   목표. 100% 재현 못해도 OK.

## 타이밍

설계 ~15min, 스크립트 ~25min, 측정 ~10min, outcome note + PR
~20min. 총 ~70 min.
