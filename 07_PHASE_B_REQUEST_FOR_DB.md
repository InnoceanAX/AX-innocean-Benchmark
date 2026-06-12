# 벤치마크 → DB수집 에이전트 : Phase B 데이터 확장 요청

> 작성: 2026-06-12 · Benchmark 에이전트 · 기준 커밋 Phase A(다차원 벤치마크) 라이브
> 배경: 기능정의서(Sohee 최종)는 **매체·업종·국가 + 캠페인유형·타겟·디바이스** 다차원 세분화를 의도.
> Phase A(권역·캠페인목표·브랜드·업종)는 `v_perf_unified`만으로 구현 완료. Phase B(타겟·디바이스)와
> 아래 데이터 보강이 있어야 스펙 완성.

---

## P0 — google_ads `campaign_name` NULL  → ✅ 벤치마크 측에서 자동 해결 (DB는 선택)

`v_perf_unified` 에서 platform='google_ads' 행의 campaign_name 이 전부 NULL.
- **해결됨**: 마트 빌드에서 raw `ads_Campaign_<계정>`(14테이블·100% 커버)로 campaign_id→name 보강.
  → Google 목표/업종 분류 정상화(영상조회/검색/퍼포먼스/브랜딩/트래픽). (커밋 464ad00)
- (선택) DB가 v_perf_unified 의 google campaign_name 을 원천에서 채워주면 마트 보강 로직 제거 가능(더 깔끔).
  급하지 않음 — 현재 자체 해결됨.

---

## P1 — 디바이스(device) 세그먼트  ★ 이번 요청 핵심

- 실무자 필수 차원(모바일/데스크톱/태블릿/CTV별 벤치마크).
- 현재: raw `ads_*Stats.segments_device` 에 존재(MOBILE/DESKTOP/TABLET/CONNECTED_TV/OTHER) 하나 **통합뷰엔 없음**.

**요청(정확 스펙): 디바이스 분할 뷰 `apac_kr_unified.v_perf_unified_device` 신설**
```
date DATE, platform STRING, campaign_id STRING, market STRING, brand STRING,
device STRING (MOBILE/DESKTOP/TABLET/CONNECTED_TV/OTHER),
impressions FLOAT64, clicks FLOAT64, spend_krw FLOAT64, conversions FLOAT64,
is_excluded BOOL
```
- grain = date × platform × campaign × **device** (캠페인 grain에 device만 추가).
- 합계가 기존 v_perf_unified 와 일치해야 함(같은 정규화/제외 규칙 적용).
- Google: raw `ads_CampaignStats_*.segments_device` 사용(검증됨, 데이터 있음).
  Meta/DV360/TikTok: 각 raw에 device breakdown 있으면 포함, 없으면 그 플랫폼은 제외(부분이라도 OK).
- 이 뷰가 생기면 벤치마크 마트가 device 차원을 **자동 추가**합니다(코드 1곳만 연결).

> 대안(빠른 임시): v_perf_unified 자체에 device 컬럼 추가 + grain에 device 포함도 가능.
> 단 기존 캠페인 grain 소비처가 영향받을 수 있어 **별도 뷰**를 권장.

---

## P2 — 타겟 (연령/성별)

- 현재: Google `ads_AgeRangeStats`/`ads_GenderStats` **테이블은 있으나 0행**(수집 안 됨).
  → Google Ads Data Transfer 설정에서 **Age/Gender 리포트 활성화** 필요할 수 있음.
- Meta는 insights에 age/gender breakdown 옵션 존재(현재 미수집).
- **요청: 연령/성별 세그먼트 수집 가능 여부 확인 + 가능 시 통합뷰(또는 세그먼트 뷰)에 추가.**
  불가하면 "현재 미수집"으로 회신 주시면 UI에서 해당 차원 숨김 처리하겠습니다.

---

## P3 — 지표 보강 (선택)

- **revenue/매출**: ROAS 지표용. v_perf_unified 에 revenue 컬럼 없음(google_ads엔 raw에 conversions_value 존재).
- **영상 지표(VTR/조회수)**: 영상 캠페인 벤치마크용. raw 영상 스탯(video_quartile 등)에 존재.
- 우선순위 낮음 — P0~P2 이후.

---

## 정리 (우선순위)
1. **P0 google_ads campaign_name** ← 가장 임팩트 큼, 매핑만 연결
2. **P1 device** ← 실무자 필수
3. **P2 age/gender** ← 수집 가능 여부 확인
4. P3 revenue/영상 ← 나중

각 항목 가능여부/ETA 회신 주시면 그 기준으로 UI 차원/필터를 확장하겠습니다.
(벤치마크는 `v_perf_unified` 만 소비 → 통합뷰에 추가되면 마트 빌드에서 자동 반영)
