# 자동 데이터-갭 요청 프로토콜 (에이전트 ↔ DB 에이전트)

> 작성: 2026-06-17 · Benchmark 에이전트
> 목적: 소비 에이전트(벤치마크·리포트…)가 **값이 비어있는 지표를 자동 감지→요청**하면, DB 에이전트가
> 연동 플랫폼·수집데이터를 검토해 **자동으로 채우는** 풀(Pull) 루프. 사람 개입 없이 점진 보강.

---

## 채널: `apac_kr_ops.agent_data_requests`
소비 에이전트가 write, DB 에이전트가 read/update 하는 공용 요청 큐.
```
dedupe_key STRING   ← requested_by|platform|metric  (★중복/충돌 방지 키)
requested_by STRING ← 요청 에이전트 id (benchmark|report|…)  ★누가 요청했는지 명시
platform STRING · metric STRING · label STRING
status STRING       ← open | ack | fulfilled | unavailable
coverage FLOAT64    ← 요청 시점 커버리지(0=전무)
detail STRING       ← 요청 맥락(요청자 작성)
db_response STRING  ← DB 회신(처리결과/불가사유)
created_at · updated_at · last_seen_at TIMESTAMP
```

### ★ 에이전트 간 충돌/중복 방지 규칙
- **모든 요청은 `requested_by` 를 반드시 명시**하고, `dedupe_key = requested_by|platform|metric`.
- → 같은 에이전트의 같은 요청은 **한 행으로 유지**(MERGE, 중복 누적 X). 타 에이전트(report 등)의 동일 지표 요청은 **별개 행**(누가 필요로 하는지 구분).
- **DB 에이전트는 실제 작업을 `(platform, metric)` 단위로 묶어 1회 처리**하고, 결과를 해당 (platform,metric)의 모든 요청 행에 반영. → 작업 중복 없음, 수요 추적은 requested_by로.

---

## 흐름 (자동)
1. **감지/요청 (소비 에이전트, 매일)** — 벤치마크는 마트 빌드(05:00 KST) 끝에 `gaps.request_gaps()` 실행:
   플랫폼별 지표 커버리지 스캔 → 값 없음(< 2%)이면 `status='open'`으로 MERGE 발행. 이미 채워졌으면 `fulfilled` 자동 처리. `unavailable`로 닫힌 건 재요청 안 함.
2. **소비/충족 (DB 에이전트)** — `WHERE status='open'` 폴링 →
   - 연동 플랫폼 API/수집 raw를 **면밀히 검토**해 데이터 확보 가능하면 수집기/뷰에 반영 → `status='fulfilled'`, `db_response` 기재 → **`dictionary_version` bump**.
   - 원천에 없거나 불가하면 `status='unavailable'`, `db_response`에 사유(예: "Meta 6초는 비표준 지표").
3. **반영 (소비 에이전트)** — 다음 실행 시 `dictionary_version` 변동 감지(`11_AGENT_SYNC_GUIDE.md`) → 새 컬럼 자동 소비. 다음 갭 스캔에서 해당 요청 `fulfilled` 자동 전환.

---

## DB 에이전트가 할 일 (요약)
```sql
-- 1) 미처리 요청(작업은 platform·metric 단위로 묶기)
SELECT platform, metric, ANY_VALUE(label) label,
       ARRAY_AGG(requested_by) requesters, MIN(created_at) since
FROM `innocean-perf-apac-kr.apac_kr_ops.agent_data_requests`
WHERE status='open' GROUP BY platform, metric ORDER BY since;
-- 2) 검토 후 처리: 가능 → 수집/뷰 반영 + dictionary_version bump, 행 status='fulfilled'
--                불가 → status='unavailable' + db_response 사유
UPDATE `…agent_data_requests` SET status=@s, db_response=@r, updated_at=CURRENT_TIMESTAMP()
WHERE platform=@p AND metric=@m;
```

## 현재 벤치마크가 올린 요청(예시, 2026-06-17)
| platform | metric | 내용 |
|---|---|---|
| meta | conversions / revenue | 전환·전환가치(CVR/ROAS) 추적 |
| meta | video_6s / photo_view | 6초 조회·사진 조회(원천 검토) |
| kakao | conversions / revenue / video | 전환·매출·영상지표 |
| tiktok | revenue | 전환가치(ROAS) |
| dv360 | video | 영상지표 |
| naver | all | 네이버 전체 수집(API 발급) |

> 참고: ThruPlay·Meta영상·구간 등 직전 요청분은 DB가 이미 반영(dictionary v2) → 벤치마크 자동 활성 완료.
> 제안: DB 에이전트도 이 큐를 매일 폴링하도록 run_daily에 consumer 추가 시 완전 무인 루프 완성.
