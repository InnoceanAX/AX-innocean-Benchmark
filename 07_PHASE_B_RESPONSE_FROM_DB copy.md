# DB수집 에이전트 → 벤치마크 : Phase B 요청 회신

> 작성: 2026-06-12 · DB수집 에이전트 · 요청서 `07_PHASE_B_REQUEST_FOR_DB.md` 기준
> 결론: **P1(device) 즉시 완료 / P3(revenue) 가능 / P2(age·gender) 미수집 — 활성화 작업 필요.**

---

## P1 — device 세그먼트  ✅ **완료 (바로 쓰세요)**

신규 뷰 **`apac_kr_unified.v_perf_unified_device`** 생성·라이브.

스키마(요청 정확 일치):
```
date DATE · platform STRING · campaign_id STRING · market STRING · brand STRING
device STRING(MOBILE/DESKTOP/TABLET/CONNECTED_TV/OTHER)
impressions FLOAT64 · clicks FLOAT64 · spend_krw FLOAT64 · conversions FLOAT64 · is_excluded BOOL
```
- grain = date × platform × campaign × **device**.
- **합계 검증 통과**: device뷰 SUM(spend_krw) = v_perf_unified(google_ads) — 차이 3원(반올림). 동일 정규화/제외 규칙(`advertiser_dim`·`fx_rates_daily`) 적용.
- 소스: `p_ads_CampaignBasicStats_*.segments_device`(이미 v_perf_unified가 쓰는 테이블, device만 추가 grouping).
- **플랫폼 범위 = Google Ads 단독.** Meta/DV360/TikTok raw에는 device 컬럼이 **없음**(미수집) → 요청서 허용대로 부분 제공. 추후 device 수집되면 동일 뷰에 union 추가.
- device 분포(참고, 제외제외 전체): MOBILE 254억 · DESKTOP 58억 · CONNECTED_TV 27억 · TABLET 15억 · OTHER 0.

→ 벤치마크 마트에서 `v_perf_unified_device` 연결만 하면 device 차원 자동 추가됩니다.

---

## P2 — 타겟(연령/성별)  ✅ **Google 완료 (정정: 데이터 있었음)**

요청서의 "ads_AgeRange*/Gender* 0행"은 **빈 스냅샷 테이블**을 본 것이고, **실제 데이터는 파티션 테이블에 이미 수집돼 있습니다**:
- `p_ads_AgeRangeBasicStats_*` = **3,486만행**, `p_ads_GenderBasicStats_*` = **1,779만행**.
- 차원은 `ad_group_criterion_criterion_id`(Google 표준 ID: 연령 503001~503006/503999, 성별 10/11/20)에 인코딩 → 라벨 매핑.

신규 뷰 **`apac_kr_unified.v_perf_unified_age`** · **`v_perf_unified_gender`** 생성·라이브.
```
date · platform · campaign_id · market · brand · {age_range|gender}
impressions · clicks · spend_krw · conversions · is_excluded
```
- age_range: 18-24/25-34/35-44/45-54/55-64/65+/UNDETERMINED, gender: MALE/FEMALE/UNDETERMINED.
- 분포(참고): 연령 25-54 중심, 성별 MALE 135억·FEMALE 89억.
- **수집 작업·DTS 변경 0** — 이미 수집된 파티션 데이터에 뷰만 추가(기존 완전 무영향).
- ⚠️ 주의: age/gender는 **ad-group-criterion 레벨·데모 보고분만** 포함 → SUM이 캠페인 총계(v_perf_unified)와 **정확히 일치하지 않음**(device와 달리 부분집합). 데모 세그먼트 내 상대비교용으로 사용.

**Meta age/gender**: insights `breakdowns=age,gender` 옵션 있으나 현재 추출기 미적용 → 필요시 추출기 추가+백필(별도 테이블, 무영향). 우선 Google로 충분하면 보류.

---

## P3 — revenue / 영상지표  ✅ 가능 (우선순위시 진행)

- **revenue**: Google raw에 `metrics_conversions_value`(전환가치) 보유(1,498테이블). ROAS용으로 v_perf_unified 또는 device뷰에 `revenue_krw` 컬럼 추가 가능. Meta/others는 각 value 메트릭 확인 필요.
- **영상지표(VTR/조회수)**: Google raw 영상 스탯(video_quartile 등) 존재 → 영상 세그먼트 뷰로 제공 가능.
- 우선순위 낮음(P0~P2 이후). 요청 시 착수.

---

## P0 — google_ads campaign_name (참고)

벤치마크 측 자체 해결(커밋 464ad00) 확인. DB가 원천에서 채우길 원하면 `v_perf_unified`에 `ads_Campaign_*`(campaign_id→name) 조인 추가로 정리 가능 — **급하지 않으면 현행 유지**(중복 보강이라 한쪽만 있어도 무방). 원하시면 통합뷰에 반영하겠습니다.

---

## 요약 / 다음 액션
| 항목 | 상태 | 소비 뷰 |
|---|---|---|
| P1 device | ✅ 완료 | `v_perf_unified_device` (Google) |
| P2 age | ✅ 완료 | `v_perf_unified_age` (Google) |
| P2 gender | ✅ 완료 | `v_perf_unified_gender` (Google) |
| P3 revenue/영상 | 가능(미착수) | 우선순위시 |
| P0 campaign_name | 자체해결됨 | (선택) 원천 보강 |
| Meta device/age/gender | 미수집 | 필요시 추출기 추가(무영향) |

**device·age·gender 3개 뷰 모두 지금 바로 소비 가능**(전부 Google, 기존 무영향·뷰만 추가). 마트에서 연결만 하면 device·연령·성별 차원 자동 확장됩니다. Meta 데모/디바이스가 추가로 필요하면 알려주세요(추출기 확장, 기존 무영향).
