# tests/agent_patterns/ — agent usage pattern catalog

trawl의 1차 consumer (개인 에이전트 **openclaw**, **hermes**, 그리고
**Claude Code**) 가 실제로 어떻게 호출하는지를 큐레이션 패턴으로 박아둔
디렉터리. 자세한 설계 동기와 카테고리 정의는
[`docs/superpowers/specs/2026-04-19-agent-patterns-design.md`](../../docs/superpowers/specs/2026-04-19-agent-patterns-design.md).

```
tests/agent_patterns/
  __init__.py
  schema.py         dataclass + YAML validator (whitelist 기반)
  loader.py         shard 로드 + ID 중복 검사
  README.md         이 문서
  coding.yaml       shard 1 — 코딩 도우미 시나리오 (~25)
  news.yaml         (예정) 시사·뉴스
  finance.yaml      (예정) 경제·주식
  sports.yaml       (예정) 스포츠
  wiki_reference.yaml (예정)
  search_ddg.yaml   (예정)
  multimedia.yaml   (예정)
  workflows.yaml    (예정) repeat_visits / host_transfer / compositional / error_handling
  baseline.json     (auto-generated) `--baseline` 실행 시 갱신
```

## 사용

`tests/test_agent_patterns.py` 가 동봉된 harness:

```bash
# 전체 (live mode — bge-m3 endpoint 필요)
mamba run -n trawl python tests/test_agent_patterns.py

# shard / 단건 / 카테고리 / agent 별 필터
mamba run -n trawl python tests/test_agent_patterns.py --shard coding
mamba run -n trawl python tests/test_agent_patterns.py --only claude_code_python_asyncio_lookup --verbose
mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query
mamba run -n trawl python tests/test_agent_patterns.py --primary-agent claude_code

# 외부 호출 없이 schema 통과만 확인 (CI offline mode)
mamba run -n trawl python tests/test_agent_patterns.py --dry-run

# p95 측정 — repeats N회 후 95퍼센타일로 budget 비교
mamba run -n trawl python tests/test_agent_patterns.py --repeats 3

# baseline 작성 → 이후 --regression이 baseline +20% 초과 시 fail
mamba run -n trawl python tests/test_agent_patterns.py --baseline
mamba run -n trawl python tests/test_agent_patterns.py --regression
```

결과는 `tests/results/agent_patterns_<ts>/` 에 기록되며 gitignored.

## 패턴 작성 규칙

### 단일 operation (가장 흔함)

```yaml
- id: <unique_snake_case>
  primary_agent: [claude_code | openclaw | hermes]   # 다중 가능
  category: single_fetch | code_heavy_query | passthrough | error_handling | large_page
  description: "한 줄 요약 (필수)"
  url: "https://..."
  query: "자연어 쿼리 (passthrough는 생략 가능)"
  live: required | optional | never                  # default: required
  assertions:
    chunks_contain_any: ["a", "b"]                   # 둘 중 하나라도 등장하면 OK
    n_chunks_returned: ">= 3"
    error_is_none: true
  budgets:
    total_ms_p95: 12000
  meta:
    drives: ["C6 hybrid", "C7 HEAD probe"]           # 어느 spike 결정에 활용
```

### 다중 operation (workflows.yaml 전용)

```yaml
- id: openclaw_dashboard_warm_profile
  primary_agent: [openclaw]
  category: repeat_visits
  description: "..."
  steps:
    - op: fetch_page
      url: "https://..."
      query: "..."
      assertions: { suggest_profile: false }
    - op: fetch_page
      ref: 0                                          # 0번 step의 url/query 재사용
      assertions: { suggest_profile: false }
    - op: profile_page
      ref: 0
    - op: fetch_page
      ref: 0
      assertions: { profile_used: true }
      budgets: { total_ms_p95: 4500 }
```

`ref: <step_idx>` 만 지원 (MVP). 동적 capture/template (`{{chunks[0].text}}`)
은 후속 PR.

## ID 규칙

`<primary_agent>_<topic>_<intent>` 형태. shard 안에서, 그리고 모든 shard
횡단으로 고유해야 한다 (`loader._load_one` 가 검사).

## Assertion / Budget DSL 키 화이트리스트

`schema.ASSERTION_KEYS` / `schema.BUDGET_KEYS` 정의. 새 키를 추가하려면
`schema.py` 의 화이트리스트와 `tests/test_agent_patterns.py:_evaluate_*`
양쪽을 함께 갱신.

| Assertion | 의미 |
|---|---|
| `chunks_contain_all` | 모든 substring이 합산된 청크 텍스트에 등장 |
| `chunks_contain_any` | 하나 이상 등장 |
| `chunks_contain_pattern` | regex 매치 |
| `n_chunks_returned` | `int` 또는 `">= N"` / `"<= N"` |
| `profile_used` | bool |
| `path` | `profile_direct` / `profile_retrieval` / `full_page_retrieval` / `raw_passthrough` / `error` |
| `fetcher_used` | `playwright+trafilatura` / `pdf` / `passthrough` / `wikipedia` / `youtube` / `github` / `stackexchange` |
| `error_is_none` | bool |
| `error_contains` | substring |
| `suggest_profile` | bool |
| `content_type` | string (passthrough 검증) |
| `truncated` | bool |

| Budget | 의미 |
|---|---|
| `total_ms_p95` | `--repeats N` 측정의 p95 ≤ 값 |
| `output_chars_max` | 마지막 측정의 output_chars ≤ 값 |
| `n_chunks_max` | 마지막 측정의 chunks ≤ 값 |

## 신규 shard 추가 시

1. YAML 파일을 `tests/agent_patterns/<shard>.yaml` 로 추가.
2. 최상위는 `{patterns: [...]}` 매핑.
3. 각 패턴 ID는 다른 shard와 중복 없이 작성.
4. `python tests/test_agent_patterns.py --dry-run --shard <name>` 으로
   schema 통과 확인.
5. 가능하면 `--shard <name>` live run으로 실 통과 확인 후 PR.

## 파리티 매트릭스와의 차이

| 레이어 | 파일 | 목적 |
|---|---|---|
| extraction 정합성 | `tests/test_cases.yaml` | 페이지 1개 → ground-truth fact가 top-k에 등장 |
| agent 워크플로 | `tests/agent_patterns/*.yaml` | 단일·반복·체인 호출 시 PipelineResult 형태가 의도대로 |

두 레이어가 모두 통과해야 회귀 없는 PR로 간주.
