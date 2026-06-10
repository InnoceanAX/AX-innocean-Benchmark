# Benchmark 백엔드 + 전용 데이터 마트

기존 프론트(`../index.html`, 라이브 block0)의 API 계약(`/api/v1/*`)을 그대로 구현하고,
**벤치마크 전용 데이터 마트**만 소비한다. 로우데이터는 절대 직접 읽지 않는다.

## 데이터 흐름 (전용 마트 패턴)

```
apac_kr_raw / apac_kr_unified   ← DB에이전트 소유, READ-ONLY (SELECT만)
        │   mart.py (스케줄 배치, SELECT-only)
        ▼   ── 쓰기는 오직 여기로 ──
apac_kr_benchmark               ← 벤치마크 전용 마트 (이 솔루션만 사용)
   ├─ bm_fact_monthly           : period(YYYY-MM) × media(G/M/N/K) × industry 사전집계
   └─ bm_advertiser_industry    : 업종 매핑 시드 (F1 결정이 채울 자리)
        │   bq.py (이 마트만 읽음)
        ▼
FastAPI (main.py)  ──  index.html 서빙 + /api/v1/benchmark, /api/v1/ai/chat
```

- **로우 미접근 원칙**: `bq.py` 는 `apac_kr_benchmark` 외 어떤 데이터셋도 쿼리하지 않는다.
- **업스트림 교체 지점**: DB의 `v_perf_unified` 제공 시 `mart.py` 의 `UPSTREAM` 만 바꾸면 됨.

## 파일
| 파일 | 역할 |
|------|------|
| `main.py` | FastAPI 앱. index.html 서빙 + API 2종 |
| `bq.py` | 마트 → 프론트 모양(summary/detail) 변환. **마트만 읽음** |
| `mart.py` | 전용 마트 생성/갱신 (raw→benchmark, SELECT-only 쓰기는 마트만) |
| `industry_map.py` | 광고주/캠페인 → 업종 매핑 규칙 (F1 비즈니스 결정 자리) |
| `ai.py` | AI 분석 답변 (Gemini, 마트 요약 근거. 키/모델 교체 가능) |

## 로컬 실행
```bash
# 1) 마트 생성/갱신 (최초 1회 + 매일 배치)
cd backend && python mart.py          # python mart.py --check 로 확인

# 2) API 서버
PORT=8080 python main.py
#   GET  /api/v1/benchmark?media=G&date_from=2026-01-01&date_to=2026-06-08
#   POST /api/v1/ai/chat  {"message":"...","media":"G"}
```
인증: 로컬은 `setup/innocean-perf-apac-kr-*.json` 자동 탐색. Cloud Run 은 SA ADC.

## 배포 (Cloud Run, 단일 컨테이너)
루트 `Dockerfile`(python/uvicorn) 사용. `Dockerfile.static` 은 백엔드 도입 전 nginx 보존본.
```bash
gcloud run deploy innocean-benchmark --source . --region asia-northeast3 \
  --allow-unauthenticated --port 8080 \
  --set-secrets GEMINI_API_KEY=benchmark-gemini-key:latest
```
- SA: Cloud Run 서비스에 BigQuery 접근 SA 연결(키파일 미배포).
- `GEMINI_API_KEY`: Secret Manager 주입(컨테이너에 키파일 없음).
- 마트 갱신: Cloud Scheduler → `python mart.py`(Cloud Run Job) 매일.

## 현재 상태 / TODO
- [x] 전용 마트(G·Meta) 생성, bq.py 마트 전용 소비, AI(Gemini) 연동 — 로컬 검증 완료
- [ ] index.html 목업 함수 → fetch(API) 연동 (소스 구조 유지하며 데이터 출처만 교체)
- [ ] `v_perf_unified` 수령 시 mart.py UPSTREAM 교체(중복/통화 정합)
- [ ] 네이버·카카오 수집되면 mart UPSTREAM 에 추가
- [ ] Cloud Run 배포 (gcloud SDK 필요)
