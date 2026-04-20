# Longform retrieval cost — chunk budget + BM25 prefilter — design (2026-04-20)

Branch: `feat/longform-retrieval-cost` (off `develop` at `17c96f0`)

Parent context:
[2026-04-20-c5-hierarchical-fetch-conclusion.md](2026-04-20-c5-hierarchical-fetch-conclusion.md)
— spike verdict `defer C5, file longform retrieval cost follow-up`.

## 문제

C5 premise spike에서 `retrieval_ms.p95 = 5057 ms` (pre-registered 1000 ms
임계치 초과). 페이지 크기(`page_chars.p95 = 157k`, `n_chunks_total.p95 =
277`)는 C5 임계치 아래였으므로 **C5(계층적 fetch)는 적합한 해법이 아님**.
원인은 embedding cost가 `n_chunks_total` 에 선형으로 비례 (≈ 10–12 ms /
chunk, bge-m3 / `:8081` batched). 드라이버:

| case | chunks | page_chars | retrieval_ms |
|---|---:|---:|---:|
| wiki_history_of_the_internet | 563 | 188,850 | 6,145 |
| arxiv_pdf | 261 | 109,737 | 5,091 |
| wiki_llm | 288 | 109,483 | 5,006 |
| korean_wiki_person | 190 | 61,245 | 3,793 |

ARCHITECTURE.md "Future work" 항목 없음 — 본 문제는 C5 spike 결과로 새로
filed 된 것. `notes/RESEARCH.md` 는 C5 conclusion에서 option 1
("chunk budget with heading-based prefilter") 로 pointing.

## 비-목표 (scope 제한)

- **`EMBEDDING_BATCH` 조정은 하지 않음.** C5 conclusion 의 option 2 는
  별도 PR. `CLAUDE.md` "Things NOT to change" 항목이라 tuning 전에 parity
  + WCXB 양쪽 재측정이 필요. 본 PR 범위 밖.
- **host-specific auto-subtree 도 하지 않음.** C5 conclusion option 3 은
  fetcher-level 변경이라 scope · risk 가 다르고, 프로파일 시스템과의
  상호작용이 있음. 본 PR 이후 별도 spike 가 필요하면 filing.
- **Reranker 는 건드리지 않음.** prefilter 는 embed 단계 앞에서만 작동.
  reranker 는 이미 prefilter 후 남은 `retrieve_k * 2` candidate 위에서
  도는 구조를 유지.
- **BM25 tokenizer는 C6 것을 그대로 재사용.** tuning은 C6 후속
  (RRF k spike)에서 하므로 중복하지 않음.

## 접근법

```
chunks ──┬──────────────────────────────────────────────────────────┐
         │                                                          │
         │ if len(chunks) <= chunk_budget  (no prefilter)            │
         │                                                          ▼
         │                                       dense cosine ranking
         │                                                          │
         │ else  ── BM25 rank over ALL chunks                       │
         │    └─► keep top `chunk_budget` indices ─► dense cosine ──┘
         │                                                          │
         ▼                                                          ▼
                                           (optional) RRF fusion on survivors
                                                          │
                                                          ▼
                                                 top-`retrieve_k`
                                                          │
                                                          ▼
                                                   reranker (unchanged)
```

### Prefilter 규칙

`src/trawl/retrieval.py::retrieve()` 에 `chunk_budget: int = 0` 인자 추가
(기본 0 = disabled). `chunk_budget > 0` 이고 `len(chunks) > chunk_budget`
일 때만 prefilter 발동.

Prefilter signal: **C6 의 `bm25_rank(query, chunk_texts)`** 를 그대로
재사용. 이미 Latin word / Hangul bigram / CJK char tokenizer + BM25Okapi
가 구현돼 있고 parity 15/15 증명됨. `chunk_texts` 는 retrieval.py가 이미
embedding 용으로 만들어 둔 것 (heading path 가 본문 앞에 prepend 된 형태)
이므로, prefilter는 **heading signal을 자동으로 포함**. 별도 heading-
only 점수 계산 필요 없음.

Prefilter 결과: 원래 `chunks` 인덱스 기준 top-`chunk_budget` 서브셋.
dense embedding · cosine · (optional) RRF 이 모두 이 서브셋 위에서만 돔.

### 왜 "top-N 유지" 인가 (bottom-quartile drop 과의 차이)

