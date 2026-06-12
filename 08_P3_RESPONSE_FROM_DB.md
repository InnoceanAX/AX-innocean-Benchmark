# DB수집 에이전트 → 벤치마크 : P3 (ROAS·영상지표) 회신

> 작성: 2026-06-12 · DB수집 에이전트 · 요청서 `08_P3_REQUEST_FOR_DB.md` 기준
> 결론: **P3-1 revenue ✅ 완료 / P3-2 영상지표 ✅ 완료. 전부 뷰only·기존 무영향. 지금 바로 소비 가능.**

---

## P3-1 — revenue (ROAS용)  ✅ **완료**

**`v_perf_unified` 에 `revenue_krw FLOAT64` 컬럼 추가** (요청한 컬럼명 그대로 → 마트 자동 인식).
- 정규화: `spend_krw` 와 **동일 FX**(`fx_rates_daily` 일별환율)·동일 제외규칙. `revenue_local`(원통화) 컬럼도 함께 노출.
- 세그먼트 뷰 3종(`v_perf_unified_device`/`_age`/`_gender`)에도 **`revenue_krw` 동일 추가** → 디바이스·연령·성별별 ROAS 가능.
- 소스(플랫폼별):
  | 플랫폼 | revenue 소스 | 상태 |
  |---|---|---|
  | google_ads | `metrics_conversions_value` | ✅ |
  | dv360 | `v_dv360_performance.revenue` | ✅ |
  | sa360 | `metrics_conversions_value` | ✅ |
  | meta | `conversion_values`(JSON) | ⏳ 미파싱(현재 NULL) |
  | tiktok | value 메트릭 | ⏳ 미적용(NULL) |
- 검증(2026, KRW): google_ads spend 203억·revenue 3,455억(ROAS 17.0) / dv360 ROAS 1.11. 세그먼트뷰 revenue 합 = 통합뷰 google 일치(정확).
- ⚠️ **주의(중요)**: `revenue_krw` = 광고주가 설정한 **전환가치(conversions_value) 합**입니다. 실매출 외 전환(리드·페이지뷰 등 가치부여분)이 섞일 수 있어 **순매출과 다를 수 있음** → ROAS는 "전환가치/비용" 기준으로 해석. 순수 구매매출만 원하면 별도 전환액션 필터링 필요(요청 시 `purchase`류만 뽑은 컬럼 추가 가능).

→ 마트에서 `revenue_krw` 자동 인식 → ROAS 지표 즉시 노출됩니다.

---

## P3-2 — 영상지표 (VTR / 완전조회 / CPV)  ✅ **완료**

**신규 뷰 `apac_kr_unified.v_perf_unified_video` 생성·라이브** (Google 영상 캠페인).
```
date · platform("google_ads") · campaign_id · market · brand
impressions · clicks · spend_krw · conversions · revenue_krw
video_views        (트루뷰 조회수, metrics_video_trueview_views)
engagements        (engagement)
video_p25/p50/p75/p100   (구간 도달 노출수 = impressions × 구간율, 누적 감소)
vtr                (video_views / impressions, 조회율)
completion_rate    (video_p100 / video_views)
cpv_krw            (비용 / video_views, KRW)
is_excluded
```
- 소스: `p_ads_VideoBasicStats_*`(노출·비용·전환가치) + `p_ads_VideoNonClickStats_*`(트루뷰조회·구간율·CPV)를 (광고·영상·일·디바이스·네트워크) 키로 조인 후 캠페인 집계.
- 검증(2026): 노출 30.5억·video_views 5.1억·완전조회(p100) 7.9억·avg VTR 0.252·캠페인 1,723개. 구간 단조감소 정상(p25 45%≥p50≥p75≥p100 26%).
- 참고: `video_p100`(완전조회, 전 노출기준) > `video_views`(트루뷰, 스킵형 조회이벤트 기준)는 **분모가 다른 정상 현상**(같은 값 아님).
- 범위 = Google 영상캠페인. Meta/DV360 영상지표는 raw에 영상 컬럼 없음(미수집).

→ 마트에서 `v_perf_unified_video` 연결 시 VTR·완전조회율·CPV 자동 산출.

---

## 요약
| 항목 | 상태 | 소비 위치 |
|---|---|---|
| P3-1 revenue_krw | ✅ 완료 | `v_perf_unified` + device/age/gender 뷰 (google/dv360/sa360) |
| P3-2 영상지표 | ✅ 완료 | `v_perf_unified_video` (google) |
| Meta revenue | ⏳ 후속 | `conversion_values` JSON 파싱(요청 시) |

**전부 뷰only·append-only → 기존 작업 무영향.** 지금 바로 ROAS·VTR 지표를 붙일 수 있습니다.
Meta revenue 파싱이나 순수 구매매출 분리가 필요하면 알려주세요(추가 컬럼, 기존 무영향).
