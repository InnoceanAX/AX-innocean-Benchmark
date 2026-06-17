# DB 수집 에이전트 작업 안내 — 자동 데이터-갭 요청 처리 (이 문서 하나로 충분)

> 받는이: DB_Management_System 에이전트 · 작성: Benchmark 에이전트(2026-06-17)
> 한 줄 요약: **소비 에이전트들이 "값 없는 지표"를 BQ 큐에 자동 적재합니다. 매일 그 큐를 읽고 → 데이터 채우고 → 큐를 닫고 → 버전을 올리세요.** 그러면 소비 에이전트가 자동 반영합니다.

---

## 0. 당신(DB 에이전트)이 매일 할 일 — 3스텝

### STEP 1. 미처리 요청 읽기 (작업은 platform·metric 단위로 묶기)
```sql
SELECT platform, metric, ANY_VALUE(label) AS label,
       ARRAY_AGG(DISTINCT requested_by) AS requesters,   -- 누가 필요로 하는지
       MIN(created_at) AS since
FROM `innocean-perf-apac-kr.apac_kr_ops.agent_data_requests`
WHERE status = 'open'
GROUP BY platform, metric
ORDER BY since;
```

### STEP 2. 각 (platform, metric) 처리 — 둘 중 하나
- **A) 채울 수 있으면**: 연동 플랫폼 API/수집 raw를 검토해 데이터를 확보하고
  **표준 통합뷰에 컬럼/뷰로 노출**(아래 3 참고) → 그 다음 STEP 3.
- **B) 불가하면**: 사유와 함께 `unavailable` 로 닫기(재요청 방지).
```sql
UPDATE `innocean-perf-apac-kr.apac_kr_ops.agent_data_requests`
SET status='unavailable', db_response='(사유)', updated_at=CURRENT_TIMESTAMP()
WHERE platform=@p AND metric=@m;   -- 같은 (platform,metric)의 모든 요청자 행 일괄 처리
```

### STEP 3. 채웠으면 버전 올리기 (필수 — 소비자 자동반영 트리거)
- 통합뷰 스키마가 바뀌면 `apac_kr_ops.dictionary_version.bump_if_changed()` 호출(이미 run_daily에 있으면 자동).
- 요청 행은 닫지 않아도 됨: **소비 에이전트가 데이터 확인되면 자동으로 `fulfilled` 처리**합니다.
  (원하면 `status='fulfilled', db_response=...`로 직접 닫아도 무방)

> **요청 큐는 당신이 만들 필요 없습니다.** 소비 에이전트가 `CREATE TABLE IF NOT EXISTS`로 생성·적재합니다. 당신은 읽고/닫기만.

---

## 1. 큐 테이블 = `apac_kr_ops.agent_data_requests`
```
dedupe_key STRING   -- requested_by|platform|metric  (중복키)
requested_by STRING -- 요청 에이전트 (benchmark|report|…)  ← 누가 요청했는지
platform · metric · label STRING
status STRING       -- open | ack | fulfilled | unavailable
coverage FLOAT64    -- 요청시점 커버리지(0=전무)
detail STRING       -- 요청 맥락          db_response STRING -- 당신의 회신
created_at · updated_at · last_seen_at TIMESTAMP
```
**중복/충돌 방지 규칙(중요):** 요청은 `requested_by` 별로 행이 분리됩니다(같은 지표라도 benchmark/report 각각 1행).
→ **실제 수집 작업은 `(platform, metric)` 단위로 1회만** 하면 되고, 그 결과는 같은 (platform,metric)의 모든 요청자 행에 적용됩니다. `requested_by`는 "누가 이 데이터를 기다리는지" 추적용입니다.

---

## 2. 데이터 채우는 곳 = 표준 통합뷰 (소비자는 raw 직접 안 봄)
- 캠페인 단위 지표 → **`apac_kr_unified.v_perf_unified`** 에 컬럼 추가(append-only).
- Meta 참여/영상 확장 → 기존 **`v_perf_unified_meta_ext`** 패턴.
- 영상 → **`v_perf_unified_video`**(플랫폼별 행, 이미 google+meta). 세그먼트 → `v_perf_unified_{device,age,gender}`.
- 컬럼은 **append-only**(기존 쿼리 안 깨짐). 추가 후 `dictionary_version` bump면 끝.

---

## 3. 지금 열려 있는 요청 (현재 10건) + 처리 힌트
| platform | metric | 무엇을 / 어디서 찾나(힌트) |
|---|---|---|
| **meta** | conversions | Meta `actions` JSON의 전환액션(purchase/lead/complete_registration 등) → `v_perf_unified.conversions`(현재 Meta NULL) 채우기 |
| **meta** | revenue | `action_values`/omni_purchase 전환가치 → `revenue_krw` 커버리지 향상(현재 ~0.6%만) |
| **meta** | video_6s | Meta는 6초 표준 지표 없음(3초=video_view, ThruPlay, quartile만). **대개 `unavailable` 판정 대상** — 확인 후 닫기 |
| **meta** | photo_view | `actions` action_type `photo_view` 존재여부 확인 → 있으면 meta_ext에 추가, 없으면 `unavailable` |
| **kakao** | conversions / revenue | Kakao Moment API 전환/전환가치 수집 가능여부(현 토큰 스코프 확인) |
| **kakao** | video | **raw `kakao_moment_stats_daily`에 `video_play_3s`·`vtr` 이미 있음** → 통합뷰로 노출만 하면 됨(빠름) |
| **tiktok** | revenue | TikTok insights의 전환가치(revenue/conversion value) 필드 → `revenue_krw` |
| **dv360** | video | DV360 raw 영상 메트릭(있으면) → `v_perf_unified_video`에 dv360 합류 |
| **naver** | all | 네이버 GFA/검색 API 키 발급 후 수집 → `v_perf_unified`에 naver 플랫폼 합류 |

> 각 항목은 "가능하면 채우고, 불가하면 사유와 함께 unavailable" 입니다. 우선순위 제안: **kakao video(빠름) → meta conversions/revenue(임팩트 큼) → 나머지**.

---

## 4. 확인 / 끝
- 채운 뒤 `dictionary_version`이 오르면, 소비 에이전트(벤치마크 등)가 다음 실행에 **자동으로 새 지표를 활성화**합니다(당신이 소비자 코드를 건드릴 필요 없음).
- 큐는 매일 소비 에이전트가 다시 스캔 → 채워진 항목은 자동 `fulfilled`, 남은 갭만 `open` 유지.
- **완전 무인화 제안:** 당신의 `run_daily` 에 위 STEP 1~3을 도는 consumer를 추가하면, 사람 개입 없이 갭이 지속적으로 메워집니다.

— 끝. 추가 질문은 이 큐의 `db_response`에 적어주시면 소비 측에서 확인합니다.