C5 conclusion 초안은 "drop bottom-quartile" 을 제안. 그 방식은 `pool -
pool/4 = 0.75 × pool` 가 되므로 **budget cap 이 없음** (563 청크 → 422
청크, 여전히 4.2s 급). 따라서 본 PR은 더 단순·결정적인 **"budget cap =
top-N"** 을 채택. quartile 대비 장점:

1. worst case 소요 시간 상한을 "budget × 10–12 ms" 로 고정.
2. 튜닝 knob 이 단일 숫자 (`chunk_budget`) 라 measurement 해석 쉬움.
3. `retrieve_k * 2` (reranker candidate window, 보통 10–24) 보다 한참
   큰 budget 선택 시 candidate 손실 위험 거의 없음.

### 기본값 및 환경 변수

- `TRAWL_CHUNK_BUDGET` — default **`0` (disabled)**. `1` 이상이면 해당
  청크 수를 budget 으로 사용. C6 와 같은 opt-in 패턴.
- `TRAWL_CHUNK_BUDGET=100` 권장 시작값 (**측정 후 revised from 150**).
  근거:
  - pre-registered 초안은 150. C5 spike의 `retrieval_ms ≈ 10–12 ms ×
    n_chunks_total` 회귀선을 그대로 사용 (150 × 10–12 ≈ 1.5–1.8 s).
  - 실측에서 per-chunk cost 가 **15-18 ms** (소규모 pool 일수록 per-call
    overhead 지배). budget=150 에서 korean_wiki_person (190 → 150) 은
    3216 → 2696 ms 로 gate 2500 ms 초과.
  - budget=100 에서 네 longform 케이스 모두 p95 ≤ 1895 ms, 4/4 rank-1
    identity 보존 (PASS 모두). 측정 상세는
    `benchmarks/results/longform-retrieval-cost/2026-04-19T23-50-46Z/report.md`.
  - budget=100 은 여전히 `_adaptive_k` 의 최대값 12 × reranker 2x =
    24 의 4배로, reranker candidate 선정 공간이 충분.
- MVP 에서 default-on 은 하지 않음. 측정 후 별도 PR.

### 통합 지점

`retrieval.py::retrieve()` signature 변경:

```python
def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
    hybrid: bool = False,
    chunk_budget: int = 0,     # NEW — 0 disables prefilter
) -> RetrievalResult: ...
```

`retrieve()` 내부:

1. chunk_texts 구축 (기존 heading-prepend 로직 그대로).
2. `if chunk_budget > 0 and len(chunks) > chunk_budget:`
   - `kept = bm25_rank(query, chunk_texts)[:chunk_budget]` (sorted index
     리스트, 원래 순서 보존 위해 `set(kept)` 사용 주의).
   - `kept_indices = sorted(kept)` (안정성).
   - `chunks` / `chunk_texts` 를 `kept_indices` 로 필터.
3. 기존 embedding loop 은 변경 없음 (다만 input 길이가 줄어듦).
4. `hybrid=True` + prefilter: dense ranking 과 sparse ranking이 모두
   **필터된 subset** 위에서만 계산됨. 이는 의도된 동작 — prefilter 가
   전체 pool을 이미 sparse-signal 기반으로 줄였으므로 RRF는 "dense vs
   sparse tie-break" 역할만 남음. (잠재적 측정 포인트: A/B 에서 hybrid 와
   prefilter 가 중복 signal을 쓰는지 관찰.)

`pipeline.py` 측 변경:

```python
chunk_budget = int(os.environ.get("TRAWL_CHUNK_BUDGET", "0"))
retrieved = retrieval.retrieve(
    query, chunks, k=retrieve_k,
    extra_query_texts=extras, hybrid=hybrid_flag,
    chunk_budget=chunk_budget,
)
```

`_build_profile_result` 의 profile_retrieval 경로도 동일하게 kwarg 전달
(현재 profile subtree 가 큰 페이지에서 retrieval 비용이 드는 동일 문제
해결에 기여).

### PipelineResult / telemetry

`PipelineResult` 에 새 필드 **추가**:

- `n_chunks_embedded: int = 0` — prefilter 후 실제 임베딩된 청크 수.
  `= n_chunks_total` 이 prefilter 미발동 의미. diagnostics + A/B
  analysis 용.

