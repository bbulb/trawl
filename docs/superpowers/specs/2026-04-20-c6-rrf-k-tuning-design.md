# C6 follow-up — RRF k tuning spike — design (2026-04-20)

Branch: `spike/c6-rrf-k-tuning` (off `develop` at `4a1dad1`, v0.3.0).

Parent context:
[2026-04-19-c6-hybrid-retrieval-design.md](2026-04-19-c6-hybrid-retrieval-design.md)
— C6 merged default-off; `notes/c6-hybrid-measurement.md` concluded
`RRF k=60 is too smooth for a 16-pattern slice` and filed this spike
as follow-up.

## 문제

C6 의 `TRAWL_HYBRID_RETRIEVAL=1` A/B (16 `code_heavy_query` patterns)
에서 **assertion pass rate 는 하락 1 (12→11) 후행 노이즈**였고 **새로
통과한 pattern 은 0**. 결론:

- 파리티 15/15 는 k=60 에서 깨지지 않았다 → BM25 signal 이 dense
  winner 를 전복하지 못함.
- 동시에, BM25 토크나이저가 만들어내는 sparse rank 는 **정말로 쓸모가
  없었는지**, 아니면 **RRF k=60 이 너무 완만해서 signal 이 희석됐는지**
  구별 불가.

이 spike 는 둘 중 어느 쪽인지를 판정한다. 답이 두 번째면 default k
조정 + 별도 PR 로 문서화. 첫 번째면 k 튜닝으로는 해결 불가 — C6.5
sparse (bge-m3 native) 또는 weighted fusion 같은 별도 경로를 타야 함.

## 비-목표

- **TRAWL_HYBRID_RETRIEVAL default-on 전환은 하지 않음.** 본 spike 는
  k 튜닝으로 hybrid 의 signal 강도가 달라지는지만 측정. default-on
  여부는 여전히 별도 판단 (1-2 주 real usage + wider slice 필요).
- **weighted fusion 은 도입하지 않음.** C6 conclusion 에서 제안된
  옵션 2 는 별도 spike. RRF 자체의 parameterisation 을 먼저 털고
  그래도 막히면 weighted 로 이동.
- **BM25 토크나이저 수정하지 않음.** 현재 tokenizer 의 Latin word /
  Hangul bigram / CJK char 규칙은 C6 PR 에서 unit + parity 로 고정됨.
  본 spike 가 k 변화만으로 assertion 을 들어올리지 못하면 tokenizer
  의문은 살아있지만 *이 spike 의* scope 는 아님.
- **C6.5 bge-m3 sparse output 도입 안 함.** 완전 신규 코드 경로라
  spike 1 세션으로 끝나지 않음. `RESEARCH.md` 상에선 still filed.

## 접근법

C6 의 hybrid 경로 (`retrieval.py::retrieve(hybrid=True)`)를 그대로
사용하되 `TRAWL_HYBRID_RRF_K` 환경변수만 sweep. 코드 변경 없음, 측정
러너만 추가.

```
retrieve(hybrid=True, TRAWL_HYBRID_RRF_K=k):
    dense_ranked = sorted by cosine
    sparse_ranked = bm25_rank(query, chunk_texts)
    fused = rrf_fuse([dense_ranked, sparse_ranked], k=k)
    top-k = fused[:k]
```

RRF score contribution = `1/(k + rank)`.
- k=10: rank 0 기여 = 1/10 = 0.10, rank 5 기여 = 0.067. **sparse rank
  5 안팎이 dense top-1 을 뒤집을 수 있는 범위**.
- k=60 (default): rank 0 기여 = 0.016, rank 5 기여 = 0.015. 거의 평탄,
  C6 결과와 일관.
- k=100: rank 0 기여 = 0.010, rank 5 기여 = 0.0095. 더 평탄.

Hypothesis: assertion 통과율이 flatten 정도와 **역단조**. k↓ ⇒ sparse
의 lexical hit 가 dense 의 semantic 위로 올라가 `chunks_contain_any`
같은 키워드 assertion 이 통과할 확률 증가.

## 측정 스위트

1. **`code_heavy_query` 16 patterns (primary signal)**
   - `tests/agent_patterns/coding.yaml` 중 `category: code_heavy_query`
     필터. C6 measurement 와 동일 slice 로 직접 비교 가능.
   - 각 (k, pattern) 조합을 `fetch_relevant` 로 직접 호출. 기록:
     - `top1_score` (rank-1 dense cosine — RRF 는 ordering 만 바꾸므로
       score 는 여전히 dense cosine, 단 어느 chunk 가 top-1 인지가
       바뀜).
     - `top1_sig` (rank-1 chunk signature; 동일 chunk 가 선택됐는지
       identity 비교).
     - `assertion_pass` (pattern 의 `chunks_contain_any` / `contain_all`
       / `n_chunks_returned` / `error_is_none` 평가).
     - `retrieval_ms`, `n_chunks_total`, `n_chunks_embedded`.
   - `TRAWL_FETCH_CACHE_TTL=300` 기본값 유지 — iteration 2+ 에서
     fetch 노이즈 제거.

