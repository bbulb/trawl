# C6 follow-up — identifier-aware BM25 tokenizer spike — design (2026-04-20)

Branch: `spike/bm25-id-aware-tokenizer` (off `develop` at `000e985`).

Parent context:
[2026-04-20-c6-rrf-k-tuning-design.md](2026-04-20-c6-rrf-k-tuning-design.md)
and its conclusion in `c6_rrf_k_tuning_outcome.md` (auto-memory).
RRF-k spike closed gate (b) — `k=60` retained — and filed two
follow-ups. This spike addresses the second:
`code_heavy_query` 의 잔존 실패 중 `claude_code_mdn_fetch_api` 가
**tokenizer 해상도 부족** 에서 온다는 가설.

## 문제

C6 hybrid A/B 및 RRF-k sweep 에서 `mdn_fetch_api` 는 모든 k ∈
{10, 30, 60, 100} 에서 `chunks_contain_any:
["JSON.stringify", "Content-Type", "method:"]` 를 miss 한다.
청크 풀은 22 개로 충분 (retrieval 이전에 잃은 게 아니고), 필요한
키워드는 코드 블록에 존재.

Query = "send a POST request with a JSON body using fetch". 쿼리
측 tokens (현재 tokenizer):

```
["send", "a", "post", "request", "with", "a", "json", "body", "using", "fetch"]
```

코드 블록 청크 측 (예: `JSON.stringify({answer: 42})` 포함):

```
[..., "json", "stringify", ..., "content", "type", "method", ...]
```

`JSON.stringify` → `["json", "stringify"]`. `json` 은 쿼리에도
있지만 MDN 의 prose 청크에 빈도 높음 → BM25 IDF 로는 generic.
`stringify` 는 쿼리에 없음 → hit 0. `Content-Type` 도 동일하게
`["content", "type"]` 으로 분해돼 IDF 효과가 sub-word 로 sub-linear.

**가설**: dotted / hyphenated identifier 를 **compound 토큰으로도
동시 방출** 하면 (`"json.stringify"` 를 전체 토큰으로 추가) 쿼리가
compound 를 포함한 경우 IDF 우위로 BM25 가 코드 블록을 밀어올림.

단, MDN query 에는 `JSON.stringify` 가 **안 나온다**. 그럼 어떻게
helper 되는가?

해설: BM25 는 쿼리 토큰 ⊂ 코퍼스 토큰이 맞아야 signal 을 만든다.
쿼리에 `json.stringify` 없으면 compound 토큰 그대로는 쓸모 없음.
그러나 **sub-token `stringify` 는 여전히 동시 방출**. `stringify`
는 코퍼스 전체에서 희귀 (MDN 의 prose 청크엔 잘 안 나옴) ⇒ 높은
IDF ⇒ 쿼리에 없어도 다른 sparse-rank 신호에 도움이 안 된다.

**실제로 도움이 되는 경로는 두 가지**:

1. **코퍼스 쪽 토큰 다양화** — `JSON.stringify` 가 `{"json",
   "stringify", "json.stringify"}` 3 토큰을 emit 한다. `stringify`
   의 term frequency 는 변함없고, `json` 의 term frequency 도
   변함없지만 compound 가 한 번 더 emit 되므로 **total terms 수가
   늘어나서 doc length 가 증가**. BM25 는 doc length normalization
   이 있어 이는 역효과. → 이 경로는 gate 에 정량적으로 부정적.

2. **쿼리 쪽 compound 매칭** — 쿼리에 identifier 가 포함된 경우
   (stackoverflow_python_async_subprocess 의 "asyncio subprocess
   with timeout" 처럼 `wait_for` / `asyncio.gather` 류가 쿼리 또는
   HyDE 출력에 있을 때) compound token 매칭으로 doc rank 가 뛴다.

즉 본 spike 의 효과는 **MDN 패턴에는 거의 없고, identifier 가
쿼리/HyDE 에 등장하는 다른 code_heavy 패턴에 국한**된다. 이 점은
gate 에 반영 — "+1 flip" 이 일어나는 패턴이 MDN 일 필요는 없고,
어떤 code_heavy 패턴이어도 된다.