기존 `retrieval_ms` 는 그대로 사용 (prefilter ms + embed ms + cosine ms
합계). BM25 prefilter 는 50 청크 기준 <20 ms, 500 청크 기준 ~100 ms —
retrieval_ms 대비 무시 가능.

telemetry 스키마 (`src/trawl/telemetry.py`) 에는 `n_chunks_embedded` 1
필드만 추가. `TRAWL_CHUNK_BUDGET` 자체는 externally observable (env).

## 리스크

1. **BM25 prefilter 가 dense-only 가 잡을 수 있었던 청크를 탈락시키는
   시나리오.** 대표: 쿼리가 본문과 lexical overlap 이 거의 없고 의미만
   일치하는 경우 (예: "2020년대 초반의 대규모 언어모델 동향" 쿼리 →
   본문엔 "GPT-3", "Transformer scaling" 만 등장). → 측정으로 검증.
   회귀 발생 시:
   - (a) budget 상향 (300 정도).
   - (b) prefilter 를 hybrid 와 동일하게 RRF(dense-approx + BM25) 로
     바꿔 dense-lite signal 추가 — 단 이건 새로운 embedding 패스 없이
     불가능하므로 MVP에서는 안 함.
   - (c) prefilter 자체를 defer.
2. **Heading signal 의 실제 기여도 미측정.** retrieval.py가 이미 heading
   prepend 하지만, BM25 tokenizer 가 heading 에 더 큰 가중치를 주지
   않는다. 측정에서 문제 되면 chunk_texts prefix를 `(heading×2 +
   body)` 로 duplicate 하는 식의 heuristic 고려 (별도 PR).
3. **Reranker 입력 품질 감소.** reranker 는 현재 `retrieve_k * 2`
   candidate 를 받음. prefilter 로 budget=150 에 도달하면 그 후
   `retrieve_k * 2` 는 여전히 150 아래라 reranker 가 pick 할 공간은
   유지. 단 reranker 가 "dense 상위 + BM25 상위" 의 합집합 위에서 동작
   하던 C6 hybrid 기본값은 이제 "BM25 프리필터된 dense 상위"로 바뀜. 본
   설계에서 이건 의도된 것 (cost 절감이 목적).
4. **arxiv_pdf 처럼 heading 이 flat 한 소스**: PDF extraction 이
   heading 구조를 제한적으로만 보존. BM25 는 body text 위주로 동작.
   prefilter 가 noisy 해질 수 있음 — 측정 포인트.

## Pre-registered decision thresholds

본 PR 을 merge 하려면 아래 **전부** 만족:

| 측정 | baseline (budget=0) | experiment (budget=100) | gate | 실측 |
|---|---|---|---|---|
| `tests/test_pipeline.py` parity | 14/15 (pre-existing `kbo_schedule` fail on develop) | 14/15 (no new regression) | **required** | **PASS** — 동일 14/15, 추가 회귀 0 |
| Longform 4-case `retrieval_ms.p95` | ≥ 5000 ms | ≤ **2500 ms** | **required** (50% 감소) | **PASS** — overall p95 6002 → 1890 ms (69% 감소) |
| Longform 4-case rank-1 identity | 4/4 baseline ranked-1 chunk | ≥ **3/4** experiment keeps same rank-1 (after reranker) | **required** | **PASS** — 4/4 identity preserved |
| hybrid + budget cross (TRAWL_HYBRID_RETRIEVAL=1 + TRAWL_CHUNK_BUDGET=100) | 14/15 | 14/15 | **required** | **PASS** |

Longform 4-case: C5 spike의 worst-offender 4 (`wiki_history_of_the_
internet`, `arxiv_pdf`, `wiki_llm`, `korean_wiki_person`).

**Pre-existing 예외.** `kbo_schedule` 는 develop HEAD 에서 이미 FAIL
(develop 클린 체크아웃 `git stash && git checkout develop` 로 재현 확인).
본 PR scope 밖 — 별도 follow-up 으로 filing.

**Fail-stop rule (moot)**: 네 gate 모두 PASS. default-off 로 merge 진행.

### 실측 결과 요약

`benchmarks/results/longform-retrieval-cost/2026-04-19T23-50-46Z/report.md`
— 네 longform 케이스 × 2 모드 × 3 iteration:

