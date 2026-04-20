# C6 follow-up — HyDE compound identifier spike — design (2026-04-20)

Branch: `spike/hyde-compound-identifiers` (off `develop` at `ba0eb39`).

Parent context:
[2026-04-20-bm25-id-aware-tokenizer-design.md](2026-04-20-bm25-id-aware-tokenizer-design.md)
— gate (b) rejected: corpus-side compound tokens don't help when
queries lack identifiers. Outcome note
(`notes/bm25-id-aware-measurement.md`) promoted **query-side
augmentation** as the next candidate: HyDE already emits identifier-
rich hypothetical answers, but those answers only reach the *dense*
embedding path. This spike tests whether routing HyDE output into the
*sparse* BM25 query too closes the MDN lexical gap.

## 문제

`claude_code_mdn_fetch_api` 실패 (`chunks_contain_any: ["JSON.stringify",
"Content-Type", "method:"]` miss). 쿼리 `"send a POST request with a
JSON body using fetch"` 에는 compound identifier 가 전혀 없음. 코드
블록 청크에는 정답이 들어있지만 BM25 sparse ranker 가 이를 밀어올릴
signal 없음 (RRF-k spike + id-aware tokenizer spike 둘 다 이 경로를
기각).

현재 HyDE 프롬프트는 "Include specific named entities (people, places,
dates, numbers) that would appear in a real answer" 라고 이미 지시.
Gemma 4 는 API 관련 쿼리에 대해 실제로 `fetch()`, `JSON.stringify`,
`Content-Type: application/json` 류 identifier 를 포함한 답을 생성
(메모리: `hyde.py` 예시). 단, 그 출력은 `extra_query_texts` → dense
embedding 평균 에만 쓰이고 BM25 쪽에는 안 전달됨:

```python
# src/trawl/retrieval.py::retrieve()
sparse_ranked = bm25_rank(query, chunk_texts)   # ← raw `query`, not augmented
```

가설: `bm25_rank(query + " " + " ".join(extras), chunk_texts)` 로
바꾸면 HyDE 가 emit 한 identifier 가 sparse 쪽 signal 로도 활용돼
MDN 류의 lexical gap 이 해소된다.

## 비-목표

- **HyDE 프롬프트 수정 금지.** 현 프롬프트가 이미 "named entities"
  요구. 프롬프트 튜닝은 별도 spike — 본 spike 는 plumbing change 만.
- **HyDE default-on 전환 금지.** HyDE 는 per-call 15-20s latency.
  `code_heavy_query` 15/16 이 이미 HyDE 없이 passing 하는데 모두 에
  HyDE 비용을 강요할 이유 없음.
- **retrieval.py 의 dense 경로 변경 금지.** dense 는 이미 HyDE 를
  활용 중. 변경 범위는 sparse 쪽 1줄 + docstring.
- **비-hybrid 경로 변경 금지.** `hybrid=False` 에서는 BM25 를 안
  쓰므로 augmentation 도 no-op. 단순 유지.
- **BM25 prefilter (chunk budget) 변경 금지.** longform 경로는 별개.
  `chunk_budget>0` 분기에서는 기존대로 raw query 사용 (BM25 prefilter
  는 HyDE 와 orthogonal; 본 spike 의 A/B 는 fusion-stage BM25 만).

## 접근법

### 단계별 측정

HyDE 는 이미 구현되어 있고 opt-in 이다 (`use_hyde=False` default).
본 spike 는 **두 단계** 로 측정:

- **Mode B — HyDE on, BM25 사용은 raw query** (현 코드 상태).
  측정 질문: HyDE 의 dense augmentation **단독으로도** MDN 이
  풀리는가? 만약 yes → code change 불필요, recommendation 만. 만약
  no → Mode C 의 가치 증명.
- **Mode C — HyDE on + BM25 query 에 HyDE 출력 concat** (env
  gated). 측정 질문: BM25 query augmentation 이 Mode B 대비 **추가**
  로 MDN (혹은 다른 패턴) 을 고치는가?

