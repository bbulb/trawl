# 다음 세션 — Contextual Retrieval 측정 및 채택 판단

작성일: 2026-05-02

## 현재 상태

브랜치/HEAD:

- 브랜치: `develop`
- 최종 HEAD: `df84be5 test: stabilize contextual profile coverage`
- 작업트리: clean

이번 세션에서 완료한 것:

- `docs/superpowers/specs/2026-05-02-contextual-retrieval-design.md`
- `docs/superpowers/plans/2026-05-02-contextual-retrieval.md`
- `src/trawl/contextual.py`
- `retrieval.retrieve(..., context_texts=...)`
- full/profile retrieval path contextual wiring
- contextual telemetry fields
- README / `.env.example` 설정 문서화
- focused unit/integration tests

주요 커밋:

```text
df84be5 test: stabilize contextual profile coverage
375a26a docs: document contextual retrieval flag
cb93338 feat(telemetry): record contextual retrieval stats
ab6ae00 feat(pipeline): wire contextual retrieval inputs
1353713 fix(retrieval): validate contextual input alignment first
474a20b feat(retrieval): accept contextual ranking inputs
a2197ad feat(retrieval): add contextual prefix builder
7f37c71 docs: plan contextual retrieval prefix
682fc38 docs: design contextual retrieval prefix
```

## 검증 결과

Reference environment는 mamba env `trawl`이다. 로컬 virtualenv가 아니라 아래
명령을 사용한다.

```bash
mamba run -n trawl pytest tests/test_contextual.py \
  tests/test_retrieval_contextual.py \
  tests/test_pipeline_contextual.py \
  tests/test_retrieval_hybrid.py \
  tests/test_telemetry.py -q
```

결과:

```text
37 passed in 1.15s
```

```bash
mamba run -n trawl ruff check src tests
```

결과:

```text
All checks passed!
```

Task 5 중 live parity 관측:

```text
baseline:   14/15, failed korean_wiki_person
contextual: 14/15, failed korean_wiki_person
```

즉 이 세션에서는 contextual-specific flipped-to-fail은 관측되지 않았다. 다만
baseline 자체가 15/15가 아니므로 다음 세션에서 parity 환경을 다시 확인해야 한다.

## 기능 요약

기본값은 off:

```bash
TRAWL_CONTEXTUAL_RETRIEVAL=0
TRAWL_CONTEXT_PREFIX_MAX_CHARS=320
```

활성화:

```bash
TRAWL_CONTEXTUAL_RETRIEVAL=1
```

활성화 시 dense embedding, BM25 prefilter, hybrid BM25 ranking 입력이
`contextual.build_contextual_texts(...)` 결과로 바뀐다. 반환되는 chunk text,
MCP payload text, reranker document construction은 바뀌지 않는다.

Telemetry에 추가된 필드:

- `contextual_retrieval_used`
- `context_prefix_chars_total`
- `context_prefix_chars_avg`

Raw context text는 telemetry에 기록하지 않는다.

## 다음 세션 목표

목표는 **구현 추가가 아니라 측정과 채택 판단**이다.

1. baseline과 contextual mode를 같은 환경에서 재측정한다.
2. flipped-to-fail, flipped-to-pass, latency p95, prefix length stats를 기록한다.
3. gate 통과 여부를 판단한다.
4. gate 통과 시 default-on 또는 `auto` mode 설계를 새 spec으로 분리한다.
5. gate 미통과 시 feature는 default off 유지하고 measurement note만 남긴다.

## 측정 명령

### 1. Sanity

```bash
git status --short
mamba run -n trawl pytest tests/test_contextual.py \
  tests/test_retrieval_contextual.py \
  tests/test_pipeline_contextual.py \
  tests/test_retrieval_hybrid.py \
  tests/test_telemetry.py -q
mamba run -n trawl ruff check src tests
```

### 2. Parity baseline

```bash
unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_pipeline.py
```

Record:

- pass count
- failed case ids
- total latency if printed
- notes about embedding/reranker availability

### 3. Parity contextual

```bash
TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_pipeline.py
```

Record the same fields, then diff against baseline:

- `flipped_to_pass`
- `flipped_to_fail`
- latency delta

### 4. Query-heavy / agent-pattern subset

If agent pattern runner is available and live dependencies are healthy:

```bash
unset TRAWL_CONTEXTUAL_RETRIEVAL
mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query

TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_agent_patterns.py --category code_heavy_query
```

If this command shape is stale, inspect `tests/test_agent_patterns.py --help` first.

### 5. Telemetry sample

Use isolated telemetry path so user cache is not polluted:

```bash
rm -f /tmp/trawl-contextual-telemetry.jsonl
TRAWL_TELEMETRY=1 \
TRAWL_TELEMETRY_PATH=/tmp/trawl-contextual-telemetry.jsonl \
TRAWL_CONTEXTUAL_RETRIEVAL=1 \
  mamba run -n trawl python tests/test_pipeline.py --only <one-fast-case>
```

Then inspect:

```bash
tail -n 3 /tmp/trawl-contextual-telemetry.jsonl
```

Confirm:

- `contextual_retrieval_used: true`
- `context_prefix_chars_total > 0`
- no raw context/chunk text fields

## Pre-Registered Gate

Adopt candidate if all are true:

| Metric | Gate |
|---|---|
| parity flipped_to_fail | `0` |
| query-heavy / agent-pattern net assertion delta | `>= +1`, or one documented retrieval failure flips to pass |
| latency p95 increase | `<= +20%` |
| telemetry privacy | no raw context/chunk text |
| focused tests | pass |
| ruff check | pass |

If the only outcome is “same pass rate, same failures, no latency harm”, keep the
feature default off and document the neutral result. Do not flip default based on
theory alone.

## Output Artifact To Create Next

Create a measurement note in a tracked path:

```text
docs/superpowers/handoffs/2026-05-02-contextual-retrieval-measurement.md
```

Suggested structure:

```markdown
# Contextual Retrieval Measurement — 2026-05-02

## Environment

## Commands

## Results

| mode | pass | fail ids | latency p50/p95 | notes |

## Flips

## Telemetry Check

## Decision

adopt / keep-off / revise

## Follow-Up
```

## Likely Next Engineering Work After Measurement

If contextual retrieval passes gate:

1. Write a design spec for default-on or `auto` mode.
2. Consider contextual embedding cache keying:
   - URL
   - markdown hash
   - model
   - chunker version
   - contextual mode
   - prefix max chars / prefix version

If contextual retrieval is neutral or fails gate:

1. Keep `TRAWL_CONTEXTUAL_RETRIEVAL=0`.
2. Move to embedding cache as next high-value performance task.
3. Preserve contextual code behind flag for future query-type-specific experiments.

## Cautions

- Use `mamba run -n trawl`, not a local virtualenv.
- Do not treat the previous `14/15` parity result as a contextual regression; both
  modes failed the same case in the same environment.
- Do not change reranker inputs in this measurement pass.
- Do not flip defaults without a measurement note and gate decision.
