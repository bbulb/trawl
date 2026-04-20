# Reranking chunk-window cap — design (2026-04-20)

Branch: `spike/reranking-chunk-window-cap` (off `develop` at
`905038c`, post-0.4.0).

Parent context:
[2026-04-20-reranker-stability-diag-design.md](2026-04-20-reranker-stability-diag-design.md)
+ `notes/reranker-stability-diag-outcome.md` (PR #36, gitignored).
Diagnostic concluded **D2 (payload-size threshold)**: the reranker
at `localhost:8083` fast-rejects (HTTP 500, ~39 ms) any request whose
document payload blows past its 8 192-token context. Small (5 × 100)
and medium (20 × 500) bursts were clean; large (50 × 2 000 ≈ 101 k
chars) burst failed 100 %.

Actual trawl workload (`retrieve_k = _adaptive_k(n) * 2` ≤ 24 docs
× `MAX_EMBED_INPUT_CHARS` 1 800 + title/heading overhead ≈ 48 k
chars) is well inside the safe range. But there is nothing today
that *guarantees* the payload stays under the threshold — a future
change (larger chunks, higher adaptive k, external caller overriding
`k`, accidentally huge `page_title`) could push past it, at which
point every request 500s and the pipeline silently drops to cosine
fallback. This spike adds a defensive cap so the guarantee is
explicit.

## 관찰된 문제

`src/trawl/reranking.py` 의 `rerank()` 는 현재 :

- `scored` 리스트 를 그대로 `documents` 로 직렬화 (잘라내지 않음).
- `documents` 총 크기 제한 없음.
- 서버 가 HTTP 500 (어떤 원인 이든) 반환 → `except (httpx.HTTPError,
  ...)` → cosine fallback. 실패는 정상 처리 되지만 **사일런트 degradation**.

즉 :

1. 큰 입력 이 올 경우 (미래 회귀 또는 opt-in knob 조합) reranker 가
   무용지물.
2. cosine fallback 은 로깅 되지만 call-site 가 cap 발동 인지 서버
   장애 인지 구분 불가.
3. PR #36 diagnostic 이 confirm 한 failure mode 는 방어 가능 (입력
   크기 clamp) — 구현 하지 않을 이유 없음.

## 비-목표

- **reranker model 교체 / quantisation 변경 금지.** 모델 한계 (8192
  토큰) 는 given. 본 spike 는 클라이언트 단 방어.
- **adaptive_k 또는 `retrieve_k` multiplier 조정 금지.** 별개 tuning.
  CLAUDE.md "Things NOT to change" 표 에 있는 값 불변.
- **`page_title` / `heading` 길이 자르기 금지.** 이건 rerank() 호출자
  (pipeline) 책임. 본 spike 는 `documents` 총합 만 관리.
- **retry / back-off 추가 금지.** D2 는 payload 문제 — retry 는 답 아님.
  (D3 결과였다면 retry 였을 것.)
- **`bge-reranker-v2-m3` 외 다른 context-length 모델 지원 가정 금지.**
  env var 로 조정 가능 하게 만들되 default 는 현재 모델 기준.

## 접근법

### 변경 범위 (single file)

`src/trawl/reranking.py`:

1. **Env vars**:
   - `TRAWL_RERANK_MAX_DOCS` (default `30`) — 최대 문서 수. trawl 현재
     `retrieve_k` 상한 은 `_adaptive_k ≤ 12 × 2 = 24`. 30 은 4× 이하
     headroom; default 로는 안 바이트.
   - `TRAWL_RERANK_MAX_CHARS` (default `60000`) — `query + sum(docs)`
     총 문자 수 상한. 일반 워크로드 최대 추정 (~48 k) 보다 25 % 위;
     D2 threshold (~100 k) 의 60 %. default 로는 안 바이트.
2. **Helper 함수**:
   - `_max_docs_env() -> int`
   - `_max_chars_env() -> int`
