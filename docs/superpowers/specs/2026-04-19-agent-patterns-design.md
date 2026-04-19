# Agent usage pattern catalog + harness — design (2026-04-19)

Branch: `feat/agent-patterns-scaffold`

## 문제

trawl의 후속 spike(C6 hybrid, C7 HEAD probe, C8 fetch cache, C9 per-host
adaptive ceiling)는 모두 **결정 근거 데이터**가 필요하다. 기존 옵션은:

1. `tests/test_cases.yaml` (15-case 파리티 매트릭스) — 페이지 단위 extraction
   정합성만 검증. **워크플로 형태**(반복 visit, host-transfer, passthrough,
   compositional, error handling)를 평가하는 어휘가 없음.
2. `TRAWL_TELEMETRY=1` (C4 텔레메트리) — 실 사용 데이터 누적 필요. 솔로
   운영 + 개인 에이전트 시나리오에서는 **수 주 데이터 대기**가 비효율.
3. `benchmarks/run_benchmark.py` (Jina A/B) + `benchmarks/wcxb/` — 외부
   데이터셋 검증용. 의도된 워크플로가 아닌 추출 품질만 본다.

→ trawl의 1차 consumer는 **개인 에이전트 3종** (openclaw, hermes, Claude
Code)이라는 점이 명확하므로, 그 호출 양상을 **큐레이션 패턴 카탈로그**로
박아두고 즉시 자가검증 가능하게 만든다.

## 접근법

### 신설 레이어 — `tests/agent_patterns/`

| 파일 | 책임 |
|---|---|
| `schema.py` | 단일 패턴 dataclass + YAML → dataclass 변환 + 필드/enum/assertion DSL validator |
| `loader.py` | 모든 shard 로드 + ID 중복 검사 + filter API |
| `coding.yaml` | shard 1 — 코딩 도우미 시나리오 (~25 patterns) |
| `news.yaml`, `finance.yaml`, `sports.yaml`, `wiki_reference.yaml`, `search_ddg.yaml`, `multimedia.yaml`, `workflows.yaml` | 후속 PR (총 ~105 patterns) |
| `README.md` | 카탈로그 작성 규칙 + assertion DSL 레퍼런스 |

기존 `tests/test_cases.yaml` (extraction 정합성 15-case)는 그대로 유지 —
**다른 레이어 다른 목적**. 두 매트릭스가 상호 보완.

### Harness — `tests/test_agent_patterns.py`

`tests/test_pipeline.py` 와 같은 형태의 standalone CLI runner. 의존성: 동
프로젝트 내 `trawl.fetch_relevant` + bge-m3 endpoint(`:8081`). 외부 LLM 호출
없는 path만 쓰는 패턴은 cached fixture로 결정론적 실행 가능 (후속 PR에서).

```bash
python tests/test_agent_patterns.py                     # 전체
python tests/test_agent_patterns.py --shard coding      # shard
python tests/test_agent_patterns.py --only <pattern_id> --verbose
python tests/test_agent_patterns.py --category compositional
python tests/test_agent_patterns.py --baseline          # budget 기준선 갱신
python tests/test_agent_patterns.py --regression        # baseline +20% 초과 시 fail
python tests/test_agent_patterns.py --dry-run           # 스키마 검증만 (live fetch 없음)
python tests/test_agent_patterns.py --repeats 3         # p95 측정용 반복
```

### 단일-operation pattern 스키마

```yaml
- id: claude_code_python_asyncio_lookup
  primary_agent: [claude_code]                  # ⊆ {claude_code, openclaw, hermes}
  category: single_fetch                        # enum (8종)
  description: "Claude Code가 모르는 stdlib API를 1회 조회"
  url: "https://docs.python.org/3/library/asyncio-task.html"
  query: "how to gather coroutines"
  live: required                                # required | optional | never
  assertions:                                   # all must pass
    chunks_contain_any: ["asyncio.gather", "TaskGroup"]
    n_chunks_returned: ">= 3"
    profile_used: false
    error_is_none: true
  budgets:                                      # optional, p95 over --repeats
    total_ms_p95: 12000
    output_chars_max: 5000
  meta:
    drives: ["C6 hybrid", "C7 HEAD probe"]     # 어느 spike의 측정에 활용되는지
```

### Multi-operation pattern (workflows.yaml 전용)