| case | chunks | baseline p95 | exp p95 (budget=100) | reduction | rank-1 |
|---|---:|---:|---:|---:|:---:|
| wiki_history_of_the_internet | 563 | 6,179 ms | 1,221 ms | **80%** | y |
| arxiv_pdf | 96 | 1,758 ms | 1,750 ms | no-op (< budget) | y |
| wiki_llm | 288 | 4,997 ms | 1,857 ms | **63%** | y |
| korean_wiki_person | 190 | 3,276 ms | 1,895 ms | **42%** | y |

### 튜닝 여정 노트 (참고)

초안 budget=150 으로 측정했을 때 retrieval_ms.p95 gate fail — 예상
per-chunk cost 10–12 ms 가 실제 15–18 ms 였음 (소규모 pool 에서 per-call
overhead 지배). budget=100 으로 낮춰 재측정 → 모든 gate PASS. 측정이
pre-registered 값을 교정한 사례.

## 측정 계획

### Baseline pre-measurement

`benchmarks/longform_retrieval_cost_measure.py` (신규): C5 premise
script와 비슷한 형태. 입력 = longform 4-case (+ 전체 15 parity, code_
heavy_query 16 patterns). 각 케이스에 대해:

- budget=0 (baseline) 3회 median
- budget=150 (experiment) 3회 median

출력:

- `benchmarks/results/longform-retrieval-cost/<ts>/summary.json`
- `benchmarks/results/longform-retrieval-cost/<ts>/report.md` — gate
  별 pass/fail 표시.

### 유닛 테스트

- `tests/test_retrieval_chunk_budget.py` — `retrieve(chunk_budget=N)`
  가 N < len(chunks) 에서 prefilter 발동, N ≥ len(chunks) 에서
  no-op, N=0 에서 no-op 보장.
- `tests/test_bm25.py` — 기존. prefilter 자체는 BM25 API 호출만 하므로
  별도 보강 불필요.

### 파리티 매트릭스

`TRAWL_CHUNK_BUDGET=150 python tests/test_pipeline.py` — 15/15 필요.
`TRAWL_CHUNK_BUDGET=150 TRAWL_HYBRID_RETRIEVAL=1 python tests/test_
pipeline.py` — cross-interaction 확인.

## Follow-ups (이 PR scope 밖)

1. **budget auto-tuning.** 현재는 단일 상수. 쿼리 · 페이지 유형별로
   budget 을 달리 가지는 분기 (예: PDF = 250, wiki = 150, default=0)
   는 데이터 더 모이면 고려.
2. **host-specific default.** `wikipedia.py` / `pdf.py` 가 자체적으로
   budget 힌트를 반환하는 API. C5 conclusion option 3 의 축소판.
3. **prefilter signal 교체.** BM25 대신 bge-m3 sparse output(C6.5) 이
   구현되면 교체 가능. sparse 는 dense 와 같은 encoder 를 거치므로
   semantic gap 이 BM25 보다 작음.
4. **default-on 전환.** longform 4-case 외에 실제 agent_patterns 전체
   + 1-2 주 real usage 측정 후.

## 파일 변경 예상

- `src/trawl/retrieval.py` — `chunk_budget` kwarg + prefilter 로직.
- `src/trawl/pipeline.py` — env 읽어서 kwarg 전달 (full pipeline +
  profile retrieval 두 경로).
- `src/trawl/telemetry.py` — `n_chunks_embedded` 필드.
- `src/trawl/pipeline.py` — `PipelineResult.n_chunks_embedded` 필드.
- `benchmarks/longform_retrieval_cost_measure.py` — 신규 측정 러너.
- `benchmarks/results/.gitignore` — 기존대로 untouched.
- `tests/test_retrieval_chunk_budget.py` — 신규 유닛 테스트.
- `CHANGELOG.md` — Unreleased 섹션에 항목 추가.
- `CLAUDE.md` — "Things NOT to change" 표에 `TRAWL_CHUNK_BUDGET=150`
  의 근거 (측정 링크) 한 줄 추가. 값 조정 시 재측정 필요.
- `README.md` — `TRAWL_CHUNK_BUDGET` 환경변수 1줄 언급 (features 섹션).

`src/trawl_mcp/` 변경 없음 (kwargs 외부 노출은 이번 PR scope 밖).
