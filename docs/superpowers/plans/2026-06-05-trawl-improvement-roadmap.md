# trawl 개선 로드맵 — 2026-06-05 조사 후속

작성일: 2026-06-05
목적: 2026-04-27 로드맵(R1~R7)과 2026-05-18 로드맵(전 단계 종결) 이후의
상태를 결산하고, 4월 말~6월 초 6주간의 외부 delta(유사 도구 릴리스,
신규 연구, injection 방어 동향)를 조사해 다음 작업 큐를 남긴다.
landscape 전체 서베이는 `notes/related-work.md`(2026-04-19)가 여전히
유효하며, 본 문서는 그 위의 **delta + 열린 항목**만 다룬다.

## 프로젝트 목표 재확인

trawl = **단일 URL + 자연어 쿼리 → 질의 관련 근거 청크 top-k만 반환**
(로컬 bge-m3 dense + BM25 RRF hybrid + bge-reranker-v2-m3, MCP 우선).
크롤링/검색/요약은 영구 out-of-scope. primary consumer는
openclaw / hermes / Claude Code 3종이며, 개선 검증은 텔레메트리 누적
대신 `tests/agent_patterns/`의 호출 패턴 위에서 자가검증한다.

**포지션 검증 (2026-06 조사 결과).** 4~6월 사이 "로컬 오픈소스 +
single-URL + query-aware + top-k 청크" 축의 직접 경쟁자는 등장하지
않았다. 오히려 상용 진영이 같은 방향으로 수렴했다:

- **Firecrawl Highlights** (2026-05-08): URL + 쿼리 → 매칭
  문장/코드/표 행 verbatim 반환, "100x 토큰 절감" 주장. SaaS 전용.
- **Exa Highlights** (2026-04-22): 쿼리별 발췌, ~94% 토큰 절감 주장.
  검색 인덱스 의존, 호스티드.

둘 다 trawl의 가치 제안("페이지 전체가 아니라 질의 관련 조각만")을
상용으로 재확인해 준 사례이며, 로컬/투명/오픈소스 축은 여전히 비어
있다. 기존 아키텍처 결정(bge-m3 유지, hybrid 기본 on, Qwen3-Embedding
기각)을 바꿀 외부 근거는 없다.

## 기존 로드맵 결산 (R1~R7)