3. **Cap 로직 (rerank 내부)**:
   - Step A: `documents = documents[:MAX_DOCS]` + `scored =
     scored[:MAX_DOCS]` (rank 이 낮은 꼬리 버림).
   - Step B: `total = len(query) + sum(len(d) for d in documents)`.
     `total > MAX_CHARS` 이면 per-doc truncate:
     `per_budget = max(200, (MAX_CHARS - len(query)) // len(documents))`.
     각 doc 을 `d[:per_budget]` 로 clip.
   - Step C: cap 이 둘 중 하나라도 firing 하면 `logger.warning(...)`
     **한 번** (call 마다). format:
     `reranker input capped: docs=<pre>→<post> chars=<pre>→<post>
     (TRAWL_RERANK_MAX_DOCS=<N> TRAWL_RERANK_MAX_CHARS=<M>)`.
4. **반환 길이 일관성 보장**: Step A 가 doc 수 를 줄이면 `scored[i]`
   인덱스 기반 매핑 은 truncated list 와 맞춰 진행. (서버 response 는
   capped `documents` 기준.)
5. **Fallback 불변**: 서버 HTTP error 나 JSON 에러 시 기존 `except`
   블록 그대로 동작 (cosine top-k).

### 왜 default-on (즉, 환경변수 가 있을 때 가 아닌 상시 적용) 인가

- **Correctness guard, not performance flag.** D2 diagnostic 가
  증명한 모델 한계 는 객관적 fact. 넘는 순간 100 % 실패. 사용자 가
  명시적으로 opt-in 해야 할 이유 없음.
- **Default 는 normal workload 에 무영향.** `MAX_DOCS=30` / `MAX_CHARS=60000`
  둘 다 실제 trawl 최대 예상치 의 > 1.2× headroom. 15-parity 와 16-
  code_heavy_query 는 훨씬 작은 payload 이므로 default 로는 cap 안
  바이트 — 회귀 risk 0.
- **Env var 는 override 용.** user 가 모델 교체 하거나 chunking 재설정
  한 경우 cap 을 위/아래 조정 가능. C8 / C9 / shadow-DOM 같은 fix
  카테고리 와 동일 철학.

### 왜 truncate-based (drop vs proportional truncation) 인가

원본 diagnostic design doc 의 action sketch 는 "documents[:MAX_DOCS]
truncation + optionally per-string MAX_CHARS/len truncation". 여기서
두 가지 선택지:

- **(i) Drop tail docs until total < MAX_CHARS**: 가장 단순. 낮은-
  cosine-rank 꼬리 가 먼저 사라짐. 단점: 랭킹 quality 영향 예측 어려움.
- **(ii) Per-doc proportional truncation**: 모든 doc 유지, 각각 clip.
  장점: 랭킹 영향 적음 (reranker 가 꼬리-부분 만 보고 score 조정).
  단점: 각 doc 가 잘릴 수 있어 fine-grained 정보 상실.

본 spike 는 **하이브리드**: Step A (doc-count cap, drop tail) →
Step B (per-doc truncate, fit total). Doc-count cap 은 trawl 의 실제
상한 기반 이고, per-doc truncate 은 pathological 대비 backstop. 둘 다
동시 firing 하면 가장 safe.

## Pre-registered decision gates

baseline = 현 develop 의 `rerank()` 동작 (cap 없음).

| 결과 | 조건 | 액션 |
|---|---|---|
| **(a) 채택** | (1) stability diagnostic `--via-trawl` 모드 에서 large burst 500 rate 0 % (cap 이 payload 를 안전 크기 로 줄여 서버 가 정상 응답). (2) parity 15/15 default cap 유지. (3) `code_heavy_query` 16/16 default cap 유지. (4) cap 없이 실행 (`TRAWL_RERANK_MAX_DOCS=0`) 시 동작 과 의미적 동등성 (default cap 이 normal workload 에 미적용 증거). | `feat(reranking): defensive doc-count/char cap on rerank() inputs` PR 로 머지. env var 유지 (override 용). default 상 always-on. CLAUDE.md knobs 표 / CHANGELOG 업데이트. |
| **(b) 기각** | parity 또는 code_heavy_query 가 default cap 로 회귀 OR large burst 500 rate 를 낮추지 못함 | design doc + benchmark runner 만 남기고 코드 revert. 기각 사유 + 측정 기록. `notes/reranking-chunk-window-cap-outcome.md` 에 귀결. |
| **(c) 디자인 수정 필요** | (b) 의 원인 이 default 값 자체 가 너무 공격적 (e.g. `MAX_DOCS=30` 이 실제로 적용 되어 회귀) | default 값 재조정 후 단일 재측정. parity + code_heavy_query 회복 시 (a) 로 이행. 재측정 도 실패 하면 (b). |