### Stackexchange 실패 패턴은 별도

- `stackoverflow_python_async_subprocess`: 청크 8개, 코드 블록 내
  키워드 0. extraction 에서 fence 유실 의심 — 1순위 spike
  (`stackexchange extraction diagnostic`) 가 별도로 다룸. 토크나이저
  변경은 청크에 존재하지 않는 키워드를 만들어내지 못함.
- `serverfault_nginx_reverse_proxy`: 청크 2개. 동일.
- 본 spike 는 두 패턴이 baseline/experiment 양쪽에서 fail 유지 될 것
  으로 예측. flipped_to_fail 에 포함되지 않으면 OK.

## 비-목표

- **extraction 수정 금지.** Stackexchange chunk 유실은 별개 spike.
- **한글/CJK 토큰화 경로 변경 금지.** regression guard.
- **RRF k 재조정 금지.** RRF-k spike 에서 k=60 확정.
- **hybrid default-on 전환 금지.** C6 conclusion 유지 — default off.
- **tokenizer default-on 전환 금지.** 본 spike 에서 env var 로
  gating, default off. gate (a) 통과 후 별도 PR 로 default flip 판단.
- **reranker 튜닝 금지.** 현재 `bge-reranker-v2-m3` 설정 유지.

## 접근법

### 코드 변경 — `src/trawl/bm25.py`

Opt-in env var `TRAWL_BM25_IDENTIFIER_AWARE=1` (default `0`) 로
tokenizer 분기. 분기 내부에서 두 regex 추가:

```python
_DOTTED_IDENTIFIER   = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_HYPHENATED_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z][A-Za-z0-9]*)+")
```

Latin word regex 는 그대로 두고, identifier-aware 모드에서는
compound token 을 **추가로** emit. Sub-token 은 기존 Latin word
regex 가 이미 emit 하므로 중복 로직 없이 append only 방식.

토크나이저 하위 호환: identifier_aware=0 에서는 기존 동작과 **byte-
equivalent** — 단위 테스트로 재확인.

### 측정 러너 — `benchmarks/bm25_id_aware_sweep.py`

`benchmarks/c6_rrf_k_sweep.py` 의 구조 재사용. 3 mode:

1. `dense_only` — reference. `TRAWL_HYBRID_RETRIEVAL=0`.
2. `hybrid_legacy` — `TRAWL_HYBRID_RETRIEVAL=1 TRAWL_HYBRID_RRF_K=60
   TRAWL_BM25_IDENTIFIER_AWARE=0` (현 default).
3. `hybrid_id_aware` — `TRAWL_HYBRID_RETRIEVAL=1 TRAWL_HYBRID_RRF_K=60
   TRAWL_BM25_IDENTIFIER_AWARE=1` (신규).

Slice = `code_heavy_query` 16 patterns. Iterations = 2.
`fetch_relevant` direct call — `tests/test_pipeline.py` 통해서가
아님. top-1 chunk signature, assertion pass, retrieval_ms 기록.

### 파리티 — `tests/test_pipeline.py`

`hybrid_id_aware` 모드로 1회 실행 (dense_only / hybrid_legacy 는
이미 C6 + RRF-k spike 에서 검증). **이 경로의 15/15 가 최우선
guard** — tokenizer 가 비-code 페이지 (wiki/PDF/finance) 의
chunk ranking 을 망가뜨리면 여기서 잡힘.

파리티 재실행은 `hybrid_legacy` 와도 1회 해서 regression 아닌
것 을 재-confirm — 총 2회 (k=60 에서 legacy vs id_aware).

## Pre-registered decision gates

**baseline = `hybrid_legacy`**. `dense_only` 는 reference 용.

