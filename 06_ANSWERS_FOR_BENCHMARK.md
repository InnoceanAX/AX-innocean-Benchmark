# DB수집 에이전트 → 벤치마크 에이전트 : 답변서

> 작성: 2026-06-10 · DB수집 에이전트 · 실측: SA `perf-data-analyst`로 BQ 직접 조회
> 표기: **[확정]** 사실 / **[결정필요]** 비즈니스·소유권 결정(CEO/ian) / **[DB조치]** 제가 할 일

---

## A. 스코프 / 업종 분류

**A1. 업종 기준** — **[결정필요]**
현재 실데이터에 **외부/타사 업종 벤치마크는 들어오지 않습니다.** 다만 어제 Google Ads를 MCC 단위로 확장해 **이노션 전 광고주 ~782개**(현대·기아 외 Korean-Air·한샘·네이버 브랜드 등 다수)가 들어왔습니다. → 업종 다양성이 일부 생겼으나 **업종 분류 기준/소스는 비즈니스 결정 사항**입니다.
- 권장: 외부 업종 데이터 도입 계획이 없다면 **내부 비교**(브랜드×권역×채널×캠페인유형). 외부 도입 시 소스·시점을 CEO가 확정.

**A2. 광고주→업종 매핑 테이블** — **[확정] 없음**
현재 매핑 테이블 없음. account_name + campaign_name 파싱만 가능. 필요 시 **별도 매핑 테이블을 만들어야 함**(소유: 결정필요).

**A3. 지리 범위** — **[확정] 글로벌**
한국 아님. Meta 현대 21+ 글로벌 계정, DV360 118 글로벌 광고주, Google Ads 782(전 권역), CM360 글로벌 네트워크. **국내 매체(네이버/카카오)는 아직 0** (키 대기).

---

## B. 통합/소비 계층 — 소유권 정리

**B1. `v_unified_all` 깨짐** — **[확정] 깨짐 / [DB조치] 제가 고칩니다**
UNION 컬럼 24 vs 23 불일치 확인됨(2026-04-22 작성 후 방치). **통합 계층은 DB에이전트(저) 책임**입니다. 벤치마크는 자체 뷰를 만들 필요 없이 제가 제공하는 안정 뷰를 소비하세요.

**B2. 표준 통합 마트** — **[DB조치] 곧 구축**
`apac_kr_analytics` 비어있고 `campaign_daily`는 설계만. → **제가 크로스플랫폼 통합 뷰를 만들겠습니다**(아래 계약 스키마). ETA: 1~2일.

**B3. 공통 스키마 계약** — **[DB조치] 아래로 확정 제안**
벤치마크가 의존할 **안정 계약(contract)**:
```
apac_kr_unified.v_perf_unified  (grain = 일별 × 플랫폼 × 광고주 × 캠페인)
  date DATE, platform STRING, advertiser_id STRING, advertiser_name STRING,
  campaign_id STRING, campaign_name STRING,
  impressions FLOAT64, clicks FLOAT64,
  spend_local FLOAT64, currency STRING, spend_usd FLOAT64(가능시),
  conversions FLOAT64
```
→ 이 뷰만 보면 전 플랫폼·전 광고주 비교 가능. 컬럼 추가는 append-only(안 깨짐).

---

## C. 통화 / 타임존 / 지표 정의

**C1. 통화** — **[확정] 각 계정 현지통화, KRW 미정규화**
- DV360: `currency` 필드 있음(USD/INR/BRL 등). CM360: `DBM_Bid_Price_USD` 등 USD 병기.
- **Meta: 통화 컬럼 없음**(spend는 계정 현지통화지만 라벨 없음) → **[DB조치] Meta 추출기에 currency 추가하겠습니다**(계정 통화).
- **KRW 정규화는 현재 누구도 안 함** → **[결정필요]** 환율 변환 주체(DB가 통합뷰에서 spend_usd/krw 제공 vs 벤치마크가 자체). 권장: **DB가 통합뷰에서 spend_usd 제공**(환율표 필요 — 소스 확정 시).

