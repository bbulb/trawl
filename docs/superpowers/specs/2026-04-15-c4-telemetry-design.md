# C4 선결 데이터 수집 — Telemetry (design)

Date: 2026-04-15
Status: proposed
Related: `notes/RESEARCH.md` §C4 (Index-based extraction as profile
fallback)

## 배경

`notes/RESEARCH.md` C4 후보는 profile cache miss가 실제로 문제
라는 실측이 있어야만 `in_review`로 진입할 수 있다. 그 전제는 현재
검증되지 않았다 — 파이프라인에 경로별(hit/miss/fallback) 카운터가
없고, profile cache는 파일로 존재하지만 호출 경로 통계가 남지 않는다.

본 스펙은 이 선결 데이터를 **장기간(최소 수 주 단위) 수집**하기 위한
최소 구현을 정의한다. 구현 자체로 어떤 설계 결정도 내리지 않는다.
수집된 데이터를 훗날 분석해 C4 accept/reject 판단을 내리기 위한
원재료 확보가 목적이다.

## 스코프

**In scope.**
- `fetch_relevant()` 호출 1회 = 이벤트 1건을 JSONL로 append.
- Opt-in (환경변수 기본 off). 테스트·CI 오염 방지.
- URL 평문 기록. 쿼리는 SHA-1 해시(앞 16자)만.
- 단순 크기 기반 rotation (세대 1개).
- `tests/test_telemetry.py` 단위 테스트.

**Out of scope.**
- 분석 CLI 또는 대시보드. 필요 시 ad-hoc pandas/jq로 처리.
- 쿼리 원문·청크 본문·HyDE 텍스트 저장.
- SQLite / 압축 / 다세대 로테이션.
- C3 합성쿼리 학습 데이터 수집 (쿼리 평문이 필요해짐).
- profile_eval 36-case 비-IDEAL 분류 (별도 수동 작업).

## 설계

### 모듈 경계

신규 파일 `src/trawl/telemetry.py`. 공개 인터페이스:

```python
def record(result: PipelineResult) -> None
```

`pipeline.fetch_relevant()` 반환 직전에 한 줄:

```python
telemetry.record(result)
return result
```

삽입 지점은 profile fast path, PDF/YouTube/GitHub/StackExchange/
Wikipedia/passthrough 전용 fetcher 경로, full page retrieval 경로
모두가 수렴하는 마지막 단일 지점이다.

### 활성화·경로

- `TRAWL_TELEMETRY=1` → 활성화. 미설정/`0` → `record()`는 즉시
  return.
- `TRAWL_TELEMETRY_PATH` → 기본 `~/.cache/trawl/telemetry.jsonl`
  (다른 trawl 캐시와 동일 디렉터리).
- `TRAWL_TELEMETRY_MAX_BYTES` → 기본 `67108864` (64 MB).
- 디렉터리 없으면 `mkdir(parents=True, exist_ok=True)` 후
  `chmod 0o700`. 파일 최초 생성 시 `chmod 0o600`.

### 이벤트 스키마

JSONL 한 줄 = 한 이벤트. 플랫 구조. 필드:

| 필드 | 타입 | 출처 |
|---|---|---|
| `ts` | str (ISO-8601 UTC) | 기록 시점 |
| `schema` | int | `1` |
| `host` | str | `urlsplit(result.url).netloc` |
| `url` | str | 평문. `result.url` |
| `query_sha1` | str | `sha1(query).hexdigest()[:16]` |
| `fetcher_used` | str | PipelineResult |
| `path` | str | PipelineResult (`profile_fast` 등) |
| `profile_used` | bool | PipelineResult |
| `profile_hash` | str \| null | PipelineResult |
| `suggest_profile` | bool | PipelineResult |
| `suggest_profile_reason` | str \| null | PipelineResult |
| `content_type` | str \| null | PipelineResult |
| `structured_path` | bool | PipelineResult |
| `rerank_used` | bool | PipelineResult |
| `hyde_used` | bool | PipelineResult |
| `fetch_ms` | int | PipelineResult |
| `chunk_ms` | int | PipelineResult |
| `retrieval_ms` | int | PipelineResult |
| `rerank_ms` | int | PipelineResult |
| `total_ms` | int | PipelineResult |
| `page_chars` | int | PipelineResult |
| `n_chunks_total` | int | PipelineResult |
| `error` | str \| null | PipelineResult |

**저장하지 않는 것**: `query` 평문, `chunks[]`, `hyde_text`.