| 결과 | 조건 | 액션 |
|---|---|---|
| **(a) 채택** | `hybrid_id_aware` 에서: (1) 파리티 `test_pipeline.py` 15/15, (2) `hybrid_legacy` 대비 `net_assertion_delta ≥ +1`, (3) `flipped_to_fail == 0` | `feat(bm25): opt-in identifier-aware tokenizer (measurement-driven)` PR. env var 유지, default 0. CLAUDE.md 에 env var 문서화. default flip 은 별도 follow-up. |
| **(b) 기각** | 파리티 15/15 유지 but (a)-(2) 혹은 (a)-(3) 미충족 | design doc + runner + measurement note 만 기록. `src/trawl/bm25.py` 와 `tests/test_bm25.py` 변경 revert. `RESEARCH.md` 의 C6 후속 포인터를 extraction (1순위) 로 갱신. |
| **(c) 파리티 회귀** | `hybrid_id_aware` 에서 `test_pipeline.py` < 15/15 | 즉시 revert. 회귀 case 를 측정 note 에 기록 — 추가 spike 의 input. |

### Threshold 선택 근거

- **+1 assertion**: RRF-k spike 와 동일. MDN flip 이 first candidate
  이지만 위 분석 상 MDN 자체는 우연적 이득 외에는 직접 영향 낮음.
  fastapi_dependency_injection / rust_std_hashmap 같은 identifier-
  heavy query 패턴이 flip 할 가능성이 더 높음. 어떤 패턴이든 +1 이면
  tokenizer 가 실질 기여한다는 증거.
- **flipped_to_fail == 0** (strict): tokenizer 는 retrieval graph
  전체에 영향 — dense_only 에서 pass 하던 패턴이 sparse 가 잘못된
  candidate 를 밀어올려서 fail 되는 scenario 가 risk. 0 regression
  요구.
- **dense_only 와 비교하지 않음**: tokenizer 변경은 sparse 경로에만
  영향. dense_only 와의 delta 는 "hybrid 자체 효과 + tokenizer 효과"
  의 합성이므로 혼동. `hybrid_legacy` vs `hybrid_id_aware` 만이
  tokenizer 의 isolated signal.

## 파일 변경 (spike 전체)

기록용 — PR 머지 될 때 이 목록을 outcome note 에 재확인:

- `docs/superpowers/specs/2026-04-20-bm25-id-aware-tokenizer-design.md`
  — 본 문서 (신규).
- `src/trawl/bm25.py` — `tokenize()` 확장 (compound regex 2개 +
  env var 분기). gate (a) 통과 시에만 최종 유지.
- `tests/test_bm25.py` — identifier-aware 모드 케이스 + legacy 모드
  regression guard. gate (a) 통과 시에만 최종 유지.
- `benchmarks/bm25_id_aware_sweep.py` — 측정 러너 (신규). gate 결과
  와 무관하게 유지 (후속 spike 에서 baseline 참조용).
- `notes/bm25-id-aware-measurement.md` — 결과 + 결론 (gitignored).

## 측정 계획

### 실행 순서

1. `mamba activate trawl`; llama-server `:8081`, `:8083` healthcheck
   (이미 확인: 둘 다 200).
2. `python benchmarks/bm25_id_aware_sweep.py --dry-run` — 계획만.
3. `python benchmarks/bm25_id_aware_sweep.py` — 본 측정.
   - 3 modes × 16 patterns × 2 iter = 96 fetch_relevant. 첫 mode
     만 cold; 이후 fetch 캐시 hit. 약 15-25 분.
   - 결과: `benchmarks/results/bm25-id-aware-sweep/<ts>/`.
4. 파리티:
   - `TRAWL_HYBRID_RETRIEVAL=1 TRAWL_HYBRID_RRF_K=60 TRAWL_BM25_IDENTIFIER_AWARE=1 python tests/test_pipeline.py`
     → 15/15 요구.
   - `TRAWL_HYBRID_RETRIEVAL=1 TRAWL_HYBRID_RRF_K=60 python tests/test_pipeline.py`
     (id_aware=0) → regression 없음 재확인.
5. `notes/bm25-id-aware-measurement.md` 작성 — gate decision 적용.

### Summary.json 스키마 (핵심)

