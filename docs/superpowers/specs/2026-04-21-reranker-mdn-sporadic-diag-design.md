# Reranker MDN sporadic 500 — design (2026-04-21)

Branch: `spike/reranker-mdn-sporadic-diag` (off `develop` post-0.4.1).
Pre-registered follow-up from the **0.4.1 carry-forward caveat**
(`CHANGELOG.md` "Known limitations") and the 1순위 entry in
`notes/next-session-2026-04-21-followups.md`.

## 관찰된 이슈

0.4.0 → 0.4.1 사이 6개 spike 측정 (PR #31/#32/#33/#34/#36/#38)
에서 `claude_code_mdn_fetch_api` 실행 시 `bge-reranker-v2-m3` at
`localhost:8083` 가 **작은 payload 로도 간헐 HTTP 500** 반환:

```
reranker unavailable, falling back to cosine: Server error '500 Internal Server Error'
for url 'http://localhost:8083/v1/rerank'
```

- PR #38 shipping gate (2026-04-21 CI) 직전 측정 시에도 재현.
- Typical payload: ~8 chunks × ~1-2 k chars = 총 **1-2 k chars**.
  이는 PR #36 에서 확인한 payload-size threshold (40 k / 50 k chars
  사이) 와 무관 — **다른 failure mode**.
- `reranking.py` 의 `except` 가 catch → **cosine fallback**. 결과
  assertion (`chunks_contain_any: JSON.stringify / Content-Type /
  method:`) 은 대부분 PASS (shadow-DOM unwrap 덕분에 텍스트 자체는
  존재) — **blind spot**: rerank 의존 metric 이나 rerank 만 붙잡는
  top-k 랭킹 변화는 cosine fallback 으로 되돌려지고 있음.

## 비-목표

- **`reranking.py` / `fetchers/playwright.py` 수정 금지.** 본 spike
  는 진단. 수정은 별도 spike 에서 pre-registered gate 통과 후.
- **llama-server 재시작 / 모델 교체 / 설정 변경 금지.** 관찰만.
- **`code_heavy_query` 전수 재측정 금지.** 본 failure 는 MDN
  pattern 에 집중 — MDN 재생 만 측정.
- **D2 (40 k threshold) 재검증 금지.** 이미 `reranking.py` 에 cap
  들어가 있고 PR #38 에서 확정. 본 spike 는 cap 아래 (< 40 k, 주로
  1-2 k) payload 에서의 500 을 조사.

## 가설 후보

| 코드 | 가설 | 식별 방법 |
|---|---|---|
| **H1** 동시성 / slot 충돌 (다른 tenant 의 `:8083` 점유) | reranker 전용 서버라 낮지만 가능 | burst 간 canary vs 외부 동시 호출 구분 — 동시에 `trawl-mcp` 류 실행 되는 타이밍 과 failure cluster 비교 |
| **H2** 특정 불변 payload 가 100 % 실패 (KV cache / Unicode edge) | MDN 코드 블록 내 특수 문자 / 비정형 whitespace | 같은 payload × 200 반복 → rate 집계. > 10 % 반복 실패면 payload 원인. |
| **H3** keep-alive TCP 상태 drift (long idle) | curl 로 재현하면 구분 가능 | inter-request gap 0 ms / 500 ms / 5 s 조합 — 특정 gap 만 실패하면 keep-alive issue. |
| **H4** shadow-DOM unwrap 이 주입한 HTML-escape entity (`&lt;`, `&amp;`) 가 서버 토크나이저 를 비정상 분기 | PR #34 이후 관측 시작 한 가능성 | 원본 payload vs HTML-escape stripped 변형 비교. stripped 가 급격히 실패율 감소 하면 H4. |
| **H5** bge-reranker-v2-m3 자체의 non-deterministic edge | 동일 입력 에서 실패 / 성공 혼재 | H2 반복 실험 결과로 판정. 무작위 낮은 rate (~0.5-2 %) 이면 H5. |

## 접근법

독립 diagnostic script: `benchmarks/reranker_mdn_sporadic_diag.py`.
MDN 페이지 를 `fetch_relevant()` 로 **단 1회** 실행 → rerank 입력
(`documents` 리스트 + `query`) 을 저장. 이후 **동일 payload × 반복
× 변주** 로 `:8083` 직접 HTTP POST 하여 500 재현 및 조건 식별.

### 측정 차원

1. **Repetition** — 동일 payload × N (default 200) 반복. Baseline
   실패율.
2. **Inter-request gap** — 3 sweep 실행:
   - gap = 0 ms (as-fast-as-possible)
   - gap = 500 ms
   - gap = 5 000 ms
   각 sweep N=50. keep-alive drift (H3) 검출.
3. **Unicode / escape strip** — 2 variant:
   - 원본 `documents` (shadow-DOM unwrap 결과)
   - `html.unescape` + ASCII-only filter + 표준 whitespace 치환
   각 variant × 50 반복. H4 검출.
4. **Canary** — 매 20 req 마다 MDN 아닌 고정 payload (e.g. React
   `useEffect` docs 의 rerank 입력) 1 request 삽입. MDN 전용 실패
   vs 범용 실패 구분 (H1 / H2 의 MDN 특정성 확증).

총 scenario: ~500-700 HTTP calls. 10-15 분 소요.

### 기록할 데이터

각 request:
- `index`, `sweep` (repetition / gap_0 / gap_500 / gap_5000 / strip / canary)
- `http_status`
- `elapsed_ms`
- `n_docs`, `doc_char_total`
- `variant` (source: MDN / MDN_stripped / canary_react)
- `error_message` (실패 시)

