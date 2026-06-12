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

## P2 — 타겟(연령/성별)  ⚠️ **현재 미수집 — 활성화 필요**

확정: `ads_AgeRangeBasicStats_*`·`ads_AgeRangeConversionStats_*`·`ads_Gender*Stats_*` **전 계정 0행**.
- 즉 Google Ads Data Transfer가 **Age/Gender 리포트를 실제로 적재하지 않는 상태**(테이블 스키마만 존재).
- 원인 후보: DTS 전송에 데모그래픽 리포트 미포함 / 계정 데모 데이터 없음 / 리포트 옵션 비활성.
- Meta: insights에 `breakdowns=age,gender` 옵션 존재하나 **현재 추출기 미적용**.

**조치(택1, 회신 주시면 진행):**
1. **Google**: DTS 전송 설정 점검 — Age/Gender 리포트 활성/권한 확인(dev_adtech 영역 가능성). 활성화되면 자동 적재 → device처럼 세그먼트 뷰 신설.
2. **Meta**: meta_extractor에 age/gender breakdown 추가(API 호출·행수 증가) + 백필.
- ETA: Google은 DTS 설정 확인 후 수일 / Meta는 추출기 수정+백필 수일.
- **그때까지는 "age/gender 미수집"으로 UI에서 해당 차원 숨김 처리 권장**(요청서 제안대로).

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
| 항목 | 상태 | 다음 |
|---|---|---|
| P1 device | ✅ 완료(`v_perf_unified_device`) | 마트 연결만 |
| P2 age/gender | ⚠️ 미수집 | Google DTS 활성화 점검 + Meta breakdown — **진행 회신 요청** |
| P3 revenue/영상 | 가능 | 우선순위 정해지면 착수 |
| P0 campaign_name | 자체해결됨 | (선택) 원천 보강 |

device 뷰는 지금 바로 소비 가능합니다. P2를 Google/Meta 중 어디부터 갈지 알려주시면 그 기준으로 활성화 작업 시작하겠습니다.
