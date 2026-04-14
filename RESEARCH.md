# Research notes — 2026-04-14

첫 공개 이후 trawl 개선 방향을 잡기 위해 수집한 관련 프로젝트·논문·벤치마크
정리. 초점은 **URL + 자연어 쿼리 → 관련 청크만 반환**이라는 trawl의 포지션에
직접 맞닿은 작업들이다. 각 개선 후보는 본 문서 하단의 체크리스트에서 하나씩
검토·결정한다.

## 1. 경쟁·비교 대상 프로젝트

"URL → LLM-친화 출력" 카테고리에서 현재 주도하는 것들.

| 프로젝트 | 출력 | 포지셔닝 |
|---|---|---|
| Jina Reader | 전체 페이지 마크다운 | 가장 광범위한 사용, `r.jina.ai/{url}` |
| Firecrawl | 마크다운 / 구조화 / 크롤 | MCP 벤치마크 상위권, 대규모 크롤 지원 |
| Crawl4AI | 마크다운 + chunking | 오픈소스 대안, LLM-aware chunking 내장 |
| ScrapeGraphAI | 구조화 | LLM+graph 파이프라인 자동생성 |
| Web2MD | 마크다운 (브라우저 로컬) | Chrome 확장, 개인정보 민감 유즈케이스 |
| Markdowner | 마크다운 | LLM 필터/크롤 옵션 |
| webmcp (AuthBits) | MCP stdio | search-first + 다중 URL 정제 |
| Bright Data / Exa / Parallel / Tavily | MCP / API | 상용, 대규모 agent workload |

**차별 포인트.** 위 도구 대부분이 "전체 페이지 정제 후 반환"이다.
trawl처럼 **쿼리 조건부 top-k 청크**를 기본값으로 하는 곳은 거의 없다.
Crawl4AI가 가장 가깝지만 쿼리-aware 랭킹·리랭킹까지는 아님.
profile_page (VLM으로 selector 학습, 캐시) 역시 기존 카테고리에 없는 축이다.

## 2. 핵심 참고 논문

trawl 파이프라인에 직접 연결되는 것만 골랐다.

### 2.1 청킹·임베딩
- **Late Chunking** (arXiv:2409.04701, Günther et al., Jina) — 토큰 전체를
  long-context 임베딩 모델로 먼저 인코딩한 뒤 마지막에 chunk-pooling.
  청크가 주변 문맥을 잃는 문제를 직접 겨냥. bge-m3가 8K context를 지원하니
  trawl에 바로 실험 가능.
- **Late chunking vs Contextual Retrieval 비교 연구** (arXiv:2504.19754,
  2025) — 두 기법 정량 비교. Contextual Retrieval(Anthropic) 쪽이 더 가벼운
  개선일 수 있음. HyDE 대안으로 검토 가치.
- **Chunk Twice, Embed Once** (arXiv:2506.17277) — chunking ×
  representation trade-off 체계적 연구. `max_chars=450` 결정을 외부 벤치와
  교차검증하는 근거로 사용 가능.