Baseline:
- **Mode A — HyDE off, hybrid on** (현 default). RRF-k spike 및
  id-aware tokenizer spike 의 baseline 과 동일. MDN FAIL 유지 기대.

### 코드 변경 (Mode C 용)

`src/trawl/retrieval.py` 의 sparse 경로 1줄:

```python
# Before
sparse_ranked = bm25_rank(query, chunk_texts)

# After
bm25_query = query
if _extras_in_bm25_enabled() and extra_query_texts:
    bm25_query = query + " " + " ".join(extra_query_texts)
sparse_ranked = bm25_rank(bm25_query, chunk_texts)
```

env var `TRAWL_BM25_EXTRAS=1` (default `0`) 로 gate. gate (a) 시 env
var 유지한 채 opt-in 으로 ship; gate (b) 시 revert.

docstring 도 업데이트: "`extra_query_texts` is also concatenated into
the BM25 query when ``TRAWL_BM25_EXTRAS=1``".

구현 복잡도: 추가 env var helper + 1줄 분기 + docstring. 유닛 테스트
는 BM25 layer 에서 커버되므로 (HyDE 가 concat 된 쿼리 vs 원본 쿼리
의 tokenize / rank 는 이미 테스트 중) 신규 unit 추가 최소.

### 측정 스위트

3 modes × 16 `code_heavy_query` patterns × 2 iter = 96 runs. 각
hyde-on mode 는 추가 15-20s. 추정 총 시간 30-45분.

Modes:
1. `hybrid_hyde_off` (baseline) — `TRAWL_HYBRID_RETRIEVAL=1`, `use_hyde=False`.
2. `hybrid_hyde_on_dense` — `TRAWL_HYBRID_RETRIEVAL=1`, `use_hyde=True`,
   `TRAWL_BM25_EXTRAS=0` (기존 경로).
3. `hybrid_hyde_on_full` — `TRAWL_HYBRID_RETRIEVAL=1`, `use_hyde=True`,
   `TRAWL_BM25_EXTRAS=1` (실험).

**Note**: `dense_only` 는 이번에 측정 안 함 — prior spike 가 이미 확보한
reference. mode-A baseline 은 `hybrid_hyde_off`.

Parity (15/15 guard):
- `hybrid_hyde_off` (baseline 재확인, 이미 여러 spike 에서 파리티 확정)
- `hybrid_hyde_on_full` (full 실험 경로의 wider regression guard)
- `hybrid_hyde_on_dense` 는 기존 코드와 동일 (HyDE 는 dense 만 증강) —
  매 spike 에서 재측정 안 해도 됨. 단, runner 는 optional 로 지원.

## Pre-registered decision gates

**baseline = `hybrid_hyde_off`** (현 default).

| 결과 | 조건 | 액션 |
|---|---|---|
| **(a1) 채택, 코드 변경 없음** | `hybrid_hyde_on_dense` 가 baseline 대비 (1) 파리티 15/15, (2) `net_assertion_delta ≥ +1`, (3) `flipped_to_fail == 0` | `docs` PR: HyDE 사용 권장 가이드 (code_heavy_query 쿼리에 `use_hyde=True`). `retrieval.py` 변경 없음. 메모리 / CLAUDE.md 에 가이드 pointer. |
| **(a2) 채택, 코드 변경 포함** | (a1) 미충족이지만 `hybrid_hyde_on_full` 이 `hybrid_hyde_on_dense` 대비 **추가로** (2)(3) 충족 — 즉 BM25 augmentation 이 *incremental* 효과 | `feat(retrieval): opt-in BM25 extras concat (measurement-driven)` PR. env var 유지, default 0. 가이드 PR 동반. |
| **(b) 기각** | 어떤 mode 도 baseline 대비 net delta ≥ 1 이 아님 | design doc + runner + note 만. 코드 변경 revert. C6 follow-up 은 lexical gap 해소 시도 종결. |
| **(c) 파리티 회귀** | `hybrid_hyde_on_*` 중 하나라도 파리티 < 15/15 | 해당 mode revert. 회귀 case 를 note 에 기록. |

