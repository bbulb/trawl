# C6 — Hybrid dense + BM25 retrieval (RRF fusion) — design (2026-04-19)

Branch: `feat/c6-hybrid-retrieval` (stacked on `feat/cache-hit-assertion-key`)

## 문제

현재 `src/trawl/retrieval.py` 는 bge-m3 의 dense 출력만 사용한다. ARCHITECTURE.md
"Known limitations" 가 이미 적어둔 그대로 **코드 페이지 (함수 시그니처, API
symbol) 에서 dense embedding rank noise** 가 나타난다 — 같은 토큰이지만 문맥이
조금 다른 청크들이 쿼리와 거의 같은 cosine 점수를 받아 top-k 내부 순서가 불안정.
`tests/agent_patterns/coding.yaml` 의 `code_heavy_query` 카테고리 21 패턴이
정확히 이 실패 모드를 측정한다.

ARCHITECTURE.md "Future work" 항목 3 (`BM25 hybrid retrieval`) + RESEARCH.md
C6 에 정리된 후속. 본 PR 은 MVP 수준의 BM25 층 + RRF fusion 도입.

## 비-목표 (scope 제한)

- **bge-m3 sparse 활용은 이번 PR에서 하지 않는다.** RESEARCH.md C6 원문은 bge-m3
  의 sparse 출력도 같이 쓰는 안을 포함했으나, 로컬 llama-server
  (`/v1/embeddings`, `/embedding`) 는 dense 벡터만 리턴하며 sparse weight 을
  외부로 노출하지 않는다. 이 기능을 받으려면 FlagEmbedding 의 `BGEM3FlagModel`
  을 띄우는 별도 HTTP 서비스 (python+transformers) 가 필요 — infra / scope
  변경이 커서 후속 C6.5 로 분리.
- **Reranker 는 건드리지 않는다.** 현재 `src/trawl/reranking.py` 의 bge-reranker-v2-m3
  pipeline 은 hybrid 이후에도 그대로 2x candidate 에 대해 동작. 본 PR은
  "candidate selection 이전 단계" 만 교체.
- **tokenizer fine-tune / 사전 학습 없음.** 규칙 기반 multilingual tokenizer 로
  한정. 결과가 dense 대비 회귀하면 그대로 defer.

## 접근법

```
query ──┬→ dense embedding  ─→ cosine ranking  ─┐
        │                                         ├→ RRF fusion ─→ top-(2k)
        └→ BM25 over chunks  ─→ lexical ranking ─┘                   │
                                                                      ▼
                                                        reranker (unchanged)
                                                                      │
                                                                      ▼
                                                              top-k result
```

### BM25 층

라이브러리: `rank_bm25 >= 0.2.2` — pure-python BM25Okapi, MIT, ~200 lines.
`environment.yml` / `pyproject.toml` 에 추가. 0.2.2 기준 zero-dep (numpy 만).

Tokenizer: 규칙 기반 multilingual splitter. `src/trawl/bm25.py` 에 자체 구현.

```python
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_HANGUL_RUN = re.compile(r"[가-힣]+")
_CJK_CHAR   = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")  # kana + CJK unified

def tokenize(text: str) -> list[str]:
    text = text.lower()
    tokens: list[str] = []
    tokens.extend(_LATIN_WORD.findall(text))
    for run in _HANGUL_RUN.findall(text):
        # Hangul: char bigrams (한글 ~1 syllable per token for bge-m3)
        tokens.extend(run[i:i+2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.append(run)
    tokens.extend(_CJK_CHAR.findall(text))  # ja/zh: char-level
    return tokens
```

Tokenizer 설계 근거:

- **Latin word-level** — snake_case / camelCase / dotted name 을 살려서
  분해. `asyncio.gather` → `asyncio`, `gather`. 코드 쿼리가 정확히 이 symbol 을
  lookup 하므로 word boundary 가 딱 맞음.
- **Hangul bigram** — "명량 해전" 같은 2 음절 단어가 `명량`, `량 `, ` 해`, `해전`
  처럼 나뉘어 부분 매칭을 허용. 1-syllable-single-token 인 한국어 특성상
  unigram BM25 는 stop-word-level 노이즈가 큼.
- **CJK char-level** — 일본어·중국어는 word boundary 가 없으므로 char 단위.
  bge-m3 의 내부 tokenizer 도 유사하게 분해.

이 tokenizer 는 `tests/test_bm25.py` 에서 per-언어 sanity unit test 로 커버.

### BM25Okapi 파라미터

`rank_bm25` 기본값 (`k1=1.5`, `b=0.75`) 유지. 튜닝은 후속 — 이번 PR 의 scope 는
"BM25 층이 붙었는가 + 회귀 없는가" 로 한정.

### RRF fusion

```python
def rrf_fuse(
    dense_ranked: list[int],   # chunk index in rank order (best first)
    sparse_ranked: list[int],
    *,
    k: int = 60,               # RRF smoothing constant (paper default 60)
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for rank, idx in enumerate(dense_ranked):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    for rank, idx in enumerate(sparse_ranked):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank)
    ordered = sorted(scores.items(), key=lambda x: -x[1])
    return ordered
```

RRF 선택 근거:

- **weighted score fusion (α·dense + (1-α)·sparse)** 보다 **RRF 가 안정적**.
  dense (cosine [-1, 1]) vs BM25 (unbounded, depends on corpus length) 스케일이
  크게 달라 가중치 튜닝이 까다롭다. RRF 는 rank 만 보기 때문에 스케일 불일치
  영향 없음.
- **k=60** 은 BEIR 의 Contextual Retrieval, Anthropic blog 의 RRF reference,
  `rank_bm25` 저자 reference 전부에서 사용되는 default.

