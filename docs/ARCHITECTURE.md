# ARCHITECTURE — INNOCEAN Benchmark

## 절대 규칙 (변경 불가)

1. **단일 HTML 파일** — `index.html` 안에 모든 CSS / JS 인라인.
2. **외부 의존**: Chart.js + Pretendard 폰트 CDN **만** 허용.
3. **vanilla JS** — 프레임워크 / 라이브러리 추가 금지.
4. **rounded-none** (chip / pill 예외).
5. **Cache-Control: no-store** — nginx 강제.
6. **Brand Safety DOM** 기준.
7. **adshub 디자인 절대 금지**.
8. **이모지 사용 금지** — 모든 아이콘 inline SVG.

## 디자인 토큰

```css
:root{
  --ind:#4F46E5; --ind2:#4338CA;
  --bg:#FAFAFA; --bdr:#E5E7EB;
  --ts:#555; --tm:#767676;

  --hh:70px; --sh:40px; --fh:80px;
}
body.dark{
  --bg:#0F172A; --text:#F9FAFB;
  --card-bg:#1E293B; --border-c:#374151;
}
```

## Chrome

| 영역 | 위치 | 높이 | 내용 |
|---|---|---|---|
| Header | fixed top | 70px | INNOCEAN 로고 + 세로바 4×32px + `BENCHMARK` 라벨 + nav |
| Subheader | fixed top+70 | 40px | 좌 `INNOCEAN AI SOLUTION` / 우 `Vol. 2026` |
| Footer | bottom | 80px | 좌 `\| INNOCEAN` / 우 copyright |

## 카드 / 버튼

```css
.card{ border:2px solid var(--bdr); border-radius:0; }
.card:hover{ border-color:#000; }

.btn-pri{ background:#000; color:#fff; border-radius:0; }
.btn-sec{ background:#fff; color:#000; border:1px solid #000; }
```

## SVG 아이콘

- viewBox `0 0 24 24`, `fill:none`, `stroke:currentColor`, `stroke-width:1.8`.
- `.ico` 기본 크기 / `.ico-sm` 14px.

## Chart.js 4.4

- 색 팔레트: `var(--ind)`, `#1F2937`, `#9CA3AF`.
- `responsive:true`, `maintainAspectRatio:false`.

## 폴더 구조

```
AX-innocean-Benchmark/
├── index.html
├── Dockerfile
├── nginx.conf
├── README.md
├── CHANGELOG.md
└── docs/
    ├── ARCHITECTURE.md
    └── FEATURES.md
```

## 배포

```bash
gcloud run deploy innocean-benchmark --source . --region asia-northeast3 --allow-unauthenticated --port 8080 --quiet
```