### 경로별 판단 규칙

- (a1) 가 트리거되면 (a2) 로 추가 이동하지 않음 — 코드 변경 최소화.
  BM25 augmentation 은 HyDE-only 로 풀리지 않는 잔여 failure 가 있을
  때만 가치.
- (a2) 로 가면 그 시점에서 `hybrid_hyde_on_full` 의 net delta 가
  baseline 대비도 (a1) 조건 만족해야 함 (즉 ≥ +1, fail flip 0). Full
  경로가 dense 경로 대비 역효과 내면 반대로 의심 — 지금 가설은 incremental.

### Threshold 선택 근거

- **+1 assertion delta**: C6/RRF-k/tokenizer spike 와 동일 기준.
  통상 MDN flip-to-pass 가 유일한 후보 — 만약 다른 경로에서 fail flip 이
  생기면 보통 spurious. 보수적 채택.
- **flipped_to_fail == 0**: HyDE 는 dense 경로 전체에 영향. 기존에
  dense 단독으로 잘 풀리던 패턴이 HyDE 의 noise 로 rank-1 을 잃는 risk.
- **baseline = `hybrid_hyde_off`** (아닌 `dense_only`): 이 spike 의
  조사 대상은 HyDE 의 signal 기여분. hybrid 는 유지한 채 HyDE on/off 만
  A/B. hybrid 효과는 RRF-k spike 에서 이미 기록.

## 파일 변경 (spike 전체)

- `docs/superpowers/specs/2026-04-20-hyde-compound-identifier-design.md`
  — 본 문서 (신규).
- `src/trawl/retrieval.py` — sparse 경로 1줄 + helper + docstring 변경
  (Mode C 구현). gate (a2) 통과 시에만 유지.
- `benchmarks/hyde_compound_id_sweep.py` — 측정 러너 (신규).
  gate 결과와 무관하게 유지.
- `notes/hyde-compound-id-measurement.md` — 결과 + 결론 (gitignored).

## 측정 계획

### 실행 순서

1. `mamba activate trawl`; `:8081` (embed) / `:8083` (rerank) /
   `:8082` (utility LLM for HyDE) healthcheck. HyDE 가 `:8082` 를
   써야 하므로 **이 spike 는 utility LLM 서버가 필수**.
2. retrieval.py + helper 구현 (Mode C 코드).
3. `python benchmarks/hyde_compound_id_sweep.py --dry-run` — 계획.
4. `python benchmarks/hyde_compound_id_sweep.py` — 본 측정.
   - 3 modes × 16 patterns × 2 iter = 96 runs.
   - HyDE 비용 포함 ~35-45 분.
   - 결과: `benchmarks/results/hyde-compound-id-sweep/<ts>/`.
5. Parity:
   - `hybrid_hyde_off` (baseline 재확인): `TRAWL_HYBRID_RETRIEVAL=1
     python tests/test_pipeline.py` → 15/15 요구.
   - `hybrid_hyde_on_full`: `TRAWL_HYBRID_RETRIEVAL=1
     TRAWL_BM25_EXTRAS=1 python tests/test_pipeline.py --hyde` →
     15/15 요구.
6. `notes/hyde-compound-id-measurement.md` 작성 + gate decision 적용.

### Summary.json 스키마

```json
{
  "generated_at": "...",
  "iterations": 2,
  "modes": ["hybrid_hyde_off", "hybrid_hyde_on_dense", "hybrid_hyde_on_full"],
  "baseline_mode": "hybrid_hyde_off",
  "parity": {
    "hybrid_hyde_off":     {"pass": 15, "total": 15, "ok": true},
    "hybrid_hyde_on_full": {"pass": 15, "total": 15, "ok": true}
  },
  "per_mode": { ... },
  "diff_vs_baseline": {
    "hybrid_hyde_on_dense": { flipped_to_pass, flipped_to_fail, net_assertion_delta, ... },
    "hybrid_hyde_on_full":  { ... }
  },
  "gate_decision": "a1_adopt_docs_only" | "a2_adopt_with_code" | "b_reject" | "c_parity_regression"
}
```