2. **파리티 15/15 (guard)**
   - `tests/test_pipeline.py` 를 `TRAWL_HYBRID_RETRIEVAL=1
     TRAWL_HYBRID_RRF_K=k` 로 각 k 에 대해 1회. exit code 0 = 통과.
   - **예외**: `kbo_schedule` 은 develop HEAD 에서 이미 PASS (URL 이
     2026-04-15 PR #25 로 핀 됐음). 본 spike 는 15/15 를 요구.

3. **Modes**
   - `dense-only` (baseline, `TRAWL_HYBRID_RETRIEVAL=0`) — 16 pattern
     × 2 iter.
   - `hybrid_k={10, 30, 60, 100}` (`TRAWL_HYBRID_RETRIEVAL=1
     TRAWL_HYBRID_RRF_K=k`) — 16 pattern × 2 iter × 4 k = 128 runs.
   - 합계 ~160 fetch_relevant call. 반복은 p95 측정용.

4. **평가**
   - Per-k aggregate:
     - `assertion_pass / 16` (단조 목표: baseline 대비 ≥ 0, 이상적
       +1 이상).
     - `top1_identity_change_rate`: dense-only 대비 rank-1 chunk 가
       바뀐 pattern 비율. 변한 case 에서 assertion 이 **improved**
       되는 건지 **regressed** 되는 건지 구별해서 기록.
     - `retrieval_ms.p95` (BM25 overhead 가 k 별로 동일해야 정상).
     - Parity `pass/fail`.

## Pre-registered decision gates

본 spike 는 **데이터가 말할 때만 defaults 를 바꾼다**. 세 결과 중
하나로 떨어진다:

| 결과 | 조건 | 액션 |
|---|---|---|
| **(a) k 채택** | 어떤 k\* ∈ {10, 30, 100} 에서: (1) 파리티 15/15 유지, (2) `assertion_pass` ≥ baseline **+1**, (3) assertion regressed 된 pattern 수 ≤ 1 (net improvement) | `feat(retrieval): retune RRF k to {k\*} (measurement-driven)` PR. `bm25.py` `DEFAULT_RRF_K` 상수 + `CLAUDE.md` 값 변경. |
| **(b) k=60 유지** | 어떤 k 도 (a) 조건을 만족하지 못하지만 파리티는 전부 유지 | `notes/c6-rrf-k-measurement.md` 에 결론만 적고 마감. `RESEARCH.md` 의 후속 포인터를 weighted fusion 또는 C6.5 sparse 로 갱신. |
| **(c) 파리티 회귀** | 어떤 k 에서 파리티 < 15/15 | 그 k 는 버림. 나머지 k 들은 (a)/(b) 룰로 처리. 파리티 회귀는 단독 항목으로 conclusion doc 에 기록 — 추가 spike 의 재료. |

### Threshold 선택 근거

- **+1 assertion (6.25% absolute)**: C6 결과에서 `code_heavy_query`
  의 3 assertion fail (mdn/stackoverflow/serverfault) 이 모두 "keyword
  missing from top-k". 이 중 최소 1개 라도 들어와야 BM25 가 실질적으로
  기여했다고 볼 수 있음. 운 좋게 1 pattern 이 우연히 flip 할 확률을
  낮추기 위해 ≥ 1 이 아니라 =+1 이면서 regression ≤ 1 조건.
- **regressed ≤ 1**: net positive 를 요구. 2 fail 바뀌는데 2 pass 도
  바뀌면 noise 와 구별 불가.
- **Handoff 의 "rank-1 score +0.2"는 폐기.** 문서 초안의 숫자였고
  RRF 는 ordering 만 바꾸므로 dense cosine score 가 +0.2 뛰는 건
  드문 시나리오. rank-1 chunk **identity change** 가 더 정확한 signal.

## 파일 변경

본 spike 에서 이뤄지는 변경은 아래뿐:

- `docs/superpowers/specs/2026-04-20-c6-rrf-k-tuning-design.md` —
  본 문서 (신규).
- `benchmarks/c6_rrf_k_sweep.py` — 측정 러너 (신규).
- `notes/c6-rrf-k-measurement.md` — 측정 결과 + 결론 (gitignored,
  작성 후).

코드 변경 없음 (gate (a) 가 트리거되면 **별도 PR** 로 DEFAULT_RRF_K
만 바꾸는 작은 변경을 낸다).

## 측정 계획

### 실행 순서

1. `mamba activate trawl`, llama-server :8081 / :8083 확인.
2. `python benchmarks/c6_rrf_k_sweep.py --dry-run` — 패턴 로드 +
   k sweep 계획만 출력.
3. `python benchmarks/c6_rrf_k_sweep.py` — 본 측정.
   - 5 modes × 16 patterns × 2 iter ≈ 160 fetch_relevant. 케이스 당
     ~8–15s 면 총 20–40 분. 캐시 hit 의존.
   - 결과: `benchmarks/results/c6-rrf-k-sweep/<ts>/{summary.json,
     report.md}`.
4. 파리티 측정 (각 k 별 1 회):
   - `TRAWL_HYBRID_RETRIEVAL=1 TRAWL_HYBRID_RRF_K={k} python tests/
     test_pipeline.py` — exit code 기록.
   - 러너가 subprocess 로 순차 실행해 `parity.json` 에 기록.
5. `notes/c6-rrf-k-measurement.md` 작성 — gate decision 적용.

### Schema of `summary.json`

```json
{
  "generated_at": "...",
  "iterations": 2,
  "k_values": [10, 30, 60, 100],
  "baseline_mode": "dense_only",
  "parity": {
    "10": {"pass": 15, "total": 15, "ok": true},
    "30": {...}, "60": {...}, "100": {...}
  },
  "per_mode": {
    "dense_only": {
      "assertion_pass": 12,
      "assertion_total": 16,
      "retrieval_ms_p95": 950,
      "patterns": [
        {"id": "...", "assertion_pass": true, "top1_sig": "...",
         "top1_score": 0.73, "retrieval_ms": [880, 912]}
      ]
    },
    "hybrid_k10": { ... },
    ...
  },
  "diff_vs_baseline": {
    "hybrid_k10": {
      "flipped_to_pass": ["pattern_id1", ...],
      "flipped_to_fail": ["pattern_id2", ...],
      "top1_identity_changed": 6,
      "net_assertion_delta": 1
    },
    ...
  },
  "gate_decision": "(a) k=10 adopted" | "(b) k=60 retained" | ...
}
```

### Exit code

- 0 — 측정 성공 (gate decision 과 무관).
- 1 — 측정 자체가 실패 (예: 어떤 k 에서 >25% 패턴이 error).
- 2 — 인프라 실패 (embedding server unreachable).

## 리스크

1. **16-pattern 시 샘플 수가 얇다.** flip 1개가 통계적으로 의미 있는지
   확실치 않음. Mitigation: regression ≤ 1 조건으로 **net delta** 를
   요구. 결론 doc 에 "2 회 iter 만 돌림, 수 더 늘리면 표준편차 산출
   가능" 명시.
2. **fetch 캐시 편향.** 처음 mode 가 네트워크 비용을 전부 부담하고,
   이후 mode 는 캐시 hit. retrieval_ms 만 보면 되므로 fetch 캐시
   hit 은 오히려 signal 을 깨끗하게 한다 (fetch 노이즈 제거). 다만
   모든 mode 가 **동일 순서** 로 iterate 해야 공정 — 러너에서
   patterns loop 안에 modes loop 를 놓는 게 아니라 modes loop 안에
   patterns loop 를 놓고 각 mode 의 first iter 는 cold 일 수 있음을
   인정.
3. **k=10 이 지나치게 aggressive 해 top-1 이 완전히 noisy 해질 수
   있음.** 파리티 15/15 guard 가 이를 잡는다 (wiki, pdf, 구조화
   페이지에서 잘못된 chunk 가 rank-1 되면 `must_contain_all`
   assertion 이 깨짐).
4. **retrieval_ms 증가.** 이론상 BM25 overhead 는 k 와 무관 (k 는
   fusion step 뿐). 실제 측정에서 k 별 median 이 크게 다르면 버그
   signal.
5. **spike 1 세션 타임박스.** 측정 20–40 분 + 파리티 4 × 90s =
   5–10 분 + 결론 doc 15 분 ⇒ 40–65 분. 1 세션 내 완료 가능.

## Follow-ups (본 spike 범위 밖)

1. **default-on + wider slice.** gate (a) 가 트리거되더라도 default-on
   여부는 별도 — chunk budget default-on 평가와 동일 패턴.
2. **weighted fusion.** `score_dense * alpha + score_sparse * (1-alpha)`
   의 alpha sweep. RRF 대비 tunable 이 2 가 아닌 1 이라 해석 단순.
3. **tokenizer A/B.** Latin word split 을 identifier-aware 로 바꾸면
   (예: `asyncio.gather` 를 1 토큰으로 유지) sparse hit 률이 어떻게
   바뀌는지.
4. **C6.5 bge-m3 sparse.** BM25 대비 semantic gap 이 작음. 본 spike
   결과가 (b)이면 가장 유력한 다음 후보.
