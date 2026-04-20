# C6 follow-up — MDN reranker diagnostic — design (2026-04-20)

Branch: `spike/mdn-reranker-diagnostic` (off `develop` at `56dc21a`).

Parent context:
[2026-04-20-hyde-compound-identifier-design.md](2026-04-20-hyde-compound-identifier-design.md)
— gate (b) rejected; `notes/hyde-compound-id-measurement.md` identified
the **reranker** as the likely bottleneck for `claude_code_mdn_fetch_api`.
HyDE emits the right identifiers, BM25 extras route them to the sparse
query, code chunks reach the fused top-k, but the reranker demotes
them in favour of MDN intro prose.

This spike **measures first, designs fix after**. No code change in
this commit stream — only diagnostic output + an outcome note that
points to one of four pre-registered fix candidates.

## 문제 재확인

단일 실패 `claude_code_mdn_fetch_api`:
- URL: `developer.mozilla.org/.../Fetch_API/Using_Fetch`
- Query: `"send a POST request with a JSON body using fetch"`
- Assertion: `chunks_contain_any: ["JSON.stringify", "Content-Type", "method:"]`
- 현재 기본 pipeline (`hybrid on`, reranker on, HyDE off): **top-5 에 위
  키워드 포함 청크가 들어오지 않음.**

HyDE spike 의 관찰:
- HyDE 가 identifier 를 emit → dense 및 BM25 가 code chunk 의 score
  를 올림.
- `reranker 500 fallback` 상황에서는 MDN PASS (cosine 만으로 top-k).
- reranker 정상 동작 시 MDN FAIL (같은 fused top-20 이라도 reranker
  가 non-code 를 rank-1 으로 선택).

가설: **reranker 가 `JSON.stringify` / `Content-Type` 등 식별자
함유 코드 블록을 낮은 relevance 로 scoring** 한다. 이유 추정:
- `bge-reranker-v2-m3` cross-encoder 는 자연어 쿼리 ↔ 자연어 답변
  매칭에 최적화 — 코드 스니펫 단독으로는 "쿼리와의 의미 매칭" 이
  약해 보일 수 있음.
- title-injection 이 `Title: MDN - Using Fetch\nSection: Body\n\n
  <code>` 같은 형태 — 코드 블록 의 heading 이 generic 한 섹션 이름
  (예: "Uploading a file") 이면 relevance 가 희석.

## 비-목표

- **코드 변경 금지.** 본 spike 는 진단만. 고쳐야 할 방향이 결정되면
  별도 spike 로 구현.
- **reranker 모델 교체 검토 안 함.** `bge-reranker-v2-m3` 는 프로젝트
  default; 교체는 범위 밖.
- **MDN 이외 패턴 측정 최소화.** 본 spike 는 MDN 단일 패턴에 집중.
  다른 패턴 regression 관찰은 후속 spike 에서 fix 돌릴 때 수행.

## 접근법

### Diagnostic script

`benchmarks/mdn_reranker_diag.py` (one-shot) 는 MDN URL 에 대해 세
상태 를 수집:

1. **Mode raw (no rerank)** — `fetch_relevant(url, query, k=50,
   use_rerank=False)` → fused 상위 50 청크를 dense cosine 정렬 로
   반환 (hybrid=on 은 default, BM25 signal 포함).
2. **Mode reranked** — `fetch_relevant(url, query, k=50,
   use_rerank=True)` → 동일 청크 풀을 reranker 돌린 뒤 top-50
   (relevance 정렬).
3. **Mode with HyDE** — `fetch_relevant(url, query, k=50,
   use_hyde=True, use_rerank=True)` → HyDE 경로 + rerank 시 변화.

모든 mode 에서 chunk text 를 substring scan:
- `contains_stringify`: `JSON.stringify` 함유
- `contains_content_type`: `Content-Type` 함유
- `contains_method_colon`: `method:` 함유
- `any_assertion_keyword`: 위 3 중 하나 이상

출력:
- `benchmarks/results/mdn-reranker-diag/<ts>/diag.md` — 사람이 읽는
  Markdown 표 3 개. 각 mode 별로:
  - Rank | chunk heading (truncated) | body preview (first 60 chars) |
    score | k∈|k∉|chunk_sig | contains? | …
  - Top-5 hard-highlight + "all keyword-bearing chunks" 별도 섹션.
- `benchmarks/results/mdn-reranker-diag/<ts>/diag.json` —
  programmatic 접근용.

### 파리티 / 측정 범위 단순화

- 파리티 재측정 안 함 (코드 변경 없음).
- `agent_patterns` 재측정 안 함.
- 오직 MDN 단일 URL 의 rank 분포 관찰.

## Pre-registered 결정 분기

결과가 네 가지 중 하나 로 떨어진다. 각 케이스 당 다음 spike 후보
를 미리 고정 — 이 spike 는 코드 변경이 없으므로 "채택/기각" 대신
"which follow-up spike" 를 정한다.