### Threshold 근거

- **Gate (a)(1) 500 rate 0 %**: D2 진단 결과 (100% 실패) 대비 극적
  개선 증거. cap 이 작동 하면 서버 는 threshold 이내 payload 를 받아
  서 정상 score 반환. 만약 cap 후에도 large burst 실패 하면 cap
  로직 버그 이거나 서버 가 예상 보다 낮은 threshold. 둘 다 재설계
  사유.
- **Gate (a)(2) parity 15/15**: default cap (`MAX_DOCS=30`,
  `MAX_CHARS=60000`) 은 이 15 패턴 의 max payload (< 48 k) 에 바이트
  되지 않아야 함. 만약 바이트 되어 회귀 발생 → (c) default 재조정
  또는 (b) 기각.
- **Gate (a)(3) code_heavy_query 16/16**: C6 체인 결과 현재 16/16.
  동일 이유 로 cap 바이트 안 됨. MDN (24 chunks × ~1800 chars) 도
  safe range.
- **Gate (a)(4) zero-cap 동등성**: `TRAWL_RERANK_MAX_DOCS=0` /
  `TRAWL_RERANK_MAX_CHARS=0` 을 "cap 비활성" sentinel 로 정의. 이
  모드 로 parity / code_heavy_query 실행 했을 때 default 모드 와
  동일 결과. 차이 있으면 cap 로직 버그.

## 파일 변경

- `docs/superpowers/specs/2026-04-20-reranking-chunk-window-cap-design.md`
  — 본 문서 (신규, PR 포함).
- `src/trawl/reranking.py` — `_max_docs_env()` + `_max_chars_env()`
  + cap 로직 in `rerank()`. ~30 LOC 추가.
- `benchmarks/reranker_stability_diag.py` — `--via-trawl` 플래그 추가.
  기존 direct HTTP path 는 유지 (D2 재현용). 플래그 on 일 때 synthetic
  `ScoredChunk` 생성 하고 `trawl.reranking.rerank()` 호출.
- `tests/test_reranking.py` — 신규 (optional): cap 로직 unit test.
  실제 서버 호출 없이 mock `httpx.Client` 로 cap firing 검증.
- `notes/reranking-chunk-window-cap-outcome.md` — 결과 + 결론 (gitignored).

## 측정 계획

### 실행 순서

1. `mamba activate trawl`; `:8083` healthcheck.
2. 본 design doc commit.
3. `src/trawl/reranking.py` 구현.
4. `benchmarks/reranker_stability_diag.py --via-trawl` 추가.
5. Diagnostic 재측정:
   - `python benchmarks/reranker_stability_diag.py --via-trawl`
   - `python benchmarks/reranker_stability_diag.py` (direct, baseline D2 재확인)
   - 결과 : `benchmarks/results/reranker-stability-diag/<ts>/`.
6. Parity :
   - `python tests/test_pipeline.py` (default cap) → 15/15 요구.
   - `TRAWL_RERANK_MAX_DOCS=0 TRAWL_RERANK_MAX_CHARS=0 python tests/test_pipeline.py`
     → 동일 결과 (sanity, optional).
7. Agent patterns :
   - `python tests/test_agent_patterns.py --shard coding` → `code_heavy_query` 16/16.
8. `notes/reranking-chunk-window-cap-outcome.md` + gate 적용.

### Diagnostic `--via-trawl` 요약

기존 direct HTTP path 와 동일한 burst 구조 (small / medium / large)
를 따르되, 각 request 는 `trawl.reranking.rerank(query, scored,
k=10, page_title="")` 호출. `scored` 는 `ScoredChunk` 합성 — synthetic
chunks 에 `text` / `embed_text` / `heading` / `char_count` /
`chunk_index` 최소 필드만 채움.

기대 결과:
- `small` / `medium` : direct HTTP path 와 동일 (cap 바이트 안 함,
  서버 정상 응답).
- `large` : direct 모드 에선 100% 500 → `--via-trawl` 에선 cap 이
  30 docs × `per_budget ≈ 2 000` chars = ~60 k 로 줄어 서버 정상
  응답. 500 rate 0%.