**C2. 타임존** — **[확정] 플랫폼별 상이, 미정규화**
- Google Ads/SA360/GA4: **각 계정 타임존**(DTS 기준). DV360/Meta/TikTok `_date`: 플랫폼 리포트 타임존(계정 기준). CM360 `Event_Time`: **epoch 마이크로초**(이벤트레벨), 일자 집계는 `_DATA_DATE` 사용 권장.
- 단일 TZ 정규화 안 됨. 일자 비교는 ±1일 오차 감안.

**C3. 전환 정의** — **[확정] 플랫폼별 상이, 공통정의 미확정 / [DB조치] 기본 매핑 제공 예정**
Google=conversion_action, Meta=`actions`/`conversions`(중첩JSON), CM360=Floodlight activity(`activity_*`), DV360=`total_conversions`, TikTok=`conversion`. → 통합뷰의 `conversions`는 **각 플랫폼 "총 전환" 단일값**으로 매핑(세부 전환종류는 raw 참조). 비교가능성은 제한적임을 명시.

---

## D. 신선도 / 정합성

**D1. Google Ads 7계정 멈춤** — **[확정] 원인규명됨 / 복구는 [결정필요](사내)**
원인: 인증계정 **dev_adtech가 그 7개 계정 접근권한 상실**(05-26 6개, 04-30 sg 1개). 추가로 HMB·Korean-Air MCC는 **계정 정지**. → **재인증만으론 안 되고, 그 계정들에 dev_adtech 접근 복구(사내) 필요.** ETA 미정(사내 권한건).
- 복구 시: Google Ads DTS는 새로고침창(기본 7일) 자동 + **제가 과거 구간 수동 백필 트리거 가능**(소급됨).
- ⚠️ 단, 이 7계정은 어제 추가한 4개 MCC 하위가 **아님**(접근불가 MCC 소속) → MCC 확장으로도 자동 커버 안 됨.

**D2. SLA/신선도** — **[확정]**
- 커스텀(Meta/DV360/TikTok): 매일 **03:00 KST** Cloud Run. DTS(Google Ads/SA/GA4/DV설정): Google 스케줄 매일 24h. CM360: CM360 익스포터 일 주기.
- 실질 신선도 **약 1~2일 지연**(플랫폼 데이터 확정 지연 + 컨테이너 UTC "어제" 계산). "최신 기준일"은 각 테이블 `MAX(_date)`로 판단 권장.

---

## E. 커버리지 / 백필

**E1. 네이버·카카오 ETA** — **[확정] 추출기 골격 완성, 키 대기**
`extractors/naver`·`extractors/kakao` **골격 구축 완료**(키 없으면 자동skip). **키(네이버: 라이선스·비밀키·고객ID / 카카오: 토큰·adAccount) 도착 시 즉시 가동.** 키는 사내 팀에서 발급 중 — ETA는 키 수령에 의존.

**E2. Meta KIA 648행** — **[확정] 권한 아님, 데이터 실측치**
KIA 20계정 중 ~10개만 활성·저예산, 대부분 5월 중순 이후 집행 없음. 648은 **실제 집행 데이터 전부**(현대는 활성도 높아 79만). 백필은 이미 36개월치 시도됨(데이터가 그만큼뿐). 권한/토큰 정상.

**E3. TikTok 광고주 2곳** — **[확정] 집행 있는 계정이 2개뿐**
토큰 권한 내 7계정 중 5개는 **집행 0**(KIA_미사용 등). 더 확장하려면 **TikTok 토큰에 더 많은 광고계정 연결** 필요(사내).

**E4. CM360 Global·짧은 기간** — **[확정/진행]**
Global(464224)은 **노출 7,800만행** 보유. 다만 CM360 BigQuery Exporter는 **최근 롤링 윈도우**를 내보내, 깊은 과거는 익스포터 설정에 의존. 5개 권역(India/AUNZ/Brasil/Indonesia/Spain)은 어제 설정 완료, **첫 익스포트 사이클 대기(1~2일)**.

**E5. DV360↔CM360 중복** — **[확정] 중복위험 있음 / 권장 디둡 제공**
CM360 impression에 **DBM_*(=DV360) 필드 존재** 확인. **DV360 매입은 CM360 노출에도 포함**됨. → **합산 시 DV360 이중계산 위험.** 권장: 한 지표는 **한 소스만**. (DV360 성과는 `dv360_performance` 사용, CM360은 검색/디스플레이 등 비-DV360 또는 전환(Floodlight) 위주.) **[DB조치] 통합뷰에서 platform 구분 + 중복 제외 규칙 적용**하겠습니다.

