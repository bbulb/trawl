# C6 — Hybrid dense + BM25 retrieval — Implementation Plan

**Goal:** `src/trawl/retrieval.py::retrieve()` 에 BM25 lexical ranking 을
추가하고, RRF 로 dense rank 와 fusion 하는 opt-in 경로를 구현한다. Default off
(`TRAWL_HYBRID_RETRIEVAL=0`), 회귀 0 유지, coding.yaml 의 `code_heavy_query`
카테고리에서 실측으로 검증.

**Architecture:** 새 모듈 `src/trawl/bm25.py` 가 multilingual tokenizer + BM25
wrapper + RRF fusion 세 가지를 export. `retrieval.py::retrieve()` 는 `hybrid`
kwarg 를 받아 이 경로를 분기 호출. `pipeline.py::fetch_relevant()` 가 env 로
flag 전달.

**Tech Stack:** Python 3.10+, `rank_bm25>=0.2.2` (새 pip dep), stdlib `re`.
기존 httpx 경로 변경 없음.

**Spec:** [`docs/superpowers/specs/2026-04-19-c6-hybrid-retrieval-design.md`](../specs/2026-04-19-c6-hybrid-retrieval-design.md)

**Env rule:** 모든 python/pytest 명령은 `mamba activate trawl` 환경에서 실행.
플랜에서는 편의상 `mamba run -n trawl` 접두어를 생략하지만 실제 실행 시 붙인다.

---

## File structure

```
src/trawl/
  bm25.py                  NEW — tokenizer + BM25Okapi wrapper + RRF fusion
  retrieval.py             MODIFIED — hybrid kwarg 분기
  pipeline.py              MODIFIED — TRAWL_HYBRID_RETRIEVAL env wiring

tests/
  test_bm25.py             NEW — tokenizer / RRF / BM25 wrapper unit tests
  test_retrieval_hybrid.py NEW — retrieve(hybrid=True/False) A/B 동작 검증

pyproject.toml             MODIFIED — rank_bm25 dep 추가
environment.yml            MODIFIED — 동일
CHANGELOG.md               MODIFIED — Unreleased 항목 추가
CLAUDE.md                  MODIFIED — llama-server 맵 영역에 TRAWL_HYBRID_RETRIEVAL 추가
ARCHITECTURE.md            MODIFIED — Future work #3 (BM25 hybrid) "Done" 표시
```

---

## Task 0: 전제 확인

- [ ] `rank_bm25` 가 `mamba run -n trawl pip install rank_bm25==0.2.2` 로
      설치 가능한지 확인 (local wheel 있으면 offline 도 OK).
- [ ] `/v1/embeddings` endpoint 에 영향 없음 확인 (`curl -s http://localhost:8081/props | jq .model_alias`
      로 bge-m3 살아있는지만).