### Summary.json 스키마 (기존 diagnostic 에 추가)

```json
{
  ...,
  "mode": "direct" | "via_trawl",
  "cap_telemetry": {
    "n_calls_with_cap_fired": N,
    "max_docs_env": 30,
    "max_chars_env": 60000
  }
}
```

`cap_telemetry` 는 `--via-trawl` 에서만 기록. rerank.py 가 WARNING
로그 횟수 를 세서 summary 에 반영 (logging handler capture).

## 리스크

1. **default cap 이 실제 에 바이트 되어 회귀**. `MAX_DOCS=30` 은
   retrieve_k 상한 (24) 위지만, embedding fallback 또는 외부 caller
   가 더 많이 넘기면 cap 적용. 기대치: 이 spike 범위 내 테스트 에선
   발생 안 함. 발생 시 (c) 로 이행 해서 default 재조정.
2. **per-doc truncate 가 reranker 랭킹 을 바꾼다**. cap 이 firing 하는
   경우는 definition 상 "payload 가 안전 범위 외" 이므로, truncate 이
   없으면 서버 500 → cosine fallback 인 상태. 반면 truncate 후 서버
   응답 은 (비록 각 doc 꼬리 잘렸어도) cosine 보다 나은 상태.
   net positive. 측정 에서 확인.
3. **WARNING log 가 과다**. cap 은 default-off 가 아닌 default-on
   이라 firing 빈도 가 높을 수 있음. 다만 D2 기준 으로 trawl 정상
   워크로드 는 바이트 안 됨 → log 는 드물. 필요 시 env var 로
   log-level 조정 가능.
4. **env var=0 sentinel 이 ambiguous** (0 = 비활성 vs 0 = 모든 것 차단).
   `<= 0` 을 "disabled" 로 정의 (직관적). 문서화.
5. **`--via-trawl` diagnostic 이 진짜 workload 를 모사 못함**. synthetic
   ScoredChunk 에는 embed_vector 가 없음. rerank() 는 vector 안 씀
   (서버 가 embed 함) 이라 OK. 위험 낮음.
6. **cap 로직 이 cosine fallback 을 mask**. 서버 가 독립적 장애 (네트워크,
   OOM) 면 cap 과 무관 하게 HTTP error 발생 → 기존 except 블록 처리.
   변경 없음.

## 측정 범위 (작게 유지)

- **Parity**: 15-case matrix (default cap). zero-cap 은 optional sanity.
- **Agent patterns**: `coding` shard (16 code_heavy_query + 4 single_fetch).
  다른 shard 는 본 spike 와 무관 하므로 실행 안 함.
- **Diagnostic**: 기존 3-burst 구조 (small / medium / large) x 2 modes
  (direct / via_trawl) = 6 bursts = 300 requests.
- **Iteration**: parity 1 pass, diagnostic 1 pass (failure rate 는
  deterministic level 에 가까움 — D2 가 100 % 였음).

## Follow-ups (본 spike 범위 밖)

1. **MDN sporadic 500** — 작은 payload 에서 간헐 발생. 본 cap 으론
   해결 안 됨 (별개 failure mode). 재발 시 별도 진단.
2. **Reranker retry with jitter** — D4 였다면 필요 했을 것. 현재 는
   cap 으로 충분. cosine fallback 이 masking 하므로 낮은 우선순위.
3. **rerank 호출 caller-side validation** — pipeline.py 가 자체 적 으로
   payload 크기 check 후 reranking 스킵 결정 할 수도 있음. 오버-엔지
   — cap 만 으로 충분.
4. **Telemetry (optional)** — `PipelineResult` 에 `rerank_capped: bool`
   필드 추가. 별개 PR.

## 첫 행동 체크리스트

1. `git checkout -b spike/reranking-chunk-window-cap` (실행됨).
2. 본 design doc commit.
3. `src/trawl/reranking.py` 수정.
4. `benchmarks/reranker_stability_diag.py` 에 `--via-trawl` 추가.
5. Diagnostic 재실행 (direct + via_trawl).
6. Parity + `code_heavy_query`.
7. Gate decision + outcome note + PR.
