# INNOCEAN Benchmark

업종별 광고 벤치마크 데이터 조회 + AI 분석 채팅 + Summary 차트 솔루션.

🔗 **Production:** https://innocean-benchmark-291757702623.asia-northeast3.run.app

---

## 빠른 시작 (인수인계용)

### 1. 로컬에서 보기

별도 빌드 도구 없음 — 그냥 `index.html`을 브라우저로 열면 됩니다.

```bash
git clone https://github.com/InnoceanAX/AX-innocean-Benchmark.git
cd AX-innocean-Benchmark
open index.html        # macOS
# 또는 python3 -m http.server 8000 후 http://localhost:8000
```

### 2. Cloud Run 배포

GCP 프로젝트: `innocean-perf-apac-kr` (291757702623), 리전: `asia-northeast3`

```bash
gcloud run deploy innocean-benchmark \
  --source . \
  --region asia-northeast3 \
  --allow-unauthenticated \
  --port 8080 \
  --quiet
```

배포는 `Dockerfile`(nginx:alpine) + `nginx.conf`(port 8080, Cache-Control: no-store)로 처리됩니다.

### 3. 배포 후 검증

```bash
URL="https://innocean-benchmark-291757702623.asia-northeast3.run.app"
curl -sI "$URL/" | grep -i "cache-control\|content-length"
# cache-control: no-store 확인 필수
```

---

## 아키텍처 원칙 (절대 규칙)

- **단일 HTML 파일** (`index.html`) — 모든 CSS/JS 인라인
- 외부 의존: **Chart.js + Pretendard 폰트 CDN만** 허용. 다른 프레임워크/라이브러리 추가 금지
- **vanilla JS** — React/Vue/jQuery 등 사용 금지
- **rounded-none** — `border-radius: 0` (단, AI sidebar의 chip은 예외적으로 둥근 형태 사용)
- CSS 변수:
  - `--ind: #4F46E5` (키컬러)
  - `--ind2: #4338CA`
  - `--bdr: #E5E7EB`
  - `--ts: #555` / `--tm: #767676` (텍스트 secondary/muted)
- 디자인 기준은 INNOCEAN Brand Safety의 DOM 스펙. `adshub` 디자인 직접 사용 금지

## 작업 흐름

1. 로컬에서 `index.html` 수정 (수동 또는 Python 스크립트 + assert 기반 텍스트 치환)
2. 따옴표 이스케이프 보존 검증 (특히 `onclick` 내 `'`)
3. `git add . && git commit -m "..."` → `git push origin main`
4. Cloud Run 배포 (위 명령)
5. 서버에서 `curl` 응답으로 키워드 검증
6. CEO에게 URL + 검증 결과 보고

## CEO 1:1 채널
- 모든 피드백·승인은 채팅 채널을 통해 전달됨
- 변경 후 보고 시: URL + 변경 요약 + ALL PASSED + Ctrl+Shift+R 안내 필수

## 알아두기
- `Cache-Control: no-store` (nginx.conf) — CEO가 매번 강력 새로고침 없이 변경을 볼 수 있도록 항상 유지
- Docker 빌드 시 `--no-cache` 권장 (이전 이미지 캐시 방지)
- 단일 page application — `goPg('L'|'A'|'BM')`으로 페이지 전환 (history.pushState 기반)
- AI 채팅은 사이드바 패턴 (`.aisb`, `.sbinn`), 추천 질문은 `sqchips` 영역으로 표시

## 라이선스 / 운영
- INNOCEAN 내부 솔루션. 외부 공개 금지
- 배포 권한: GCP SA `perf-data-analyst@innocean-perf-apac-kr.iam.gserviceaccount.com`
