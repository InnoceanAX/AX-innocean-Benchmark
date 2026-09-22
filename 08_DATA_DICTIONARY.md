# 데이터 사전 — 소비자(벤치마크·리포트) 가이드

> 갱신: 2026-06-16 · 프로젝트 `innocean-perf-apac-kr` (asia-northeast3)
> **결론 먼저: 리포트/벤치마크는 `apac_kr_unified.v_perf_unified` 한 뷰에 붙으세요.** raw 직접 의존 비권장.
> **데이터 최신성: 매일 03:00 KST 자동수집, 실질 D-2(최신일 = `MAX(date)`).** 6개 매입 플랫폼 자동 갱신 중.

---

## 0. 데이터 최신성 한눈에 (★ 에이전트 참조용)
| 플랫폼 | 광고주 | 캠페인 | 수집기간 | 최신일 | 자동수집 |
|---|---:|---:|---|---|---|
| google_ads | 425 | 6,287 | 2025-06-01~ | 2026-06-15 (D-1) | ✅ 매일 |
| meta | 31 | 4,337 | 2023-06-24~ (3년) | 2026-06-14 (D-2) | ✅ 매일 |
| dv360 | 121 | 818 | 2024-07-01~ (2년) | 2026-06-14 (D-2) | ✅ 매일 |
| tiktok | 4 | 103 | 2024-01-01~ | 2026-06-14 (D-2) | ✅ 매일 |
| **kakao** | **5** | **48** | **2025-06-10~ (13개월)** | **2026-06-14 (D-2)** | ✅ **매일(신규)** |
| sa360 | 1 | 2 | 2026-04-21~2026-06-02 | (휴면) | ⚠️ 무집행 |

- **총 587 광고주 · ~48만 행** (`v_perf_unified` 전체). 매일 자동 갱신·append-only.
- 최신성 검증 쿼리: `SELECT platform, MAX(date) FROM apac_kr_unified.v_perf_unified GROUP BY 1`
- **신선도 D-2 정상**(플랫폼 확정지연+UTC). google_ads/세그먼트뷰는 D-1.
- 미수집(대기): **naver**(API 발급 심사 중), **CM360**(별도·아래 3). sa360은 소액·휴면.

---

## 1. ★ 표준 소비 계약 — `apac_kr_unified.v_perf_unified`
**grain: 일별 × 플랫폼 × 광고주 × 캠페인.** 6개 매입 플랫폼 통합(중복 없음).
```
date DATE · platform STRING(meta|dv360|tiktok|google_ads|kakao|sa360)
advertiser_id STRING · advertiser_name STRING
campaign_id STRING · campaign_name STRING(일부 NULL)
impressions FLOAT64 · clicks FLOAT64 · conversions FLOAT64
spend_local FLOAT64 · currency STRING
spend_usd FLOAT64 · spend_krw FLOAT64   ← 환율 정규화(ECB 일별)
revenue_local FLOAT64 · revenue_krw FLOAT64   ← 전환가치(ROAS용, spend와 동일 FX)
brand STRING(hyundai|kia|korean_air|hansem|naver|innocean_internal|other)  ← 이름/캠페인 파싱(휴리스틱)
market STRING(ISO2 국가코드: IN·BR·ES·SA·KR·GLOBAL…)  ← advertiser_dim 매핑
is_excluded BOOL  ← 테스트/미사용 계정 제외플래그(리포트는 WHERE NOT is_excluded 권장)
agency STRING(Innocean|Dplan|Dpurple)  ← 집행 회사(에이전시)
```
- **통화 정규화**: `spend_usd`·`spend_krw` = `spend_local` × 일별환율(`fx_rates_daily`, ECB/frankfurter). 10통화+KRW. 카카오는 원화(KRW, 환율 1).
- **revenue_krw(ROAS용)**: google/sa360=`conversions_value`, dv360=`revenue`, meta=`omni_purchase`(구매전환가치), **tiktok·kakao=NULL**(미추적/미수집). ROAS = revenue_krw / spend_krw. ⚠️ 광고주 설정 전환가치라 순매출과 다를 수 있음.
- **brand**: advertiser_name+campaign_name 정규식 도출(휴리스틱).
- **agency(집행회사)**: 우선순위 = 계정명 `디퍼플`→**Dpurple** / **platform=kakao→Dplan**(디플랜360 운영) / Google Ads `dplan360` MCC(7297527650) 하위계정→**Dplan** / 계정명 `디플랜`→Dplan / 나머지→**Innocean**.
- **market**: `advertiser_dim` 조인(비용기준 99.8% 매핑). 카카오 5계정은 **KR**(국내) 고정. 지역통합계정=`GLOBAL`.
- **conversions**: Meta·kakao는 NULL, 그 외 총전환. dedup 적용. **CM360은 의도적 미포함**(아래 3).

