# 에이전트 자동 동기화 가이드 (풀 방식)

> 대상: 벤치마크·리포트 에이전트 · 작성 2026-06-17
> 목적: DB(마트/스키마)가 업데이트될 때마다 에이전트가 **자동으로 최신 데이터사전을 반영**하도록.
> 방식: **풀(Pull)** — DB가 버전 신호를 발행, 에이전트가 실행 시 확인 후 변경분만 재로드. 누락 없음·인프라 단순.

---

## 핵심 3개 BQ 자산
| 자산 | 용도 |
|---|---|
| `apac_kr_ops.dictionary_version` | **버전 신호.** 마트 스키마가 바뀔 때마다 1행 누적(version_id↑) |
| `apac_kr_ops.v_data_dictionary` | **라이브 사전.** 마트 목록·계층·플랫폼·grain·용도·실제 컬럼(자동) |
| `apac_kr_ops.v_data_catalog` | **전체 자산 인덱스.** raw 포함 모든 테이블 위치·실데이터 여부 |

---

## 에이전트가 매 실행 때 할 일 (3스텝)

### STEP 1. 버전 확인 (가벼움 — 1행)
```sql
SELECT MAX(version_id) AS current_version FROM `innocean-perf-apac-kr.apac_kr_ops.dictionary_version`
```
- 직전 세션에서 기억한 `last_seen_version` 과 **같으면** → 변경 없음, 그대로 진행.
- **다르면** → STEP 2·3.

### STEP 2. 변경 내역 확인 (무엇이 바뀌었나)
```sql
SELECT version_id, updated_at, change_summary
FROM `innocean-perf-apac-kr.apac_kr_ops.dictionary_version`
WHERE version_id > {last_seen_version} ORDER BY version_id
```
- `change_summary` = "뷰N·컬럼M | 추가X 삭제Y 타입변경Z | +추가컬럼… -삭제컬럼…"

### STEP 3. 라이브 사전 재로드
```sql
SELECT mart_name, layer, platform_coverage, grain, purpose, column_count, columns
FROM `innocean-perf-apac-kr.apac_kr_ops.v_data_dictionary`
```
- 새 마트·새 컬럼을 마트 빌더/쿼리에 반영 → `last_seen_version` 을 current_version 으로 갱신.

---

## 에이전트 시스템 프롬프트에 넣을 룰 (복붙)
> **데이터 동기화:** 작업 시작 시 `apac_kr_ops.dictionary_version` 의 `MAX(version_id)` 를 조회한다.
> 직전에 본 version 과 다르면, `apac_kr_ops.v_data_dictionary` 를 다시 읽어 마트 목록·스키마 변경(신규 컬럼/마트)을 반영한 뒤 진행한다.
> 데이터 위치가 불확실하면 `apac_kr_ops.v_data_catalog` 에서 검색한다. 상세 스키마/규칙은 `08_DATA_DICTIONARY.md`.

---

## 동작 보장 (DB 측 자동화)
- 마트/뷰 스키마가 바뀌면 **`run_daily`(매일 03:00) + 마트 변경 스크립트**가 `dictionary_version.bump_if_changed()` 를 호출 → 변경 시에만 새 버전 기록(멱등).
- 버전이 안 오르면 = 스키마 그대로 = 에이전트가 재로드 불필요(안전).
- 컬럼은 append-only 원칙이라 기존 쿼리는 안 깨짐(신규 컬럼 추가만).

**요약: 에이전트는 `MAX(version_id)` 한 줄만 보면 됩니다. 바뀌었을 때만 사전을 다시 읽으면 자동 동기화 완료.**