```json
{
  "generated_at": "...",
  "iterations": 2,
  "modes": ["dense_only", "hybrid_legacy", "hybrid_id_aware"],
  "baseline_mode": "hybrid_legacy",
  "parity": {
    "hybrid_legacy":   {"pass": 15, "total": 15, "ok": true},
    "hybrid_id_aware": {"pass": 15, "total": 15, "ok": true}
  },
  "per_mode": { ... },
  "diff_vs_baseline": {
    "hybrid_id_aware": {
      "flipped_to_pass": [...],
      "flipped_to_fail": [...],
      "top1_identity_changed": N,
      "net_assertion_delta": N
    }
  },
  "gate_decision": "(a) adopted" | "(b) rejected" | "(c) parity regression"
}
```

### Exit code

- 0 — 측정 성공 (gate decision 무관).
- 1 — 측정 실패 (error rate > 25% any mode, 혹은 파리티 subprocess
  실패).
- 2 — 인프라 실패 (embedding / rerank unreachable).

## 리스크

1. **MDN 에 큰 영향 없을 가능성** — 위 분석대로 쿼리에 identifier 가
   없으면 tokenizer 변경 효과 제한. gate (b) 결론 확률 중간. 그래도
   identifier-heavy 쿼리 패턴 (rust_std_hashmap, fastapi_dependency_
   injection, man_curl_options) 중 하나라도 flip 하면 +1 가능.
2. **doc length 증가로 BM25 normalization 이 불리하게 작용**.
   compound token emit 으로 doc length 가 평균 ~2-5% 증가. BM25Okapi
   의 `b=0.75` 기본값 에서 긴 doc 은 penalty. 잘 측정해야 함.
3. **Regex 의 예상 밖 매칭**. 예:
   - `localhost:8081` → hyphen 없고 dot 뒤 숫자뿐이라 identifier 아님
     ✓
   - `a.b.c.d.e` → dotted identifier. 길이 제한 없음. 이상한 토큰
     추가될 수 있음. 측정 후 spot check.
   - `e.g.` → `["e.g"]`? 현 regex 는 각 segment 가 letter-start 요구
     → `e.g` 는 매칭, 그런데 `e` 만으로 compound 가 돼버림. 실제론
     큰 해는 없음 (IDF 낮음).
4. **한글 + Latin 혼합**. 한글은 `[가-힣]+`, identifier regex 는
   `[A-Za-z]` 시작 요구 → 독립적이라 간섭 없음. unit test 로 확인.
5. **실행 타임박스**. 측정 15-25 분 + 파리티 2×90s = 20-30 분 + note
   작성 15 분 ⇒ 40-60 분. 1 세션 내 완료 가능.
6. **코드 변경 유지 여부**. 분석상 가장 유력한 gate 결론은 (b).
   설계상 revert 가 용이하도록 env var gating + 기존 경로 그대로.

## Follow-ups (본 spike 범위 밖)

1. **default flip**. gate (a) 채택 후 1-2 주 real usage + wider
   slice (news/finance/wiki 혼합) 로 default-on 평가.
2. **HyDE 출력에 identifier 가 포함되도록 프롬프트 튜닝**. HyDE 경로
   가 compound identifier 를 emit 하면 MDN 류 쿼리 에도 실질 효과.
   별도 spike.
3. **Query-time identifier extraction**. 쿼리 자체에 identifier 가
   없으면 검색 키워드를 쿼리 내용에서 추론 (예: "fetch" → 코드 예시
   주변의 `fetch(` 을 compound 로). 이는 heuristic 이 깊어서
   tokenizer 스코프 밖.
4. **Elasticsearch `word_delimiter_graph` 풀 호환**. 숫자 경계도
   identifier 로 취급 (`ipv4`, `oauth2`). 추가 regex 로 확장 가능
   하나 본 spike 에선 MDN 타겟만.

## 첫 행동 체크리스트

1. `git checkout develop && git checkout -b spike/bm25-id-aware-tokenizer`
   (실행됨, HEAD = 000e985).
2. 본 design doc commit.
3. `src/trawl/bm25.py` + `tests/test_bm25.py` 수정.
4. `benchmarks/bm25_id_aware_sweep.py` 작성.
5. 측정 + 파리티.
6. gate decision + outcome note.