### 2.2 쿼리-aware 추출
- **DeepQSE / Efficient-DeepQSE** (arXiv:2210.08809, EMNLP'22) — 쿼리-aware
  스니펫 추출의 표준 2-stage 구조(후보 선별 → 정밀 관련도). trawl의
  retrieval → cross-encoder rerank와 구조적으로 동형. 인코더 캐싱 아이디어는
  프로필 캐시와 합쳐볼 여지.
- **An Index-based Approach for Efficient and Effective Web Content
  Extraction** (arXiv:2512.06641, 2024) — 생성형 추출(ReaderLM 류) 대비
  인덱스 기반이 빠르고 정확. "LLM으로 HTML 다시 쓰지 말고 규칙·랭킹으로"
  라는 trawl 설계 철학과 정확히 일치. 재방문 시 selector 캐시를 인덱스로
  재활용하는 방향의 근거.
- **Beyond Pixels: DOM Downsampling for LLM-Based Web Agents**
  (arXiv:2508.04412) — D2Snap / AdaptiveD2Snap. 토큰 예산에 맞춰 DOM을
  다운샘플링. trawl의 extraction 단계(현재 마크다운 기반)에 DOM-level
  단계를 추가하는 아이디어.

### 2.3 Information-Seeking Agent
- **NestBrowse** (arXiv:2512.23647, 2025) — "페이지가 1M 토큰을 넘어갈 수
  있으니 IS agent는 목표-관련 서브셋만 얻어야 한다"는 문제 정의가 trawl의
  존재 이유 그 자체. IS 전용 모델(NestBrowse-4B/30B-A3B) 학습 접근과의
  비교·포지셔닝 필요.
- **MM-BrowseComp** (arXiv:2508.13186) — 멀티모달 브라우징 에이전트 벤치.
  외부 벤치 참고.
- **WAREX / WABER** (arXiv:2510.03285, MS) — 웹 agent 신뢰성·효율성 평가
  프레임워크. latency / API 호출 수 / 토큰 수 측정 표준.

### 2.4 레이아웃·시각 이해
- **SCAN** (arXiv:2505.14381) — VLM용 semantic document layout
  segmentation. profile_page의 "anchor → DOM → LCA → selector"를
  레이아웃-aware하게 개선할 여지.
- **Visual Grounding for UI** (NAACL 2024 Industry) — DOM 메타데이터 없이
  스크린샷+자연어로 UI 요소 지시. profile_page VLM 프롬프트 설계의 참고.

### 2.5 추출 품질 벤치마크
- **WCXB — Web Content Extraction Benchmark** (Murrough Foley, 2025) —
  2,008 페이지 × 7 페이지 타입 × 1,613 도메인. 뉴스 편중 없는 최신 벤치.
  공식 결과에서 Trafilatura F1 0.958로 오픈소스 최상위. trawl extraction
  단계의 외부 교차검증에 사용 가능.
- **Trafilatura 공식 평가** — "heuristic이 복잡 페이지에서 neural
  extractor보다 잘한다"는 결론. trawl의 현재 설계를 지지.

## 3. 인접 연구 — 포지셔닝 참고

- **Skyvern (WebVoyager 85.85%)** — DOM·selector 대신 순수 비전+LLM.
  모든 액션에 VLM 호출 → 비용 큼. trawl이 "profile_page에서만 VLM"을 쓰는
  전략은 비용 최적해로 방어 가능.
- **Browser-Use** — 비전+DOM 혼합. 오픈소스 agent browsing 대표.
- **An Illusion of Progress** (arXiv:2504.01382) — 웹 에이전트 현황 비판,
  벤치 성능과 실제 신뢰성 괴리.

## 4. 출처

- [webmcp (AuthBits)](https://github.com/AuthBits/webmcp)
- [MCP Benchmark 2026 (aimultiple)](https://aimultiple.com/browser-mcp)
- [Jina Reader alternatives 2026](https://scrapegraphai.com/blog/jina-alternatives)
- [Firecrawl alternatives](https://www.eesel.ai/blog/firecrawl-alternatives)
- [Late Chunking (arXiv:2409.04701)](https://arxiv.org/abs/2409.04701)
- [Late chunking vs contextual retrieval (arXiv:2504.19754)](https://arxiv.org/pdf/2504.19754)
- [Chunk Twice, Embed Once (arXiv:2506.17277)](https://arxiv.org/html/2506.17277v1)
- [DeepQSE (arXiv:2210.08809)](https://arxiv.org/abs/2210.08809)
- [Index-based Web Content Extraction (arXiv:2512.06641)](https://arxiv.org/html/2512.06641)
- [NestBrowse (arXiv:2512.23647)](https://www.arxiv.org/pdf/2512.23647)
- [D2Snap: DOM Downsampling (arXiv:2508.04412)](https://arxiv.org/html/2508.04412v1)
- [SCAN (arXiv:2505.14381)](https://arxiv.org/html/2505.14381)
- [Visual Grounding for UI (NAACL 2024)](https://aclanthology.org/2024.naacl-industry.9.pdf)
- [MM-BrowseComp (arXiv:2508.13186)](https://arxiv.org/html/2508.13186v1)
- [WAREX (arXiv:2510.03285)](https://arxiv.org/html/2510.03285v1)
- [Illusion of Progress (arXiv:2504.01382)](https://arxiv.org/html/2504.01382v4)
- [WCXB benchmark](https://github.com/Murrough-Foley/web-content-extraction-benchmark)
- [Trafilatura evaluation](https://trafilatura.readthedocs.io/en/latest/evaluation.html)
- [Skyvern](https://github.com/Skyvern-AI/skyvern)

---

# 개선 후보 검토 큐

후보별로 `status`를 하나씩 옮겨가며 리뷰한다. 각 항목 순서는 **리스크 대비
기대효과·실험 용이성**을 눈대중으로 정렬한 것일 뿐, 의사결정은 각 후보의
"검토 포인트"를 같이 보면서 내린다.

> **워크플로.** 한 번에 하나씩 `status: pending` → `in_review` (함께
> 상세 검토/브레인스토밍) → `decided` (accept/defer/reject + 근거). 채택된
> 것은 별도 implementation plan으로 분리한다.

---

## C1. Late chunking 도입 실험 &nbsp; — `status: pending`

**요지.** `chunking.chunk_markdown`이 텍스트를 먼저 자르고 각각 임베딩하는
대신, bge-m3로 전체 본문을 먼저 인코딩하고 토큰 시퀀스에서 chunk pooling.
청크가 주변 문맥을 잃는 문제(특히 표·목록·연속 문단)를 줄임.

**기대 효과.** recall 증가, 특히 "fact가 문단 경계에 걸친" 케이스.

**검토 포인트.**
- bge-m3의 현재 컨텍스트 한도와 `MAX_EMBED_INPUT_CHARS=1800` 제약의 충돌.
- 450자 hard-cap을 유지할 것인가, chunk 경계 정의 자체를 바꿀 것인가.
- parity matrix 12/12를 유지할 수 있는 수준의 변화인가, 아니면 ground
  truth 재정의가 필요한가.
- HyDE·reranker와의 상호작용 (late chunking 후 reranker 입력 포맷).

**결정 후 다음 단계.** `src/trawl/chunking.py` + `retrieval.py` 실험 브랜치.
12-case + WCXB 부분집합 비교.

**근거 논문.** arXiv:2409.04701, arXiv:2504.19754.

---

## C2. WCXB로 extraction 단계 외부 벤치 추가 &nbsp; — `status: pending`

**요지.** 현재 12 cases + 36 profile cases만으로는 extraction의 edge case
커버리지 주장이 약함. WCXB(2,008 페이지 / 7 타입 / 1,613 도메인)를
`benchmarks/`에 추가해 Trafilatura precise/recall/BS 3-way 선택을 외부
기준으로 검증.

**기대 효과.** "우리 extraction은 제네릭하게 잘한다"는 주장을 외부 데이터로
뒷받침. 회귀 감지도 강화.

**검토 포인트.**
- WCXB 라이선스·재배포 조건.
- 실행 시간 — 2K 페이지 풀 벤치는 네트워크 없이 고정 스냅샷으로 돌려야 함.
  저장 용량·gitignore 정책.
- 목표 지표: F1 0.95+ 유지? 아니면 상대 지표(Trafilatura 대비 ΔF1)만?
- `tests/` vs `benchmarks/` 위치 — parity matrix와 분리.

**결정 후 다음 단계.** `benchmarks/wcxb/` 디렉터리, fetch-once 스냅샷
스크립트, 주간 수동 실행.

**근거.** WCXB repo / Trafilatura 공식 평가.

---

## C3. DeepQSE식 2-stage + 경량 reranker 학습 &nbsp; — `status: pending`

**요지.** 현재 bge-reranker-v2-m3는 zero-shot. DeepQSE가 제시한 `(title,
query, sentence)` concat 입력 구조를 참고해, 프로젝트 내 수집된
query–chunk 쌍(12 parity cases + benchmark trace)으로 경량 adapter를
fine-tune.

**기대 효과.** reranker 정확도 개선, 특히 한국어·수치 비교 쿼리.

**검토 포인트.**
- 학습 데이터 규모 — 12 cases는 너무 적음. 어떻게 확장? (합성 쿼리
  생성? 실제 MCP 로그?)
- 오버피팅 위험 — 12 cases에 맞추느라 일반화 상실.
- 유지보수 부담 — 어댑터 버전 관리, 임베딩 모델 교체 시 재학습 비용.
- scope 경계 — 이걸 하는 순간 trawl이 "모델 훈련 프로젝트"가 됨.
  CLAUDE.md의 out-of-scope와 충돌할 수 있음.

**결정 후 다음 단계.** 소규모 spike — fine-tune 없이 DeepQSE 입력 포맷만
흉내내 reranker 호출 → 정확도 변화 측정.

**근거.** arXiv:2210.08809.

---

## C4. Index-based extraction을 profile fallback으로 &nbsp; — `status: pending`

**요지.** 현재 profile cache hit 시 selector로 바로 추출, miss 시 Trafilatura
+ BS fallback. 그 사이에 **인덱스 기반 재활용** 층을 넣어, 동일 도메인
재방문 시 O(1) 청크 위치 재탐색.

**기대 효과.** profile 없는 첫 방문 이후 두 번째 방문부터 추출 비용 급감.
현재도 profile로 비슷한 효과가 있지만, profile이 안 걸리는 페이지 타입을
커버.

**검토 포인트.**
- profile_page와 기능 중복 — 실제 구분이 뭔가?
- 인덱스 저장소(디스크 vs 메모리), 무효화 정책(ETag·Last-Modified 신뢰).
- CLAUDE.md "크롤 아님" 원칙과 충돌하지 않는지 — 인덱스가 도메인 상태를
  누적하면 사실상 mini-crawler가 됨.

**결정 후 다음 단계.** 먼저 profile cache hit/miss 통계로 실제 miss rate가
문제인지 확인. 문제가 없으면 defer.

**근거.** arXiv:2512.06641.

---

## C5. NestBrowse식 "목표 서브셋" 계층적 fetch &nbsp; — `status: pending`

**요지.** single-URL 인터페이스는 유지하되, 내부에서 섹션/영역 단위 lazy
fetch. 긴 페이지(docs, Wikipedia 롱폼)에서 관련 섹션만 끌어오고 나머지는
skip.

**기대 효과.** 토큰 효율 추가 개선, 1M 토큰 급 페이지 대응.

**검토 포인트.**
- scope. NestBrowse는 IS **agent**. trawl은 agent가 아니라 도구.
  계층적 fetch를 도구 쪽에 넣는 게 맞는가, 아니면 호출하는 agent 책임인가.
- 복잡도 폭발 — "섹션 단위"를 무엇으로 정의할 것인가(헤딩? DOM 서브트리?).
- 기존 프로필 시스템과의 관계 — profile이 이미 LCA 기반 서브트리 선택을
  하고 있다면 실질적 차이가 얼마인가.

**결정 후 다음 단계.** 현재 파이프라인에서 "1M 토큰 급"이 실제로 문제인지
로그/벤치로 먼저 확인. 문제 없으면 defer.

**근거.** arXiv:2512.23647.

---

## 리뷰 순서 제안

1. **C1 Late chunking** — 가장 확실한 기술적 업사이드, scope 안전.
2. **C2 WCXB 외부 벤치** — 다른 모든 후보의 측정 기반이 됨, 먼저 깔면
   C1/C3 실험이 쉬워짐.
3. **C3 reranker fine-tune** — scope 민감, spike부터.
4. **C4 Index-based** — 실측 miss rate 확인 후 판단.
5. **C5 계층적 fetch** — scope/복잡도 가장 큼, 마지막.

각 후보를 하나씩 꺼내서 `in_review`로 옮기고 같이 브레인스토밍 → `decided`
처리. 채택된 것은 `superpowers:writing-plans`로 별도 plan 작성.
