# 페이드 미디어 → BigQuery 수집 : 전체 현황 정리

> 기준일: 2026-06-09 · 프로젝트: `innocean-perf-apac-kr` (BQ `apac_kr_raw`, asia-northeast3)

## 1. 핵심 아키텍처 — 수집은 3가지 방식

| 방식 | 누가 돌림 | 플랫폼 | PC 의존 |
|------|----------|--------|---------|
| **A. BQ Data Transfer Service** (관리형) | Google | Google Ads, SA360, GA4, DV360 entity | ❌ 무관 |
| **B. 네이티브 BigQuery Exporter** | CM360(Google) | CM360 | ❌ 무관 |
| **C. 커스텀 추출기 + Cloud Run Job** | 우리(GCP) | Meta, DV360 성과, TikTok | ❌ 무관 |

- **B·C 모두 GCP 안에서 자동 실행** → PC를 꺼도/절전해도 수집 지속.
- C 방식 자동화: **Cloud Run Job `paid-media-daily-collector` + Cloud Scheduler 매일 03:00 KST**. 토큰은 Secret Manager. 추출기 `extractors/<platform>/`, 오케스트레이터 `run_daily.py`, 배포 `deploy/deploy.py`(gcloud 불필요).

## 2. 플랫폼별 현황

| 플랫폼 | 방식 | 수집 데이터 | 상태 |
|--------|------|------------|------|
| **Google Ads** | A (DTS, 매일 24h) | 설정+집행+전환 (11계정) | ✅ 4계정 정상 / ⚠️ 7계정 권한실패(5/26~ 구멍, 재인증 필요) |
| **SA360** | A | 설정+집행 | ✅ |
| **GA4** | A | 이벤트/속성 | ✅ |
| **DV360 (설정)** | A (displayvideo) | entity(파트너/IO/라인아이템 등) | ✅ |
| **DV360 (성과)** | C (Bid Manager API) | 노출·비용·전환 (9파트너·340광고주) | ✅ 추출기 작동, 클라우드 백필 진행 |
| **CM360** | B (네이티브 Exporter) | 노출·클릭·전환·활동 | ✅ Global 7,800만 노출 / 5개 권역 첫 익스포트 대기 |
| **Meta** | C (Marketing API) | 설정+집행+성과+전환 (KIA 20+현대 27계정) | ✅ 클라우드 백필+매일 |
| **TikTok** | C (Marketing API) | 설정+집행+성과+전환 (7 advertiser) | ✅ 추출기 작동, 클라우드 백필 진행 |
| 네이버 / 카카오 | C (예정) | — | ⏳ 키 발급 대기(관련 팀) |

## 3. 권역(멀티 계정) 커버리지
- **Meta**: KIA 해외 20개국 + 현대 27계정 (자동탐색)
- **CM360**: 7개 네트워크 (Global·India·AUNZ·Brasil·Indonesia·Spain + Havas는 권한없음 제외)
- **DV360**: 9파트너 340광고주 (현대·기아 전권역, 자동탐색)
- **TikTok**: 7 advertiser (현대 브라질/일본, KIA 등, 자동탐색)

## 4. 작업 방식 (어떻게 했나)
1. **현황 진단**: SA로 BQ `__TABLES__`/transfer config/실행로그 직접 조회 → 실제 적재 상태 파악(문서 아님)
2. **방식 선택**: 플랫폼별 최적 경로 — Google계열=관리형, CM360=네이티브 익스포터, 비구글=커스텀 추출기
3. **추출기 패턴 표준화**: API→GCS raw→BQ, 월/28일 청크, 스키마 고정(타입 안정), skip-loaded resume, 멱등 적재, 계정 자동탐색
4. **클라우드化**: 로컬 검증 → Cloud Run Job + Scheduler로 이전 (PC 독립)
5. **권한**: SA에 필요 역할 부여, 익스포터 SA·DTS 서비스에이전트 권한, DV360 사용자 등록 등

## 5. 미결 / 다음
- **Google Ads 7계정 재인증** (내일)
- **CM360 5개 권역** 첫 익스포트 확인 (~24h)
- **DV360 / TikTok 백필 완료** 확인
- **네이버·카카오** 키 수령 후 추출기 (TikTok 패턴 재사용)
- (후순위) 소비용 통합 뷰(`apac_kr_unified`) 플랫폼 확장, DV360 성과 entity 조인

## 6. 산출물 위치
- 추출기/배포: `DB_Management_system/extractors/`, `deploy/`
- 문서: `00_CURRENT_STATE_AND_PLAN.md`, `01_DV360_CM360_FINDINGS.md`, `02_NEXT_PLATFORMS_PREP.md`, `03_CM360_ROLLOUT.md`, 본 문서
- BQ: `innocean-perf-apac-kr.apac_kr_raw.*`
