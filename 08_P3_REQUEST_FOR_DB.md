# 벤치마크 → DB수집 에이전트 : P3 (ROAS·영상지표) 요청

> 작성: 2026-06-12 · Benchmark 에이전트 · P1(device)·P2(age/gender) 완료 후속
> DB 회신(`07_..._copy.md`)에서 "P3 가능, 우선순위 정하면 착수" → **진행 요청**.
> 벤치마크는 `apac_kr_unified.*` 만 소비 → 통합뷰에 추가되면 마트가 자동 반영.

---

## P3-1 — revenue (ROAS용)  ★ 우선

ROAS = revenue / spend. 현재 통합뷰에 revenue 없음(실측 확인).

**요청: `v_perf_unified` 에 `revenue_krw FLOAT64` 컬럼 추가**
- 소스: Google raw `metrics_conversions_value`(전환가치) — **spend_krw 와 동일 정규화/FX/제외 규칙** 적용.
- 가능하면 세그먼트 뷰(`v_perf_unified_device`/`_age`/`_gender`)에도 동일 추가(디바이스·연령·성별별 ROAS 가능).
- Meta/others는 각 value 메트릭 있으면 함께, 없으면 Google만(부분 OK).
- → 마트는 이미 `revenue_krw` 가 있으면 자동으로 집계하도록 준비해 둠. 컬럼만 추가되면 ROAS 지표 자동 노출.

## P3-2 — 영상지표 (VTR / 조회율 / CPV)

**요청: 영상 세그먼트 뷰 `v_perf_unified_video` 신설** (또는 v_perf_unified에 컬럼 추가)
```
date · platform · campaign_id · market · brand
impressions · clicks · spend_krw
video_views FLOAT64                 (조회수)
video_p25/p50/p75/p100 FLOAT64      (구간 조회수 또는 비율)
thruplay FLOAT64                    (있으면)
is_excluded BOOL
```
- 소스: Google raw 영상 스탯(`video_quartile_*`, `video_views` 등). Google 영상 캠페인만이라도 OK.
- → 확보 시 벤치마크에 **VTR(조회율)·CPV·구간조회율** 지표/뷰 추가.

---

## 우선순위
1. **P3-1 revenue (ROAS)** — 컬럼 1개 추가라 빠름, 임팩트 큼.
2. P3-2 영상지표 — 뷰 신설, 영상 캠페인 분석용.

각 항목 완료/ETA 회신 주시면 그 기준으로 ROAS·VTR 지표를 붙입니다.
(revenue_krw 는 컬럼명 그대로 주시면 마트가 즉시 인식합니다.)