예) 브랜드별 월간 비용(KRW 정규화):
```sql
SELECT FORMAT_DATE('%Y-%m',date) m, brand, platform, ROUND(SUM(spend_krw)) krw
FROM `innocean-perf-apac-kr.apac_kr_unified.v_perf_unified`
WHERE date >= '2026-01-01' AND NOT is_excluded GROUP BY 1,2,3 ORDER BY 1,4 DESC
```
보조 참조뷰: `v_gads_customer`(Google Ads 광고주명·통화), `v_tiktok_advertiser`, `apac_kr_raw.fx_rates_daily`(환율).

**세그먼트 분할뷰 (Phase B, 뷰only·기존무영향. 카카오는 세그먼트 미수집=캠페인 BASIC만):**
- `v_perf_unified_device` — date×platform×campaign×**device**(MOBILE/DESKTOP/TABLET/CONNECTED_TV/OTHER). 소스 **Google+Meta+DV360**. 합계=v_perf_unified 일치(exhaustive).
- `v_perf_unified_age` — **age_range**(18-24/25-34/35-44/45-54/55-64/65+/55+/13-17/UNDETERMINED). 소스 **Google+Meta+TikTok**.
- `v_perf_unified_gender` — **gender**(MALE/FEMALE/UNDETERMINED). 소스 **Google+Meta+TikTok**.
- `v_perf_unified_video` — Google 영상캠페인. video_views·video_p25~p100·vtr·completion_rate·cpv_krw.
- 공통 컬럼 date·platform·campaign_id·market·brand·{세그먼트}·impressions·clicks·spend_krw·conversions·revenue_krw·is_excluded.
- ⚠️ 플랫폼 커버리지: **device=Google+Meta+DV360**, **age/gender=Google+Meta+TikTok**, video=Google. **카카오·sa360·DV360 데모는 미지원**. Google age/gender는 데모보고분(부분집합), Meta·TikTok는 전수(exhaustive).

**Meta 확장지표 (참여·영상, 2026-06-17):**
- `v_perf_unified_meta_ext` — date×campaign(meta). 컬럼 **link_clicks·video_3s_views·post_engagement·comment·reaction·lead·share** + impressions·clicks·spend_krw·market·brand·is_excluded. 소스 `meta_insights_daily`(actions JSON·inline_link_clicks). clicks/spend_krw=v_perf_unified(meta) 일치. (share=action_type `post` 추정값)
- 영상 ThruPlay/구간 VTR: `meta_insights_daily`에 `video_thruplay/p25/p50/p75/p100_watched_actions` 필드 추가 수집 시작(2026-06-17~), 적재 후 `v_perf_unified_video`에 Meta 합류 예정.

## 1.5 ★ 데이터 카탈로그 — `apac_kr_ops.v_data_catalog`
**전체 자산 인덱스.** 어떤 데이터가 어디에 있고 실데이터인지 한 뷰로 조회(에이전트 우선 참조).
```
dataset · table_name · kind(TABLE|VIEW) · platform · category · row_count · size_mb · is_live(행>0)
```
- platform 자동분류: google_ads·meta·tiktok·kakao·dv360·sa360·ga4·cm360·fx·mart_unified·reference.
- 예) 살아있는 Google Ads 성과테이블 찾기: `SELECT * FROM apac_kr_ops.v_data_catalog WHERE platform='google_ads' AND category='performance' AND is_live ORDER BY row_count DESC`
- 규모(2026-06): raw 1,495테이블(google_ads 27억행·cm360 3.2억·sa360 3천만), 소비마트 19+뷰.

## 1.6 Google Ads 세부 분석 마트 (raw 정제, 2026-06-17)
Google Ads의 풍부한 세부데이터를 통합뷰로 정제(MCC wildcard→광고주 매핑·KRW정규화·market). 공통컬럼 date·advertiser_id·advertiser_name·market·campaign_id·{차원}·impressions·clicks·spend_krw·conversions·conv_value_krw·is_excluded.
| 마트 | 차원 | 용도 |
|---|---|---|
| `v_gads_search_query` | search_term·match_type | 실제 검색어별 성과(SEM 분석) |
| `v_gads_keyword` | keyword_id | 키워드(criterion)별 성과 |
| `v_gads_hourly` | hour(0~23) | 시간대별 성과(요일/시간 최적화) |
| `v_gads_geo` | country_id·location | 지역별 성과 |
> 소스 `p_ads_{SearchQueryStats|KeywordBasicStats|HourlyCampaignStats|GeoStats}_*`. Google Ads 전용(다른 플랫폼 세부는 각 플랫폼 raw).