| 항목 | 상태 | 근거 |
|---|---|---|
| R1 평가/벤치 확장 | **완료** | `benchmarks/reader_comparison.py` + `--retrieval-mode` + warm-repeat 모드 (2026-05-18 로드맵 P1 Goal 1 / P2 Goal 4) |
| R2 extractor 앙상블 | **완료** | `extraction.py:105-246` — score 기반 선택 (query coverage 120 + length/heading/code/table), readability-lxml optional 4번째 후보 (`extraction.py:148,183`) |
| R3 sparse/query-aware fusion | **절반 완료** | BM25 RRF hybrid 기본 on (PR #58). bge-m3 native sparse 및 query-type별 가중 fusion은 미착수 → 본 문서 S3 |
| R4 provenance | **완료** | chunk payload에 `extractor`/`source_url`/`char_span` (`pipeline.py:221-230,296-298`) |
| R5 PDF backend 비교 | **하네스만 완료, 측정 미실행** | `benchmarks/pdf_backend_comparison.py` + `pdf_backend_cases.yaml` + `fetchers/pdf_backends.py` 존재 (커밋 `f4827c8`). outcome note 없음 → 본 문서 S4 |
| R6 schema extraction | **미착수** | 코드 흔적 없음 → 보류 유지 (하단 참조) |
| R7 injection fixture | **미착수** | tests/의 "injection" hit은 전부 reranker title-injection → 본 문서 S2 |

C큐 잔여: **C4(index-based extraction fallback)**만 pending — 개인
에이전트 시나리오에서 데이터 의존성이 약해 계속 보류. (C7 HEAD
probe는 구현 완료 확인: `fetchers/pdf.py:28` `probe()` +
`pipeline.py:330` suffix-less HEAD pre-probe.)

**성능 축 결산.** 성능 frontier는 2026-05-18 로드맵으로 사실상 포화
상태다 — embed cache(warm p95 −96.2%), chunk budget(longform p95
−69~88%), host ceiling, MCP queue 분리가 모두 기본 on이고, 이번
delta에서 새 latency lever(더 빠른 임베딩/리랭커 모델 등)는 나오지
않았다(Qwen3 기각 유지, bge-m3 후속 없음). 아래 큐에서 성능 인접
항목은 S3 하나뿐이며 p95 gate로 회귀만 방지한다.

## 외부 조사 delta 요약 (2026-04-27 → 06-05)

### 유사 도구

| 도구 | delta | 시사점 |
|---|---|---|
| Firecrawl | `/parse`(4/28), Question Format(5/6), **Highlights Format(5/8)**, v2.10 SDK(5/15), `/monitor`(5/26) | Highlights가 기능적으로 가장 근접. SaaS·비공개 메커니즘이라 결정 불변. 벤치 비교 대상 후보 |
| Exa | **Highlights 공개(4/22)** — 쿼리별 발췌 <100ms, 캐시 없음 | 보완 포지션. embed-cache 설계의 외부 비교 데이터포인트 |
| Crawl4AI | v0.8.7~0.8.9 — 전부 보안 패치(Docker RCE/SSRF) | 변화 없음 |
| Jina Reader / ReaderLM | 변화 없음 | — |
| Tavily / Parallel / Bright Data | extraction 축 변화 없음 | — |
| MCP 생태계 | query-aware 신규 경쟁자 없음 (mcp-read-website-fast는 query-blind 전문 변환기) | 포지션 공백 지속 |

### 연구 (2026-02~06)

- **arXiv 2604.01733** (금융 23K 질의, 10개 retrieval 전략 비교):
  hybrid+rerank 2단계가 우승 구성(trawl 구조 재확인). exact-match/
  identifier 쿼리에서 BM25 비중 상향의 이득 신호 → S3 근거.
- **AXE** (arXiv 2602.01838): 0.6B LLM + XPath-traceable 추출, SWDE
  F1 88.1%. R6 재개 시 참고 레퍼런스.
- **DeepQSE / Index-based extraction 계보의 직접 후속 없음.**
- **임베딩/리랭커 delta**: bge-m3 / bge-reranker-v2-m3 후속 없음.
  ML-Embed(arXiv 2605.15081, 2026-05)는 GGUF·한국어 수치 미확인 —
  모니터링만. Qwen3-Reranker-4B는 GGUF 서빙 가능(공식 변환본 +
  `--pooling rank` 필수; community GGUF는 near-zero score 버그)이나
  한국어 수치 미공개 — 수치 공개 전 spike 금지.
- **PDF backend**: MinerU 2.5가 한국어 PDF + 표 추출에서 비교군 최강,
  3.1.0에서 AGPL → Apache 2.0 기반 커스텀으로 라이선스 완화(통합 전
  원문 확인 필요). Docling은 표 97.9% + Apache-2.0 + 더 빠름 → S4.

### Injection 방어 (R7 입력)

문헌 합의: "annotation vs sanitization" 이분법이 아니라 **계층 조합**.

1. **결정론적 strip** — 합법 사용이 없는 패턴: Unicode Tag chars
   (U+E0000–E007F), zero-font / `display:none` 류 CSS-hidden 텍스트.
2. **annotation** — 보존 가치가 있는 의심 콘텐츠: 원문 유지 +
   suspicious marker (기존 R7 기획과 일치, Microsoft Spotlighting
   계열이 공식 지지).
3. **MCP 경계 표시** — `openWorldHint: true` (2025-03-26 spec부터
   가능), 응답 콘텐츠 provenance 마커는 spec Issue #711 / PR #1913
   진행 중 — 확정 전 자체 필드로 선반영, 확정 후 정렬.
4. (에이전트 측 책임) output filtering — trawl 범위 밖이되 명백한
   탈취 패턴(외부 URL 유도 등) annotation은 가능.

실사례 (2025-12~2026-04): Claude 확장 ShadowPrompt, GitHub PR 코멘트
경유 Claude Code 명령 실행("Comment & Control"), claude.ai 대화 탈취
("Claudy Day") — 전부 웹 콘텐츠 경유. 벤치마크: BIPIA, InjecAgent,
AgentDojo, WebSentinel 데이터셋. 단독 CSS-hidden fixture 공개셋은
없음 → Unit 42 payload catalog 기반 자체 fixture가 현실적.

## 시나리오 시뮬레이션 (agent_patterns) 현황과 갭

하네스 조사 결과 (2026-06-05, 패턴 104개 / 목표 ~105 도달):

| # | 갭 | 위험 |
|---|---|---|
| 1 | 최근 live run이 coding shard(24개)만 — 나머지 80패턴 검증 일자 미상 | 회귀 침묵 |
| 2 | `news.yaml:198` `openclaw_news_hada_io` assertion이 구 GeekNews 리브랜드 전 문자열(`points by`/`댓글`) — parity는 PR #49에서 고쳤으나 shard 미반영 (2026-06-05 grep으로 직접 확인) | 확정 false negative |
| 3 | `op: profile_page` step의 반환값을 평가하는 evaluator 없음 — workflows shard의 profile assertion 무력 | 커버리지 구멍 |
| 4 | multi-op 패턴에서 `--repeats`가 step별 독립 반복 — `cache_hit` 의존 step이 flaky | 측정 신뢰성 |
| 5 | `live: optional`이 schema에만 있고 하네스는 전부 live 실행 — 불안정 사이트 noise 처리 불가 | flake |
| 6 | C16 enrichment assertion이 workflows 2패턴뿐 — excerpts/chain_hints 회귀 신호 얇음 | 회귀 침묵 |
| 7 | spec의 fixture 기반 결정론 실행(`live: never` 폴백), budget_diff.md 리포트 미구현 | CI 불가 |
| 8 | coding 외 shard가 전부 single_fetch — repeat_visits/host_transfer/error_handling이 workflows 13개에만 존재 | 시나리오 다양성 |

## 우선순위 작업 큐

### S1. agent_patterns 신뢰성 회복 — `status: proposed` (최우선)

**목표.** 시뮬레이션 하네스를 "전 shard가 주기적으로 green"인 상태로
만든다. 이후 모든 spike의 검증 표면이므로 가장 먼저.

**작업.** (a) stale assertion 일괄 갱신 — news.yaml hada 패턴을
parity PR #49와 동일하게 (`GeekNews`/`topic?id`), 전 shard 1회 live
run으로 추가 drift 색출. (b) `live: optional` skip/warn 메커니즘
구현. (c) `profile_page` step evaluator 추가. (d) multi-op repeats를
시나리오 단위 반복으로 수정(또는 cache-의존 step 문서화 + 제외).
(e) budget_diff.md 리포트.

**Gate.** 전 8 shard live run에서 flake 분류 가능 상태로 pass/skip
구분 보고. parity 15/15 불변. 하네스 변경은 coding 24/24 유지.

### S2. Injection 방어 3계층 (R7 실행) — `status: proposed`

**작업.** (a) fetch 후 전처리: Unicode Tag chars 결정론 strip +
CSS-hidden 텍스트 탐지(`display:none`, `font-size:0`, off-screen,
white-on-white) → 삭제 아닌 `[SUSPICIOUS_HIDDEN]` annotation + 로깅.
(b) instruction-like 문구 정규식 → `ScoredChunk.suspicious_injection`
필드 (recall 우선). (c) MCP: `fetch_page`/`profile_page`에
`openWorldHint: true`, tool description과 응답에 untrusted boundary
명시. (d) fixture: Unit 42 catalog + BIPIA 차용 payload로 최소 3종
시나리오 fixture HTML + 오프라인 테스트.

**Gate.** injection fixture가 marker를 생성. 정상 본문 parity 15/15 +
coding 24/24 회귀 0. 기본 경로 latency 영향 < 5%.

### S3. Query-type aware fusion 가중 (R3 잔여) — `status: closed — already implemented + validated-neutral (2026-06-06)`

**결론 (2026-06-06).** 이 피처는 이미 구현·테스트·default-on 상태였음
(`b81d57b`, 2026-04-27, 로드맵 작성 이전 — R1/R2/R4/C7와 같은 stale
케이스). 미실행이던 A/B만 수행: `benchmarks/query_aware_fusion_ab.py`로
reader_comparison 6케이스(identifier 3/concept 3) weighted↔equal RRF
격리 측정. 결과 **end-to-end neutral (net facts Δ +0, top1 1/6 marginal)**,
순수 fusion 단계에서도 identifier 케이스는 weighted=equal로 **동일**
(rankers concur → 5× 스윙 무효), rerank가 차이를 가림. Gate(coding net
≥ +1) 미충족. 결정: **그대로 유지**(neutral·tested·harmless, retrieval
hot-path 가드레일), 토글 미추가(unrequested config), 단순화/튜닝 안 함.
상세: `docs/superpowers/specs/2026-06-06-query-aware-fusion-validation.md`.
재spike 금지(새 신호 없는 한): dense·BM25가 상단에서 실제로 불일치하는
대형 페이지 코퍼스 또는 reranker 제거 결정.

**(원래 제안, 참고용)** rule-based 쿼리 분류(identifier/code 패턴 vs 개념 질의 —
LLM 호출 없음) → identifier일 때 RRF에서 BM25 rank 가중 상향.
`TRAWL_HYBRID_QUERY_WEIGHTS=1` 토글로 A/B.

**Gate.** (C6 규율 재사용) coding shard net assertion delta >= +1,
flipped-to-fail 0, parity 15/15, retrieval p95 <= +5% (LLM 호출이
없으므로 +20%가 아니라 +5%로 조임).

**근거.** arXiv 2604.01733 "From BM25 to Corrective RAG:
Benchmarking Retrieval Strategies for Text-and-Table Documents"
(hybrid 2단계 우승 + exact-match 쿼리의 BM25 우위; 2026-06-05 arXiv
실재·제목 일치 확인), DAT(arXiv 2503.23013)의 LLM-free 변형.
나머지 2026년 arXiv ID들은 web 조사 출처로 spot-check 미실시 —
해당 spike 착수 시 원문 확인 선행.

### S4. PDF backend 측정 실행 (R5 완결) — `status: proposed`

**작업.** 이미 있는 `benchmarks/pdf_backend_comparison.py`를 실행해
PyMuPDF vs Docling vs MarkItDown vs Unstructured vs MinerU 측정,
outcome note 작성 후 채택/기각. MinerU는 라이선스 원문 확인 선행.
채택 시에도 optional extra로만 (기본 의존성 불변).

**Gate.** PDF parity 케이스 recall 하락 0. 표 질의에서 structured
backend의 answer hit 개선이 있어야 채택. 없으면 PyMuPDF 유지 결정을
outcome note로 남기고 종결.

### S5. 시나리오 다양화 (S1 완료 후) — `status: proposed`

**작업.** coding 외 shard에 repeat_visits / error_handling / 
large_page 패턴 각 1~2개씩 추가 (primary consumer 3종의 실제 워크플로
기준). C16 enrichment assertion을 single_fetch 패턴 5개 이상에 분산.

**Gate.** 신규 패턴 전부 live PASS 또는 `live: optional` skip 분류.
하네스 코드 변경 없이 카탈로그만으로 가능해야 함 (S1-(b),(c) 선행).

### 보류 / 모니터링

- **R6 schema extraction**: 보류 유지. "retrieval-then-extract" 자체는
  업계 표준이 됐지만 LLM 호출 + API 설계가 붙고, content rewriting
  금지 원칙과 긴장. AXE(2602.01838)의 XPath-traceable 방식을 재개 시
  레퍼런스로.
- **C4 index-based fallback**: 보류 유지.
- **Qwen3-Reranker-4B**: 한국어 수치 공개 시에만 spike (공식 변환
  GGUF + `--pooling rank` 필수, community GGUF 버그 주의).
- **ML-Embed (2605.15081)**: GGUF + MIRACL-ko 수치 공개 시 재평가.
- **MCP spec PR #1913** (응답 provenance 마커): 확정 시 S2-(c) 정렬.
- **Firecrawl/Exa Highlights**: reader_comparison 비교 대상 추가 후보
  (둘 다 유료 — 측정 비용 발생, 필요 시에만).

## 권장 실행 순서

1. **S1** — 검증 표면 복구. 이거 없이는 S2~S5의 gate를 믿을 수 없다.
2. **S2** — 외부 의존 없음, 실사례 누적으로 시급성 상승, fixture는
   오프라인이라 S1과 독립 진행도 가능.
3. **S3** — 코드 변경 작음, C6 측정 인프라 재사용.
4. **S4** — 하네스가 이미 있어 측정만 하면 종결되는 부채.
5. **S5** — S1의 하네스 개선 위에서 카탈로그 확장.

각 항목은 기존 spike 규율 유지: design doc 선행 commit →
pre-registered gate → 측정 → 채택/기각, PR 1개 = spike 1개.

## 참고 자료 (delta분만)

- [Firecrawl Changelog](https://www.firecrawl.dev/changelog) /
  [Highlights Format](https://www.firecrawl.dev/blog/question-format-launch)
- [Exa Highlights (2026-04-22)](https://exa.ai/blog/highlights-for-agents)
- [From BM25 to Corrective RAG, arXiv 2604.01733](https://arxiv.org/abs/2604.01733)
- [DAT: Dynamic Alpha Tuning, arXiv 2503.23013](https://arxiv.org/abs/2503.23013)
- [AXE: Adaptive XPath Extractor, arXiv 2602.01838](https://arxiv.org/abs/2602.01838)
- [ML-Embed, arXiv 2605.15081](https://arxiv.org/abs/2605.15081)
- [Qwen3-Reranker GGUF 서빙 가이드](https://gist.github.com/VooDisss/42bce4eb5c76d3c325633886c5e348ee)
- [MinerU changelog](https://opendatalab.github.io/MinerU/reference/changelog/)
- [Microsoft Spotlighting / indirect PI 방어](https://www.microsoft.com/en-us/msrc/blog/2025/07/how-microsoft-defends-against-indirect-prompt-injection-attacks)
- [CaMeL, arXiv 2503.18813](https://arxiv.org/abs/2503.18813) /
  [MELON, arXiv 2502.05174](https://arxiv.org/abs/2502.05174)
- [WebSentinel, arXiv 2602.03792](https://arxiv.org/abs/2602.03792)
- [Unit 42 — Fooling AI Agents (hidden text catalog)](https://unit42.paloaltonetworks.com/ai-agent-prompt-injection/)
- [BIPIA](https://github.com/microsoft/BIPIA) /
  [InjecAgent, arXiv 2403.02691](https://arxiv.org/abs/2403.02691)
- [MCP Tool Annotations](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/) /
  [MCP spec Issue #711](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/711)