```yaml
- id: openclaw_dashboard_warm_profile
  primary_agent: [openclaw]
  category: repeat_visits
  description: "매일 같은 대시보드 → 3 visit → suggest_profile → profile_page → fast"
  steps:
    - op: fetch_page
      url: "https://example.com/dashboard"
      query: "오늘의 지표 요약"
      assertions: { suggest_profile: false }
    - op: fetch_page
      ref: 0                                    # 0번 step의 url/query 재사용
      assertions: { suggest_profile: false }
    - op: fetch_page
      ref: 0
      assertions: { suggest_profile: true }
    - op: profile_page
      ref: 0
    - op: fetch_page
      ref: 0
      assertions: { profile_used: true, path: "profile_direct" }
      budgets: { total_ms_p95: 4500 }
```

### Categories (enum)

| ID | 검증 대상 |
|---|---|
| `single_fetch` | 1회 fetch + ground-truth 회수 (가장 흔함) |
| `repeat_visits` | 같은 URL 반복 → suggest_profile → profile fast path |
| `host_transfer` | 같은 host A의 profile이 sibling URL B로 transfer |
| `passthrough` | JSON/XML/RSS URL은 query 없이 동작 |
| `compositional` | A의 출력 → B의 query 구성 (C16 enabler) |
| `error_handling` | paywall/anti-bot/dead URL → grace fail |
| `large_page` | 200+ chunk 페이지 token budget 보존 |
| `code_heavy_query` | API docs에 코드형 쿼리 — top-k에 코드 블록 회수 |

### Assertion DSL

지원 키 (스키마가 화이트리스트):

| Key | 의미 |
|---|---|
| `chunks_contain_all` | 모든 substring이 합산된 청크 텍스트에 등장 |
| `chunks_contain_any` | 하나 이상 등장 |
| `chunks_contain_pattern` | regex 매치 |
| `n_chunks_returned` | `int` 또는 `">= N"` / `"<= N"` |
| `profile_used` | bool |
| `path` | string (예: `profile_direct`, `full_page_retrieval`, `raw_passthrough`) |
| `fetcher_used` | string (예: `playwright+trafilatura`, `passthrough`) |
| `error_is_none` | bool |
| `error_contains` | substring (error path 검증용) |
| `suggest_profile` | bool |
| `content_type` | string (passthrough 검증용) |
| `truncated` | bool |

### Budget DSL

| Key | 의미 |
|---|---|
| `total_ms_p95` | `--repeats N` 측정의 p95가 이 값 이하 |
| `output_chars_max` | 마지막 측정의 output_chars 상한 |
| `n_chunks_max` | 마지막 측정의 chunks 수 상한 |

`--regression` mode는 **baseline 대비 +20%** 가 한계.

### 결과 디렉터리

```
tests/results/agent_patterns_<ts>/
  summary.md            # shard×category 매트릭스 + 통과율
  patterns.jsonl        # pattern별 PipelineResult 전체
  budget_diff.md        # baseline 대비 latency/token Δ
  failures/<id>.md      # 실패 패턴 상세
```

기존 `tests/results/` gitignore 규칙이 그대로 적용.

## C16 — Compositional payload enrichment (병합 spec)

`workflows.yaml` 의 `compositional` 카테고리가 의미 있게 채워지려면 trawl이
chained agent 호출을 **저비용**으로 지원하는 metadata를 노출해야 한다.
trawl 자체는 stateless single-shot 유지 (CLAUDE.md "out of scope: crawling"
원칙 보존). 변경 범위는 `PipelineResult` 필드 추가에 한정.

### 신규 필드

```python
@dataclass
class PipelineResult:
    # ... existing fields ...
    excerpts: list[dict] = field(default_factory=list)
        # [{"chunk_idx": int, "summary_120c": str}] — top-3 청크의 첫 1~2 문장
        # (첫 마침표/공백 단위 ngram 추출, LLM 호출 없음)
    outbound_links: list[dict] = field(default_factory=list)
        # [{"url": str, "anchor_text": str, "in_chunk_idx": int}]
        # extraction 단계에서 본 모든 a[href] 보존 — agent가 다음 fetch URL을
        # LLM 호출 없이 선택 가능
    page_entities: list[str] = field(default_factory=list)
        # 제목 + heading_path에서 추출한 noun-phrase 후보 (간단 규칙 기반)
        # agent가 다음 query 보강 시 사용
    chain_hints: dict = field(default_factory=dict)
        # 도메인 타입별 follow-up 힌트 (예: arxiv → {"recommended_followup_filter":
        # "site:arxiv.org"}). 초기엔 빈 dict, 도메인별 룰을 점진적으로 추가.
```