## 리스크

1. **HyDE non-determinism.** Gemma 4 의 output 이 random-sampled
   (temperature=0.7). iter 2 만으로는 HyDE 가 MDN 관련 identifier 를
   일관되게 emit 하는지 확신 어려움. 완화: iter 3 옵션 지원, gate
   통과 전 HyDE 출력 샘플 확인 (telemetry 또는 note 에 기록).
2. **HyDE latency cost in A/B.** HyDE on mode 가 off mode 대비 현저
   히 느림 → retrieval_ms 비교는 의미 없음. 본 spike 는 **assertion
   pass rate 만** 목표, retrieval_ms 는 regression guard (예산 초과 여부)
   차원 에서만 기록.
3. **HyDE server (:8082) 불안정 가능성**. 최근 reranker :8083 이 500
   error 내는 이슈 (id-aware spike note 참조) → HyDE 서버도 같은 호스트
   면 동반 불안정. 실패 시 HyDE 는 empty string 반환 (`hyde.expand()`
   예외 처리) — 기능상 hyde_off 와 동등. 따라서 빈 HyDE 출력 비율을
   per-mode 에 기록.
4. **`use_hyde=True` 는 category 별 적용이 아니라 호출 별**. Gate
   (a1) 일 때 "권장" 가이드만 내면 실제 사용자가 언제 켜야 할지 모호
   함. 완화: 가이드 문서 예시로 "code_heavy_query / API reference URL"
   쿼리 시 `use_hyde=True` 를 매개변수로 추가.
5. **BM25 query 길이 증가.** HyDE 출력 200-300 토큰 concat → BM25
   query 길이 급증. BM25Okapi 는 길이 normalization 되어 있어 query
   쪽에는 영향 없지만, 희귀 단어가 많이 섞이면 rank 가 분산. Mode B
   vs C 비교로 판별.
6. **Parity set 에 code_heavy_query 가 거의 없음.** `test_pipeline.py`
   의 15 case 는 대부분 prose / finance / wiki 류 — HyDE augmentation
   이 여기에서 negative 영향 주는지 측정. 만약 조기에 regression 나오면
   HyDE 는 category-conditional 적용 (별도 spike 로 이동).

## Follow-ups (본 spike 범위 밖)

1. **HyDE 프롬프트 튜닝** — gate (b) 시 다음 후보. 현 프롬프트가
   identifier 를 충분히 emit 하지 않으면 "Include API identifier
   names when the query describes library or framework usage" 명시 추가.
2. **Category-conditional HyDE default**. `use_hyde=True` 를
   `code_heavy_query` 카테고리에만 적용할 수 있게 pipeline 시그니처
   확장. gate (a1/a2) 시 follow-up PR.
3. **HyDE 서버 이중화 / fallback**. `:8082` 불안정 시 empty HyDE 로
   fallback 되지만, retry 또는 대체 모델 (예: :8080 으로 재라우팅)
   option. 별 spike.
4. **HyDE cache**. 동일 쿼리 반복 호출 시 cached HyDE 재사용. 실사용
   에서 per-session 쿼리 반복 많을 때 유효. `TRAWL_HYDE_CACHE_TTL`
   env var. 별 spike.

## 첫 행동 체크리스트

1. `git checkout -b spike/hyde-compound-identifiers` (실행됨, HEAD =
   `ba0eb39`).
2. 본 design doc commit.
3. `src/trawl/retrieval.py` 최소 변경 (env gate + BM25 query 증강).
4. `benchmarks/hyde_compound_id_sweep.py` 작성.
5. HyDE 서버 (`:8082`) healthcheck — **없으면 spike 중단하고 사용자
   에게 알림**. `:8082` 가동 후 재개.
6. 측정 + 파리티.
7. gate decision + outcome note.