**스키마 진화.** `schema: 1` 유지. 필드 추가는 JSONL이라 무해
(누락 키는 pandas에서 NaN). 기존 필드 의미 변경 시에만 bump.

### 실패 처리

`record()` 본체 전체를 `try/except Exception`으로 감싼다. 실패 시
`logger.warning("telemetry record failed: %s", e)` 한 줄 남기고
삼킨다. telemetry 실패가 `fetch_relevant()` 반환을 막으면 안 된다.

### 동시성

MCP stdio 서버 + 라이브러리 직접 호출 병행 가능성 있음 →
멀티 프로세스 append 가능.

POSIX `O_APPEND`는 `PIPE_BUF`(최소 4 KB) 이하 단일 `write()`에
대해 원자성을 보장한다. 한 이벤트는 ~500 bytes 수준, 한 줄이 한
`write()`이므로 조건 자연 충족. `fcntl.flock` 불필요.

구현:

```python
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps(event, ensure_ascii=False) + "\n")
```

### Rotation

`record()` 진입 시 `path.stat().st_size`를 체크. `TRAWL_TELEMETRY_MAX_BYTES`
초과 시 `path` → `path + ".1"`로 rename (기존 `.1` 덮어씀). 새
`path`에 append 시작.

Race: 두 프로세스가 동시에 rename을 시도하면 한 쪽이 실패 가능.
`try/except OSError`로 삼킨다 — 어차피 다음 append가 새 파일에서
성공한다.

세대는 1개만 유지. 오래된 데이터를 쥘 필요 없음 — C4 판단은 최근
수 주~수 개월 구간.

### 테스트

신규 `tests/test_telemetry.py`. 외부 서버 의존 없음. `tmp_path`
기반.

1. `TRAWL_TELEMETRY` 미설정 → `record()` no-op, 파일 미생성.
2. `TRAWL_TELEMETRY=1` + `TRAWL_TELEMETRY_PATH=tmp_path/t.jsonl`
   → 호출 1회 후 줄 수 1, 모든 필수 키 존재, `query_sha1`이
   known input에 대해 일치.
3. 호출 3회 → 줄 수 3, 각 줄 독립 파싱 가능.
4. `TRAWL_TELEMETRY_MAX_BYTES=500` + 호출 여러 번 → `.jsonl.1`
   생성, 새 `.jsonl`에 최신 이벤트.
5. `tmp_path` chmod 0o500 → `record()`가 예외 발생시키지 않음,
   logger.warning 호출은 mock으로 확인.

### 파리티 매트릭스 영향

- `TRAWL_TELEMETRY` 미설정이 기본 → `tests/test_pipeline.py` 12
  cases 무변화 예상.
- 커밋 전 12/12 확인 의무 (CLAUDE.md Critical Rules).

### 문서 변경

- `.env.example`: 세 환경변수 추가 + 1줄 주석 각각.
- `ARCHITECTURE.md`: 짧은 "Telemetry (optional)" 섹션 — 스키마
  링크 (본 스펙), opt-in, rotation 규칙.
- `notes/RESEARCH.md` §C4: "선결 데이터 수집 기능 merged
  2026-04-15. N주 수집 후 재검토" 1줄 추가.
- `README.md`: 변경 없음. 내부 도구.

## 분석 방법 (참고용, 구현 없음)

수집 이후 C4 판단 시 사용할 질의 예시:

```python
import pandas as pd
df = pd.read_json("~/.cache/trawl/telemetry.jsonl", lines=True)

# hit/miss 비율
df["profile_used"].mean()

# 호스트별 miss 횟수 상위
df[~df["profile_used"]].groupby("host").size().sort_values(ascending=False).head(10)

# 재질의 여부 = 같은 query_sha1 반복
df.groupby("query_sha1").size().gt(1).mean()

# 재방문 시 profile miss 여전한 호스트 (C4의 직접 타겟)
revisits = df.groupby("host").filter(lambda g: len(g) >= 3)
revisits.groupby("host")["profile_used"].mean().sort_values()
```

분석 작성은 C4 재검토 세션에서 `notes/` 아래 노트북 혹은 ad-hoc
스크립트로 수행한다.

## 근거

- `notes/RESEARCH.md` §C4 자체 기준: "먼저 profile cache hit/miss
  통계로 실제 miss rate가 문제인지 확인".
- CLAUDE.md "Things NOT to change" — `pipeline.py` 로직 격리를
  위해 telemetry를 별도 모듈로 분리.
- CLAUDE.md "In scope: fetching one page at a time" — telemetry는
  도구 관측성이지 크롤이 아님. 스코프 위반 없음.
