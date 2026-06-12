# 데이터 사전 — 소비자(벤치마크·리포트) 가이드

> 갱신: 2026-06-10 · 프로젝트 `innocean-perf-apac-kr` (asia-northeast3)
> **결론 먼저: 리포트/벤치마크는 `apac_kr_unified.v_perf_unified` 한 뷰에 붙으세요.** raw 직접 의존 비권장.

---

## 1. ★ 표준 소비 계약 — `apac_kr_unified.v_perf_unified`
**grain: 일별 × 플랫폼 × 광고주 × 캠페인.** 5개 매입 플랫폼 통합(중복 없음).
```
date DATE · platform STRING(meta|dv360|tiktok|google_ads|sa360)
advertiser_id STRING · advertiser_name STRING
campaign_id STRING · campaign_name STRING(일부 NULL)
impressions FLOAT64 · clicks FLOAT64 · conversions FLOAT64
spend_local FLOAT64 · currency STRING
spend_usd FLOAT64 · spend_krw FLOAT64   ← 환율 정규화(ECB 일별)
brand STRING(hyundai|kia|korean_air|hansem|naver|innocean_internal|other)  ← 이름/캠페인 파싱(휴리스틱)
market STRING(ISO2 국가코드: IN·BR·ES·SA·KR·GLOBAL…)  ← advertiser_dim 매핑
is_excluded BOOL  ← 테스트/미사용 계정 제외플래그(리포트는 WHERE NOT is_excluded 권장)
agency STRING(Innocean|Dplan|Dpurple)  ← 집행 회사(에이전시)
```
- **378 광고주 · ~20만 행.** 매일 자동 갱신. 컬럼 추가만(append-only) → 안 깨짐.
- **통화 정규화 완료**: `spend_usd`·`spend_krw` = `spend_local` × 일별환율(`fx_rates_daily`, ECB/frankfurter, 매일 자동갱신). 10통화(EUR·INR·PHP·BRL·THB·IDR·JPY·MYR·AUD·USD) 커버.
- **brand**: advertiser_name+campaign_name 정규식 도출(휴리스틱).
- **agency(집행회사)**: 우선순위 = 계정명 `디퍼플`→**Dpurple** / Google Ads `dplan360` MCC(7297527650) 하위계정(`apac_kr_raw.v_dplan360_advertisers`)→**Dplan** / 계정명 `디플랜`→Dplan / 나머지→**Innocean**. 국내는 대부분 Dplan(dplan360 MCC), 글로벌은 대부분 Innocean(현대·기아 해외).
- **market**: `apac_kr_raw.advertiser_dim`(platform·advertiser_id→market·is_excluded) 조인. 이름의 국가토큰(ISO α3/α2)→ISO2, 통화·한글명 보조. **비용기준 99.8% 매핑**. 지역통합계정=`GLOBAL`. 미상 1건(극소액).
- **is_excluded**: 현재 테스트/미사용 패턴(`dev`·`미사용`·`test`…)만 자동 플래그. 운영팀 확정목록 반영 가능.
- conversions: Meta는 현재 NULL(actions JSON 별도 파싱), 그 외 총전환.
- dedup 적용. **CM360은 의도적 미포함**(아래 3 — DV360/Ads와 노출 중복).

예) 브랜드별 월간 비용(KRW 정규화):
```sql
SELECT FORMAT_DATE('%Y-%m',date) m, brand, platform, ROUND(SUM(spend_krw)) krw
FROM `innocean-perf-apac-kr.apac_kr_unified.v_perf_unified`
WHERE date >= '2026-01-01' GROUP BY 1,2,3 ORDER BY 1,4 DESC
```
보조 참조뷰: `v_gads_customer`(Google Ads 광고주명·통화), `v_tiktok_advertiser`, `apac_kr_raw.fx_rates_daily`(환율).

**세그먼트 분할뷰 (Phase B, 2026-06-12, Google Ads + Meta·뷰only·기존무영향):**
- `v_perf_unified_device` — grain date×platform×campaign×**device**(MOBILE/DESKTOP/TABLET/CONNECTED_TV/OTHER). 소스 Google `p_ads_CampaignBasicStats_*.segments_device` + Meta `meta_device_daily.impression_device`(android_smartphone·iphone·ipod→MOBILE / android_tablet·ipad→TABLET / desktop→DESKTOP / 기타→OTHER). 합계=v_perf_unified 일치.
- `v_perf_unified_age` — **age_range**(18-24/25-34/35-44/45-54/55-64/65+/UNDETERMINED). 소스 Google `p_ads_AgeRangeBasicStats_*`(3,486만행, criterion_id 매핑) + Meta `meta_demo_daily.age`(gender 합산).
- `v_perf_unified_gender` — **gender**(MALE/FEMALE/UNDETERMINED). 소스 Google `p_ads_GenderBasicStats_*`(1,779만행) + Meta `meta_demo_daily.gender`(age 합산).
- 공통 컬럼 date·platform(google_ads|meta)·campaign_id·market·brand·{세그먼트}·impressions·clicks·spend_krw·conversions·is_excluded. (conversions: Meta는 NULL)
- ⚠️ **Google**: device는 총계와 일치(exhaustive), age/gender는 데모 보고분만이라 총계와 불일치(부분집합·데모 내 상대비교용). **Meta**: device·age·gender 전부 캠페인 총계와 일치(exhaustive, 데모 보고분 = 전체). DV360/TikTok 세그먼트는 미수집(필요시 추출기 확장).
- 수집: Meta 세그먼트는 `meta_extractor.py --segments`(데모 age,gender / impression_device 2패스, 캠페인레벨, 월청크·idempotent), 일일증분은 run_daily `meta_seg`. 12개월 백필 적재중.