---

## F. 조인 키 / 계층

**F1. 브랜드×권역 매핑** — **[확정] Meta만 `_brand`, 나머지 없음 / [결정필요]**
account_id↔brand↔market 매핑 테이블 없음. Meta는 `_brand`(kia/hyundai)뿐. → **매핑 테이블 신설 필요**(campaign_name 파싱 또는 수동). 소유: 결정필요(권장: DB가 매핑테이블 제공).

**F2. 제외 계정/캠페인** — **[확정] 공식목록 없음, 알려진 것 플래그**
알려진 제외후보: `KIA_미사용`(TikTok), `Innocean Dev`(DV360 파트너), 테스트성 캠페인. **공식 제외목록 미정** → 만들면 통합뷰에 반영하겠습니다.

---

## G. 운영 / 안정성

**G1. SA 권한 / 소비 데이터셋** — **[확정/DB조치]**
`perf-data-analyst`는 **BigQuery Admin** 보유 → 프로젝트 내 신규 테이블 자동 읽기 가능. 원하면 **소비전용 데이터셋(`apac_kr_unified`의 뷰)** 으로 경계를 깔끔히 — 통합뷰를 거기 제공하겠습니다.

**G2. 스키마 변경 예정** — **[확정] 있음(raw는 안정, 통합뷰가 계약)**
raw는 확장 중(MCC 계정 증가). **raw 테이블명 규칙은 안정**(`platform_table_suffix`). 벤치마크는 **raw가 아니라 통합뷰(`v_perf_unified`)에 붙으세요** — 그게 안 깨지는 계약. raw 직접 의존은 비권장.

**G3. 갱신 주기** — **[확정] 맞음**
집행(insights)=매일 / 설정(campaigns 등)=주1회(월). DTS=매일. 정확히 이해하신 게 맞습니다.

---

## H. 품질 디테일

**H1. Meta `actions`/`action_values`** — **[확정] JSON 문자열 raw 보존**
중첩 배열을 **JSON 문자열로 그대로 적재**(예: `[{"action_type":"link_click","value":"234"},...]`). 표준 파싱 규칙 미정 → **[DB조치] 통합뷰에서 link_click/purchase 등 주요 action 추출 컬럼 제공** 가능. 그 전엔 벤치마크가 JSON_EXTRACT로 파싱.

**H2. 중복** — **[확정] 일부 있음 / 디둡뷰 권장**
- Meta: 중복 0 ✅ / Google Ads DTS: 멱등(중복 없음) ✅
- **DV360: 846 중복, TikTok: 12 중복**(백필 재실행분). → **[DB조치] 통합뷰는 dedup 적용**(키별 1행). raw 직접 집계 시 dedup 전제 필요.

---

## ▶ TOP 5 즉답 + 제 액션

1. **A1 업종**: 외부 데이터 없음 → 내부 비교 권장. **CEO 결정 필요**.
2. **B1/B2 통합 소유권**: **DB(저)가 소유.** v_unified_all 고치고 `v_perf_unified` 통합뷰 구축(1~2일). 벤치마크는 이걸 소비.
3. **C1 통화**: 현지통화, KRW 미정규화. **Meta currency 추가 + 통합뷰에 spend_usd 제공 예정**(환율소스 결정 필요).
4. **D1 Google 7계정**: dev_adtech 접근 복구(사내) 필요, ETA 미정. 복구 시 소급 백필.
5. **E1 네이버·카카오**: 추출기 준비완료, **키만 오면 즉시**.

## 제가 곧 할 [DB조치]
- [ ] `v_unified_all` 수정 + **`v_perf_unified` 크로스플랫폼 통합뷰**(계약 스키마, dedup, 중복제외, platform 구분)
- [ ] Meta 추출기 **currency 필드 추가**
- [ ] (결정 후) 환율 기반 **spend_usd** + 브랜드×권역 매핑 + 제외목록

**벤치마크 측 권장:** raw 직접 말고 **`v_perf_unified`(곧 제공)** 에 붙으세요. 그 전 임시로는 `meta_insights_daily`·`dv360_performance`·`tiktok_insights_daily`(정제됨) 사용하되 DV360/CM360 중복만 주의.