### 통합 지점

`src/trawl/retrieval.py::retrieve()` 시그니처 추가:

```python
def retrieve(
    query: str,
    chunks: list[Chunk],
    *,
    k: int = 5,
    base_url: str = DEFAULT_EMBEDDING_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    extra_query_texts: list[str] | None = None,
    hybrid: bool = False,       # NEW
) -> RetrievalResult: ...
```

`hybrid=True` 시 flow:

1. 기존 dense pass 를 그대로 돌려 `dense_ranked` (전체 청크 dense rank 순) 를 얻음.
2. `src/trawl/bm25.py::score()` 가 query + 전체 청크에 대해 `sparse_ranked` 계산.
3. `rrf_fuse(dense_ranked, sparse_ranked)` 로 최종 순서 계산.
4. 상위 k 만 `ScoredChunk` 로 wrap. `score` 필드는 **dense cosine** 값을 유지
   (후속 reranker 및 telemetry 가 읽는 기존 숫자 불변). RRF 점수는 diagnostics.

`src/trawl/pipeline.py::fetch_relevant()` 에서:

```python
hybrid_flag = os.environ.get("TRAWL_HYBRID_RETRIEVAL", "0") == "1"
retrieval_result = retrieve(
    query, chunks, k=k*2, ..., hybrid=hybrid_flag,
)
```

Reranker 는 계속 2x candidate 위에서 돌고, `hybrid` 는 candidate 선정 단계만
교체.

### 환경 변수

- `TRAWL_HYBRID_RETRIEVAL` — default `0` (off). `1` 이면 pipeline 에서 BM25
  + RRF 사용. 처음 defaulting off 인 이유: coding.yaml live 측정에서 회귀 없는
  것을 확인한 뒤 별도 PR 로 default on.
- `TRAWL_HYBRID_RRF_K` — default `60`. 디버깅용, 공개하지 않는 tuning knob.

### Telemetry & PipelineResult

`PipelineResult` 에 필드 추가 **안 함**. `retrieval_elapsed_ms` 에 BM25 시간이
같이 묻힘. BM25 는 로컬 pure-python 이라 50 청크 기준 <20ms — telemetry 노이즈
수준. hybrid-on flag 는 이미 `TRAWL_HYBRID_RETRIEVAL` 이 externally observable.

## 리스크

1. **CJK tokenizer 선택이 dense 보다 나쁠 가능성.** → `coding.yaml` 21 패턴 +
   `tests/test_cases.yaml` 12 케이스 A/B 측정. 회귀가 있으면 default-off 유지
   + follow-up PR 에서 tokenizer 교체.
2. **RRF 가 reranker 앞단에서 candidate 를 바꿔 reranker 결과가 회귀.** →
   reranker 는 이미 2x candidates 를 받으므로 top-k * 2 slot 안에 기존
   winner 가 살아있을 가능성이 높음. 측정으로 확인.
3. **rank_bm25 의 공간복잡도.** → 청크당 2-5KB memory, 50 청크 기준 ~250KB
   per retrieval. 누적 누출 없음 (`BM25Okapi` 인스턴스는 호출 종료 시 gc).

## 측정 계획

A/B 매트릭스 (모든 측정은 live llama-server 필요):

| 테스트 | baseline (hybrid=0) | experiment (hybrid=1) | 목표 |
|---|---|---|---|
| `tests/test_pipeline.py` | 12/12 | 12/12 | 파리티 유지 |
| `tests/test_pipeline.py --only code_heavy_query 패턴` (per-pattern rerun) | rank@1 baseline | rank@1 experiment | ≥ baseline |
| `tests/test_agent_patterns.py --category code_heavy_query --shard coding` | baseline pass rate | experiment pass rate | ≥ baseline |
| 신규 `tests/test_bm25.py` (unit) | n/a | 모든 테스트 pass | tokenizer, RRF 수학 sanity |

예상 수치 (Contextual Retrieval 논문, Anthropic blog 2024 참고):

- dense-only: baseline
- dense + BM25 + RRF: recall@10 **+ 5-15%** on code/technical queries
- reranker 까지 거친 후: +2-5% 최종 recall 개선 (reranker 가 이미 noise 를
  걷어내므로 gain 축소)

측정 결과가 "회귀 없음 + 근소 개선" 이면 default-off 상태로 merge, 별도 PR 에서
default-on 전환. "회귀" 면 본 PR 자체를 hold 하고 tokenizer 재설계.

## 테스트 계획

- `tests/test_bm25.py` — tokenizer 언어별 bigram 생성, RRF 수학 (동일 rank
  시 점수 합산, 빈 ranking 처리), BM25Okapi wrapper 의 empty-corpus 방어.
- `tests/test_retrieval_hybrid.py` — `retrieve(hybrid=True)` 가 dense-only와
  동일 청크 수를 반환하고 rank order 만 다른지, `hybrid=False` fallback 시 기존
  결과와 완전 일치하는지.
- `tests/test_pipeline.py` 는 `TRAWL_HYBRID_RETRIEVAL` env 를 건드리지 않으므로
  자동 pass (default off).

## Follow-ups (이 PR scope 밖)

1. **C6.5 — bge-m3 sparse output.** FlagEmbedding `BGEM3FlagModel` 을 별도
   서비스로 띄우고 sparse weight 을 가져오는 방식. infra 변경 필요.
2. **BM25 파라미터 튜닝 (`k1`, `b`).** code-query 카테고리에 맞춰 measurement
   주도로.
3. **Hybrid default-on 전환.** 본 PR merge 후 1-2 주 real usage 관측 뒤.