## 2. 정제 단일플랫폼 뷰/테이블 (세부 분석용)
| 용도 | 위치 | grain |
|---|---|---|
| Meta 성과 | `apac_kr_raw.meta_insights_daily` | 일×광고(ad)별, `_brand`·`_account_id`·`_currency`, 전환=`actions`/`conversions`(JSON) |
| Meta 설정 | `meta_campaigns`/`meta_adsets`/`meta_ads` | 스냅샷 |
| DV360 성과 | `apac_kr_unified.v_dv360_performance`(dedup) | 일×라인아이템, `currency`, `total_conversions` |
| TikTok 성과 | `apac_kr_unified.v_tiktok_insights`(dedup) | 일×광고, `conversion` |
| TikTok 설정 | `tiktok_campaigns`/`adgroups`/`ads` | 스냅샷 |
| Google Ads | `p_ads_CampaignBasicStats_<MCC>` 등 수십종 | 일×캠페인, **내부 `customer_id`로 광고주 구분**, cost=`metrics_cost_micros`/1e6 |
| SA360 | `p_sa_CampaignStats_4885000456` 외 | 일×캠페인 |
| GA4 | `p_ga4_*` | GA4 표준 |

> ⚠️ **TikTok 성과는 `tiktok_insights_daily`** (설정테이블 `tiktok_ads`와 혼동 주의).
> ⚠️ **Google Ads는 테이블 접미사=MCC ID, 광고주는 테이블 내부 `customer_id`** (접미사 14개지만 광고주 226+).

## 3. CM360 — 별도 (합산 통합뷰 미포함)
CM360은 **광고서버**라 노출이 DV360·Google Ads와 **중복**됨 → 합산 시 이중계산. 그래서 `v_perf_unified`에 안 넣음.
- 일별 요약: `apac_kr_unified.v_cm360_daily` (date·advertiser_id·campaign_id·impressions·`dv360_sourced_imp`)
- 원본: `impression_<netID>`·`click_<netID>`·`activity_<netID>` (이벤트레벨), 날짜=`_DATA_DATE`
- 차원조인: `match_table_campaigns_<netID>`(Campaign_ID), `match_table_creatives_<netID>`(Creative_ID), `match_table_advertisers_<netID>`(Advertiser_ID)
- 현재 Global(464224)만, `_DATA_DATE` 2026-06-01~05(5일). 5권역·과거확장 진행 중.
- **권장: CM360은 전환(Floodlight activity)·크로스채널 어트리뷰션용으로만.** 노출/비용 합산엔 v_perf_unified 사용.

## 4. 커버리지 요약 (2026-06-10)
| 플랫폼 | 광고주 | 기간 | 비고 |
|---|---|---|---|
| Google Ads | 226+ (782 entity) | 2026-01~ (1년백필 진행) | MCC 4개 |
| DV360 | 118 | 2024-07~ | 완료 |
| Meta | 31 | 2023-06~(3년) | KIA 저예산 |
| TikTok | 2 | 2024-01~ | 집행계정만 |
| SA360 | 1 | 2026-04~(50일) | |
| CM360 | Global | 5일 | 5권역 대기 |

## 5. 운영 메모
- **신선도**: 매일 자동(03:00 KST + DTS). 실질 **D-2** (UTC계산+확정지연). 최신일 = `MAX(date)`.
- **중복**: raw에 DV360 846/TikTok 12 소량 중복 → **dedup 뷰 사용**(`v_dv360_performance`/`v_tiktok_insights`). Meta/GAds 중복 없음.
- **권한**: SA `perf-data-analyst`(BigQuery Admin) → 신규 테이블 자동 읽기.
- **안정성**: raw 테이블명 규칙 안정. 변경 영향 없는 **계약=`v_perf_unified`** 에 붙을 것.
- 미결(별도 결정/사내): 통화정규화·브랜드×국가 매핑·제외계정목록·CM360 과거백필·네이버/카카오(키대기)·Google Ads 7계정+정지2MCC(사내).

---
**핵심: `v_perf_unified` = 전 플랫폼 통합 계약. 세부는 정제 단일뷰. CM360은 별도(중복).**