- [ ] 현재 branch tip 이 `feat/cache-hit-assertion-key` (#17) 인지 `git branch --show-current` 로 확인.
- [ ] 새 브랜치 `feat/c6-hybrid-retrieval` 를 #17 위에 stack:
      `git checkout -b feat/c6-hybrid-retrieval feat/cache-hit-assertion-key`.

**완료 기준:** 위 4가지 pass.

---

## Task 1: `src/trawl/bm25.py` 스켈레톤 + tokenizer

### Implementation

- [ ] `src/trawl/bm25.py` 신규 생성.
- [ ] Imports: `re`, `math`, `from rank_bm25 import BM25Okapi`.
- [ ] 정규식 상수 선언:
  - `_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")`
  - `_HANGUL_RUN = re.compile(r"[가-힣]+")`
  - `_CJK_CHAR   = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")`
- [ ] `def tokenize(text: str) -> list[str]` 구현. 순서:
  1. `text = text.lower()`
  2. Latin word 전부 추출
  3. Hangul run 별로: `len==1` → 그대로, `len>=2` → 2-gram list
  4. CJK char (kana / CJK unified 제외 Hangul) 전부 추출
- [ ] 모듈 docstring 및 public 함수 docstring 작성.

### Test — `tests/test_bm25.py::test_tokenize_*`

- [ ] `test_tokenize_latin` — `"asyncio.gather() lock"` → `["asyncio", "gather", "lock"]`.
- [ ] `test_tokenize_hangul_bigram` — `"명량 해전"` → `["명량", "해전"]` (1-syllable 각각 그대로? NO — "명량" 은 2-syllable 이므로 단일 bigram; "해전" 동일; 공백 무시).
  - 구체적으로 `"명량해전"` → `["명량", "량해", "해전"]` (연속 4 syllable 의 3개 bigram).
- [ ] `test_tokenize_mixed` — `"Python asyncio 사용법"` → `["python", "asyncio", "사용", "용법"]` (Latin lower + Hangul bigram).
- [ ] `test_tokenize_empty` — `""` → `[]`, `"   "` → `[]`.
- [ ] `test_tokenize_cjk_char` — `"日本語"` → `["日", "本", "語"]`.
- [ ] `test_tokenize_numeric_skipped` — `"abc 123 def"` → `["abc", "def"]` (숫자 단독 토큰은 BM25 가치 낮음, 명시적 skip 확인).
  - 주의: `_LATIN_WORD` 는 `[A-Za-z]` 로 시작하므로 `"def2"` 는 `"def2"` 하나로 추출됨. 이 케이스도 테스트.

**완료 기준:** `pytest tests/test_bm25.py::test_tokenize -v` 전부 pass.

---

## Task 2: BM25 scorer

### Implementation

- [ ] `src/trawl/bm25.py` 에 `def bm25_rank(query: str, documents: list[str]) -> list[int]` 추가.
- [ ] Body:
  1. `tokenized_corpus = [tokenize(d) for d in documents]`
  2. 빈 corpus 방어: `if not any(tokenized_corpus): return list(range(len(documents)))`
  3. `bm25 = BM25Okapi(tokenized_corpus)`
  4. `q_tokens = tokenize(query)`
  5. 빈 쿼리 방어: `if not q_tokens: return list(range(len(documents)))`
  6. `scores = bm25.get_scores(q_tokens)`
  7. `ranked = sorted(range(len(documents)), key=lambda i: -scores[i])`
  8. `return ranked`
- [ ] Return type 은 "document index in rank order (best first)" — len == len(documents).

### Test — `tests/test_bm25.py::test_bm25_*`

- [ ] `test_bm25_basic_term_match` — corpus `["hello world", "unrelated", "world hello again"]`, query `"hello"` → 1 순위는 doc 0 또는 doc 2 (둘 다 match), doc 1 은 꼴찌.
- [ ] `test_bm25_empty_corpus` — `bm25_rank("q", [])` → `[]`.
- [ ] `test_bm25_empty_query` — 빈 쿼리 → `list(range(N))` (원래 순서 유지).
- [ ] `test_bm25_all_empty_docs` — 모든 doc 이 tokenize → [] → `list(range(N))`.
- [ ] `test_bm25_korean_bigram_match` — corpus `["명량 해전 승리", "이순신 장군"]`, query `"해전"` → rank 0 은 doc 0. 입력이 1음절 "해" 로 들어와도 (`tokenize("해")` 가 `["해"]` 이므로 bigram 없음) BM25 가 document 의 `"해전"` bigram 과 매칭 안 됨을 확인 → 이 edge case 는 문서화만.

**완료 기준:** `pytest tests/test_bm25.py::test_bm25 -v` pass.

---

## Task 3: RRF fusion

### Implementation

- [ ] `src/trawl/bm25.py` 에 상수 + 함수 추가:
  - `DEFAULT_RRF_K = int(os.environ.get("TRAWL_HYBRID_RRF_K", "60"))`
  - `def rrf_fuse(rankings: list[list[int]], *, k: int = DEFAULT_RRF_K) -> list[int]:` —
    여러 ranking 을 받아 RRF 점수 합산 후 sorted descending. 단일 리스트도
    허용 (그대로 반환).
- [ ] Body:
  1. `scores: dict[int, float] = {}`
  2. 각 ranking 의 rank/index 순회하며 `scores[idx] += 1.0 / (k + rank)`.
  3. `sorted(scores, key=lambda i: -scores[i])`
  4. 빈 ranking 전부면 `[]`.
- [ ] `os` import 추가 (env var 읽기).

### Test — `tests/test_bm25.py::test_rrf_*`

- [ ] `test_rrf_identical_rankings` — `[[0,1,2], [0,1,2]]` → `[0,1,2]` (두 번 같은 점수 → 동률이지만 stable sort 로 0,1,2 유지).
- [ ] `test_rrf_disjoint_top` — `[[0,1,2], [2,1,0]]` → `[1, 0 또는 2, ...]` (1 은 두 ranking 모두 2등 → 가장 높은 합산 점수).
- [ ] `test_rrf_one_ranking_missing_index` — `[[0,1,2], [0,1]]` → len(output) == 3.
- [ ] `test_rrf_empty_rankings` — `rrf_fuse([[], []])` → `[]`.
- [ ] `test_rrf_custom_k` — `k=1` 이 `k=60` 대비 top rank 의 영향력이 더 커지는 sanity.

**완료 기준:** `pytest tests/test_bm25.py -v` 전체 pass.

---

## Task 4: `retrieval.py::retrieve()` 에 hybrid 경로 통합

### Implementation

- [ ] `src/trawl/retrieval.py` 상단 import: `from .bm25 import bm25_rank, rrf_fuse`.
- [ ] `retrieve()` 시그니처에 `hybrid: bool = False` 추가.
- [ ] 함수 body 에서 dense 계산 완료 직후 (`scored.sort(key=lambda s: -s.score)` 바로 전) 에 분기:
  ```python
  if hybrid:
      dense_ranked = sorted(
          range(len(chunks)),
          key=lambda i: -cosine(avg_q, chunk_embs[i]),
      )
      sparse_ranked = bm25_rank(query, chunk_texts)
      fused = rrf_fuse([dense_ranked, sparse_ranked])
      scored = [
          ScoredChunk(
              chunk=chunks[i],
              score=cosine(avg_q, chunk_embs[i]),
          )
          for i in fused
      ]
  else:
      scored = [
          ScoredChunk(chunk=c, score=cosine(avg_q, ce))
          for c, ce in zip(chunks, chunk_embs, strict=True)
      ]
      scored.sort(key=lambda s: -s.score)
  ```
  - 기존 non-hybrid 분기 동작 bit-for-bit 유지.
- [ ] `scored[:k]` return 은 공통 (분기 밖).

### Test — `tests/test_retrieval_hybrid.py`

- [ ] 작은 corpus fixture: 4-5 개 `Chunk` (text + heading 채움). 하나는 query 와 정확히 lexical 일치 ("FastAPI dependency injection"), 다른 하나는 semantic 만 일치 ("DI framework in Python").
- [ ] `test_hybrid_off_matches_baseline` — monkeypatch `_embed_batch` 로 고정 벡터 주입. `retrieve(hybrid=False)` 와 기본 호출 결과 완전 동일.
- [ ] `test_hybrid_on_shape` — `retrieve(hybrid=True)` 결과 `len(scored) == k`, 모두 unique chunk.
- [ ] `test_hybrid_on_lexical_win` — dense 에서 lexical exact match 가 2등인데 BM25 에서 1등인 fixture 설계 → hybrid 에서 top-1 이 lexical exact match.
- [ ] `test_hybrid_empty_chunks` — 빈 청크 리스트는 분기 상관없이 `RetrievalResult(scored=[])`.
- [ ] `test_hybrid_preserves_score_field` — `ScoredChunk.score` 는 hybrid 분기에서도 cosine 값 유지 (RRF 점수가 대입되지 않음).

**완료 기준:** `pytest tests/test_retrieval_hybrid.py -v` pass. Live infra 없이 monkeypatch 로 돌아감.

---

## Task 5: `pipeline.py::fetch_relevant()` env wiring

### Implementation

- [ ] `src/trawl/pipeline.py` 에서 `retrieve(...)` 호출 지점 찾기. grep `retrieve(` → `fetch_relevant` 내부.
- [ ] 호출 직전에 `hybrid_flag = os.environ.get("TRAWL_HYBRID_RETRIEVAL", "0") == "1"` 추가.
- [ ] `retrieve(...)` 호출에 `hybrid=hybrid_flag` 전달.
- [ ] `import os` 가 이미 존재하는지 확인 (대부분 있음).

### Test — `tests/test_pipeline.py`

- [ ] 수정 **없이** 전체 돌려서 12/12 유지 확인.
- [ ] `TRAWL_HYBRID_RETRIEVAL=1 python tests/test_pipeline.py` 도 12/12 통과해야 함 (회귀 없음 확인). 실패 시 spec 의 "리스크 #1" 경로로 분기: tokenizer 재검토.

**완료 기준:** `python tests/test_pipeline.py` 12/12 (default off) + `TRAWL_HYBRID_RETRIEVAL=1 python tests/test_pipeline.py` 12/12.

---

## Task 6: `code_heavy_query` A/B 실측 (live infra)

### Implementation

- [ ] `tests/test_agent_patterns.py --category code_heavy_query --shard coding --verbose > /tmp/c6_baseline.txt 2>&1`
      로 baseline 저장 (default off).
- [ ] `TRAWL_HYBRID_RETRIEVAL=1 tests/test_agent_patterns.py --category code_heavy_query --shard coding --verbose > /tmp/c6_hybrid.txt 2>&1`
      로 experiment 저장.
- [ ] 두 파일 `diff` + `grep -c "PASS\|FAIL"` 로 pass rate 비교.

### 측정 결과 기록

- [ ] `notes/c6-hybrid-measurement.md` (gitignored) 에:
  - 측정 시각 + 환경 (`uname -a`, llama-server commit hash 등)
  - per-pattern pass/fail table
  - rank-1 diff 있는 패턴의 before/after chunk 내용 비교 (최대 3개)
  - 결론: accept default-on / keep default-off / defer

### Decision gate

- hybrid pass rate >= baseline + 회귀 패턴 0 → Task 7 (default-off 유지하며 merge, follow-up PR 에서 default-on).
- hybrid pass rate < baseline 또는 회귀 발생 → 플랜 중단, spec "리스크 #1" 경로로 complete-rewrite 검토.

**완료 기준:** 측정 완료 + `notes/c6-hybrid-measurement.md` 작성 + 의사결정 문서화.

---

## Task 7: 문서 & changelog

### Implementation

- [ ] `CHANGELOG.md` `Unreleased` 섹션 상단에 spec 의 CHANGELOG entry 추가.
- [ ] `CLAUDE.md` 의 "llama-server endpoint map" 섹션 근처에 `TRAWL_HYBRID_RETRIEVAL` 항목 추가 (default 0, 활성 조건, measurement 요약).
- [ ] `ARCHITECTURE.md` "Future work" 항목 3 (`BM25 hybrid retrieval`) 을 strikethrough + "Done (C6, 2026-04-20)" 추가 — C7/C8/C9 포맷과 동일.
- [ ] spec 의 마지막 `## CHANGELOG entry` 블록 삭제 (중복 방지).

### Test

- [ ] `grep -c "TRAWL_HYBRID_RETRIEVAL" CLAUDE.md` >= 1.
- [ ] `grep "C6" CHANGELOG.md | head` 에서 신규 엔트리가 나옴.
- [ ] `grep "Done (C6" ARCHITECTURE.md` >= 1.

**완료 기준:** 위 grep 세 개 모두 hit.

---

## Task 8: Commit & PR

### Implementation

- [ ] `git add src/trawl/bm25.py tests/test_bm25.py tests/test_retrieval_hybrid.py src/trawl/retrieval.py src/trawl/pipeline.py pyproject.toml environment.yml CHANGELOG.md CLAUDE.md ARCHITECTURE.md docs/superpowers/`
- [ ] Commit message (HEREDOC, 본 repo 는 co-author trailer 안 붙임 — memory `feedback_commit_coauthor.md`):
      ```
      feat(retrieval): C6 — BM25 hybrid retrieval behind TRAWL_HYBRID_RETRIEVAL

      <body — 스펙 요지 + 측정 요약>
      ```
- [ ] `git push -u origin feat/c6-hybrid-retrieval`
- [ ] `gh pr create --base feat/cache-hit-assertion-key --title "feat(retrieval): C6 — BM25 hybrid retrieval (opt-in)" --body "$(cat <<'EOF' ... EOF)"`
- [ ] PR body 에 spec / plan 경로, 측정 결과 요약, default-off 근거, follow-up 목록 포함.

### Test

- [ ] `gh pr checks` 로 CI 상태 확인. 실패 시 수정 후 재푸시.

**완료 기준:** PR URL 반환 + CI passing (또는 분석 후 pass 유도).

---

## Rollback plan

Hybrid default-off 이므로 PR merge 이후에도 회귀 발생 시:

1. `TRAWL_HYBRID_RETRIEVAL` env 제거로 즉시 baseline 복귀.
2. `revert` PR 으로 코드 자체 제거 (한 줄 import + 한 줄 분기 + 신규 모듈 2개).

Risk 가 격리되어 있어 full revert 도 5분 내.