출력:
- `benchmarks/results/reranker-mdn-sporadic-diag/<ts>/diag.json` —
  raw per-request data.
- `diag.md` — 사람 리포트: sweep 별 fail rate, variant 별 fail rate,
  canary 비교, 제안 D-gate.

### MDN payload 캡처

`benchmarks/reranker_mdn_sporadic_diag.py --capture` 서브커맨드 로
먼저 MDN 페이지 payload 를 json 으로 dump. 측정 은 dump 파일 을
읽어 직접 HTTP POST — MDN 서버 에 재방문 안 함 (부하 X).

캡처 할 실제 payload:
- URL: `https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch`
- Query: `send a POST request with a JSON body using fetch`
- 환경: 현 `reranking.py` 가 build 하는 `documents` 를 그대로 (title
  prefix 포함 / cap 적용 결과).

## Pre-registered decision gates

본 spike 는 코드 변경 없음. **6 가지 결정 분기** 로 다음 spike
방향 확정.

| 코드 | 조건 (raw data 에서) | 해석 | 다음 spike |
|---|---|---|---|
| **D0** | 전체 실패율 < 0.5 % | 본 진단 시점 stable. | 재발 시 재조사. 이 diag 만 보관. |
| **D1** (H1) | canary payload 실패율 ≈ MDN 실패율 AND 동시 활동 과 cluster 상관 | 서버 공용 tenant 영향 | `trawl_mcp` slot pinning (TRAWL_RERANK_SLOT?) 추가 spike. 사용자 infra 확인 |
| **D2** (H2) | 동일 payload 반복 중 > 10 % 실패 AND MDN 전용 (canary PASS) | 특정 payload 가 서버 토크나이저 특이 분기 | 문자열 bisect 로 trigger 식별 → `_build_documents` strip/escape 수정 |
| **D3** (H3) | gap 5 s 에서만 실패율 급증 OR gap 0 ms 에서만 실패율 급증 | TCP keep-alive drift | `reranking.py` client reuse → per-request `with` 또는 per-call connection recycle |
| **D4** (H4) | stripped variant 실패율 원본 대비 >2x 낮음 | HTML entity 가 서버 tokenizer 이슈 | `fetchers/playwright.py._unwrap_shadow_dom` 에서 `textContent` 를 escape 안 한 채 그대로 inject (escape 제거 실험) 또는 `_build_documents` 에서 `html.unescape` |
| **D5** (H5) | 모든 그룹 fail rate 가 0.5 % 초과 하나 > 2x baseline 도 없음 | 완전 무작위 non-deterministic | `reranking.py` 에 retry + jitter 도입 논의 (별개 spike) |

## 파일 변경

- `docs/superpowers/specs/2026-04-21-reranker-mdn-sporadic-diag-design.md`
  — 본 문서 (신규, PR 포함).
- `benchmarks/reranker_mdn_sporadic_diag.py` — diagnostic script (신규, PR 포함).
  `--capture` (MDN payload dump) + default (측정 실행) 두 서브 기능.
- `notes/reranker-mdn-sporadic-diag-outcome.md` — 결과 + D-gate 결정
  (gitignored).

`src/trawl/` 변경 **없음**.

## 측정 계획

1. `curl :8083/health` 200 확인.
2. **Capture phase** — `reranker_mdn_sporadic_diag.py --capture`
   실행. MDN URL 1회 fetch → `documents` json 덤프.
3. **Canary capture** — React `useEffect` URL 로 동일 작업. 변주용.
4. **Diag sweeps**:
   - Sweep A (repetition): MDN payload × 200, gap 100 ms.
   - Sweep B (gap_0): MDN payload × 50, gap 0 ms.
   - Sweep C (gap_500): MDN payload × 50, gap 500 ms.
   - Sweep D (gap_5000): MDN payload × 50, gap 5 000 ms.
   - Sweep E (strip): stripped MDN payload × 50, gap 100 ms.
   - Sweep F (canary): React payload × 50, gap 100 ms. 매 20 req
     마다 MDN payload 1 삽입.
5. 분석 + D-gate 적용 + outcome note.

### Exit code

- 0 — 측정 성공 (D-gate 결정 과 무관).
- 2 — `:8083` unreachable (health 실패).
- 3 — capture phase 실패 (MDN 차단 / stealth 문제).

## 리스크

1. **재현 실패 (D0).** sporadic 이라 관측 시점 에 0 % 나올 수 있음.
   이 경우 도 "현 시점 stable" 신호 로 가치 있음. 사용자 알림 필요.
2. **MDN 서버 영향.** 측정 단계 는 `:8083` 에만 부하 (동일 payload
   replay). MDN 서버 영향 zero. Capture 는 1 회 만.
3. **다른 tenant `:8083` 점유.** 10-15 분 짜리 500-700 req workload
   — PR #36 의 PR 설명 과 동일 맥락. reranker 전용 서버 가정 하에
   낮음.
4. **`documents` 포맷 drift.** `_build_documents` 가 변경 되면 캡처
   데이터 stale. 본 spike 시작 시 develop HEAD (`a2c49a6` 이상) 에
   서 capture → 같은 브랜치 에서 측정. 변경 하지 않음.

## 타이밍

설계 (본 문서) ~20 min, 캡처 + 스크립트 ~45 min, 측정 ~15 min,
outcome note + PR ~20 min. 총 ~100 min.

## 예상 산출

- PR 1 개: `spike(reranker): MDN sporadic 500 diagnostic — D?`
  (코드 변경 없음 예상; 원인 확정 시 별도 follow-up spike PR 분리).
- 본 spike 결론 은 다음 릴리즈 노트 의 "Research / Tooling" 또는
  "Fixed" 섹션 에 요약.