| 진단 결과 | 해석 | 다음 spike (별도 design doc + PR) |
|---|---|---|
| **(D1) 키워드 청크가 raw top-5 밖** | retrieval 단계 에서 이미 탈락. reranker 는 무관. | **retrieval 재검토** — 이미 RRF-k, tokenizer, HyDE 에서 실패. assertion 자체 재평가 또는 MDN extraction 재점검 (code block heading / embed_text 구성). |
| **(D2) 키워드 청크가 raw top-5 안 but reranked top-5 밖** | reranker 가 code chunk 를 demote. 가장 유력. | **reranker bypass 실험** — category-conditional (`code_heavy_query` 에서 reranker off) 혹은 `TRAWL_RERANK_FOR_CODE=0` env gate. 측정: code_heavy_query 16 패턴 파리티. |
| **(D3) 키워드 청크가 raw top-5 & reranked top-5 안** | chunk 는 top-5 에 있는데 assertion substring scan 이 다른 chunk 만 체크? | **assertion / top-k join 확인** — `chunks_contain_any` 가 top-5 blob 에 scan 하는지, 반환된 k 가 5 가 아닌지 (default 는 `k=None` → 5). 만약 actually top-5 에 있으면 이 spike 의 가정 자체가 틀림. |
| **(D4) HyDE on 시에는 reranked top-5 안이지만 off 시에는 밖** | HyDE + rerank 조합 이 실제로 도움. HyDE 의 full sweep 결과 (15/16) 와 대조 필요 — per-iter 차이 noise 일 수 있음. | **HyDE + rerank window 확장** — default k=5 → k=10 로 assertion 관대화. 단, assertion 정의 바뀜 — 별도 논의. |

본 spike 에서는 **분기 선택만** — 실제 fix 는 별도 spike 에서.

## 파일 변경

- `docs/superpowers/specs/2026-04-20-mdn-reranker-diagnostic-design.md`
  — 본 문서 (신규, PR 에 포함).
- `benchmarks/mdn_reranker_diag.py` — diagnostic script (신규, PR 에
  포함).
- `notes/mdn-reranker-diag-outcome.md` — 결과 + 다음 분기 결정
  (gitignored).

`src/trawl/` 변경 **없음**.

## 측정 계획

### 실행 순서

1. `mamba activate trawl`; `:8081` / `:8083` / `:8082` healthcheck.
2. `python benchmarks/mdn_reranker_diag.py` — 본 측정.
   - MDN URL 1개 × 3 modes × 1 iter = 3 fetch_relevant 호출.
   - HyDE 모드는 추가 15s. 총 ~30-45s.
   - 결과: `benchmarks/results/mdn-reranker-diag/<ts>/diag.{md,json}`.
3. 결과 해석 → `notes/mdn-reranker-diag-outcome.md` 작성 + 분기 선택.

### Schema (diag.json)

```json
{
  "generated_at": "...",
  "url": "https://developer.mozilla.org/.../Fetch_API/Using_Fetch",
  "query": "send a POST request with a JSON body using fetch",
  "modes": {
    "raw":       { "chunks": [ { rank, heading, preview, score, sig, contains }, ... ], "keyword_chunks_in_top5": int, "keyword_chunks_in_top10": int, ... },
    "reranked":  { ... },
    "with_hyde": { ... }
  },
  "keyword_chunks_all": [ { sig, heading, preview, raw_rank, reranked_rank, hyde_rank } ],
  "decision_hint": "D1|D2|D3|D4"
}
```

### Exit code

- 0 — 측정 성공 (결과 유의미 여부 무관).
- 2 — 인프라 실패.

## 리스크

1. **MDN 페이지 내용 변경**. developer.mozilla.org 가 문서 갱신
   하면 청크 수 / 키워드 위치 달라짐. 본 spike 는 one-shot, 재현성
   위해 fetch cache 에 의존 (첫 호출 후 같은 세션 내 동일).
2. **reranker non-determinism**. `bge-reranker-v2-m3` 는 deterministic
   (softmax over logits, no sampling). 동일 인풋 동일 결과. 안전.
3. **HyDE non-determinism**. `:8082` Gemma 4 는 temperature=0.7 로
   sampling. HyDE mode 의 경우 출력이 매번 다름 → 본 spike 의 HyDE
   mode 는 **단일 iter** 로 본 결과가 대표적이지 않을 수 있음.
   완화: HyDE output 자체를 diag 에 기록해서 해석 시 맥락 제공.
4. **reranker 500 재현**. HyDE spike 에서 간헐적 500 관측. 만약 본
   측정 중 발생하면 진단 결과가 cosine fallback 으로 왜곡. 완화:
   reranker HTTP 응답 코드를 diag.json 에 기록, 500 이면 세션 재시작
   권고.

## Follow-ups (분기별)

위 표의 D1/D2/D3/D4 각각 이 별도 spike 의 design 입력. 본 spike
commit 은 outcome note 에서 "다음 spike 는 D? 방향" 만 결정.

## 첫 행동 체크리스트

1. `git checkout -b spike/mdn-reranker-diagnostic` (실행됨).
2. 본 design doc commit 포함 예정.
3. `benchmarks/mdn_reranker_diag.py` 작성.
4. 실행 + 결과 해석.
5. outcome note + 분기 결정.