### 추출 규칙

- `excerpts`: 청크 markdown에서 첫 줄 또는 첫 마침표까지, 120자 cap. 코드
  블록은 첫 statement까지.
- `outbound_links`: extraction의 `include_links=True` 결과에서 markdown link
  파싱 → `{url, anchor_text}` 추출. 청크 단위로 grouping.
- `page_entities`: `page_title` + `chunk.heading_path` 의 token 단위 분할
  → length≥2 토큰 + 영문 capitalized / 한글 명사 휴리스틱.
- `chain_hints`: dict factory 패턴, host별 룰. arxiv/wikipedia/github 우선.

### MCP 노출

기존 `to_dict(result)`에 4개 필드 자동 포함. MCP 응답 스키마 변경은
backward-compatible (필드 추가만, 제거/리네임 없음).

### 비용

- LLM 호출 없음. extraction 결과 재활용.
- 추가 latency: <50ms (대부분 markdown re-scan).
- 응답 크기 증가: top-3 excerpt 약 360 chars + outbound_links 보통 5~30 KB
  (대형 페이지 hard cap 10KB).

## 성공 기준

- [ ] `python tests/test_agent_patterns.py --dry-run` 가 모든 shard YAML을
      schema validation 통과 (offline only, 0 외부 호출).
- [ ] `coding.yaml` 25 patterns가 live mode에서 90%+ pass (bge-m3 endpoint
      가용 시). 통과 못 하는 패턴은 known-failure로 marker + 이슈 만들기.
- [ ] 기존 15-case parity matrix 회귀 0 (`python tests/test_pipeline.py`).
- [ ] CHANGELOG / CLAUDE.md / RESEARCH.md (C16 신규 후보)에 진입 기록.

## 측정

- shard별 통과율
- category별 통과율
- 패턴별 `total_ms_p95` (baseline 작성 시)
- C16 enrichment overhead: `excerpts/outbound_links/page_entities` 추가
  전·후 `total_ms` 차이

## 리스크 & 완화

| 리스크 | 완화 |
|---|---|
| 25 patterns 작성 시 URL 변경/사이트 개편으로 false negative | `live: optional` + cached fixture 폴백 (후속 PR), 정기적 sweep run |
| compositional pattern이 너무 복잡해져 maintenance 부담 | MVP는 `ref: <step_idx>` 만 지원, 동적 capture/template은 후속 |
| C16 outbound_links가 페이로드 비대화 | 페이지당 link 상한 + `chain_hints.compress=true` 옵션 |
| schema가 바뀌면 모든 shard 수정 | 버전 필드 + migration helper, 첫 PR에서 v1 고정 |
| openclaw/hermes 실 사용 양상 미파악 → coding 외 shard에서 가짜 패턴 | 사용자 컨펌 후에만 후속 shard PR open. coding은 일반화 가능해 선행 가능 |

## 비결정 (스파이크 중 판단)

- `excerpts.summary_120c` — 첫 문장 cut vs 의미 보존을 위한 punctuation 인지 cut
- `chain_hints` 의 도메인 룰 catalog 시작점 — arxiv/github/wikipedia/youtube
- baseline budget 작성 주기 — 매 PR vs 분기별

## Out of scope

- 패턴 자동 generation (LLM이 URL 보고 패턴 작성) — manual curation만
- 외부 사용자가 자기 패턴 추가 — 일단 솔로 환경 가정
- 동적 capture (`{{chunks[0].text | extract_first_author}}`) — workflows.yaml
  shard PR로 미룸
- 기존 `tests/test_cases.yaml` 마이그레이션 — 두 레이어 공존, 마이그 안 함

## 진행 단계

| Phase | 내용 |
|---|---|
| **0a (이 PR)** | spec + scaffold + coding.yaml MVP + harness + README + CLAUDE/RESEARCH 갱신 |
| **0b** | news.yaml + finance.yaml + sports.yaml (~42 patterns) |
| **0c** | wiki_reference + search_ddg + multimedia + workflows (~38 patterns) |
| **C16 구현** | PipelineResult 4개 필드 + extraction 재활용 추출 규칙 + workflows.yaml compositional unblock |
| **C7~C9 spike** | 각 spike PR이 관련 patterns 추가 후 구현 |