## 2. 정제 단일플랫폼 뷰/테이블 (세부 분석용)
| 용도 | 위치 | grain |
|---|---|---|
| Meta 성과 | `apac_kr_raw.meta_insights_daily` | 일×광고(ad)별, `_brand`·`_account_id`·`_currency`, 전환=`actions`(JSON) |
| DV360 성과 | `apac_kr_unified.v_dv360_performance`(dedup) | 일×라인아이템, `currency`, `total_conversions` |
| TikTok 성과 | `apac_kr_unified.v_tiktok_insights`(dedup) | 일×광고, `conversion` |
| **카카오 성과** | **`apac_kr_raw.kakao_moment_stats_daily`** | **일×캠페인, `_ad_account_id`·`campaign_name`, metrics=`impressions·clicks·cost(원)·ctr·video_play_3s·vtr`** |
| Google Ads | `p_ads_CampaignBasicStats_<MCC>` 등 | 일×캠페인, **내부 `customer_id`로 광고주 구분**, cost=`metrics_cost_micros`/1e6 |
| SA360 | `p_sa_CampaignStats_4885000456` | 일×캠페인 |
| GA4 | `p_ga4_*` | GA4 표준 |

> ⚠️ **TikTok 성과는 `tiktok_insights_daily`**(설정 `tiktok_ads`와 혼동 주의). **Google Ads는 테이블 접미사=MCC ID, 광고주는 내부 `customer_id`**.
> ⚠️ **카카오**: `apis.moment.kakao.com` 비즈니스 토큰(반영구). 5계정=현대(633718)·기아(867861)·소니(820118)·올리지오(862887)·삼양(763104). conversions/revenue 미수집. rate limit 초당1회.

## 3. CM360 — 별도 (합산 통합뷰 미포함)
CM360은 **광고서버**라 노출이 DV360·Google Ads와 **중복** → 합산 시 이중계산. `v_perf_unified`에 안 넣음.
- 일별 요약: `apac_kr_unified.v_cm360_daily`. 원본 `impression_<netID>` 등(이벤트레벨, `_DATA_DATE`).
- 현재 Global(464224)만, 5권역·과거확장 진행 중(외부 GMP 설정 대기).
- **권장: CM360은 전환(Floodlight)·크로스채널 어트리뷰션용. 노출/비용 합산엔 v_perf_unified.**

## 4. 마트 정리 노트 (중복/구버전 — 혼동 방지)
- **표준 통합 = `v_perf_unified`** (market·agency·revenue_krw 등 컬럼 풍부, 6플랫폼). ★이것을 쓰세요.
- `v_unified_all` — 구버전 통합(같은 6플랫폼이나 컬럼 적음). **deprecated, v_perf_unified 사용.**
- `v_unified_google_ads` — Google Ads 채널/애드그룹 레벨 별도뷰(주간 롤업 포함). 세부는 위 1.6 `v_gads_*` 권장.
- `v_unified_ga4` — **빈 뷰(0행)**. GA4는 실질 미운영(raw도 수백행 샘플뿐). 분석 비권장.
- `v_unified_sa360` — SA360(8행, 소액·휴면).
- **전환 마트**: 별도 없음. 캠페인 전환=`v_perf_unified.conversions/revenue_krw`, 전환액션 세부=각 `p_ads_*ConversionStats`/`meta_insights_daily.actions`, 크로스채널 어트리뷰션=CM360(아래 3).

## 5. 운영 메모
- **신선도**: 매일 03:00 KST 자동(Cloud Run Job + Scheduler, PC 무관). 실질 D-2.
- **중복**: raw DV360/TikTok 소량 중복 → dedup 뷰 사용. Meta/GAds/Kakao 중복 없음.
- **안정성**: 변경 영향 없는 **계약=`v_perf_unified`** 에 붙을 것(컬럼 추가만, append-only).
- **대기**: naver(API 심사·키), CM360 5권역(외부), Google Ads 일부 정지MCC(사내).
- 관제: 수집 현황·신선도·계보·자격증명 = DataMonitoring(`dataops-web`). 전체 자산 = `v_data_catalog`.

---
**핵심: `v_perf_unified` = 6개 플랫폼 통합 계약(587 광고주, D-2). 세부는 정제 단일뷰. CM360은 별도(중복).**
