# Changelog

## 2026-06-08 18:50 — 룰/기능 명세서 추가

- `docs/ARCHITECTURE.md` 작성 (절대 규칙, 디자인 토큰, 폴더 구조)
- `docs/FEATURES.md` 작성 (페이지별 기능, AI 채팅 패턴, Phase 2 예정)
- README 보강 (문서 링크 추가)

# Changelog (previous)

원칙: 사용자(CEO)에게 영향이 있는 변경만 기재. 코드 정리/리팩터는 git 커밋 메시지로만.

## 2026-06-08
- **관련 질문 영역 제거** — AI 채팅 답변 후 표시되던 "관련 질문" 영역을 비활성화. 사이드바 하단의 추천 질문 chips는 유지.
- **Dockerfile + nginx.conf 정식 추가** — Cloud Run 직접 배포 가능. Cache-Control: no-store 명시.
- **AI 채팅 chip 인터랙션 개선** — 그라데이션 chip 디자인, 클릭 시 자동 발송, 추천 영역 접기/펴기 토글.

## 이전 작업
- AI 채팅 사이드바 + Summary 차트 + 업종별 벤치마크 비교 테이블 구조 정착.
- CSS 변수 기반 디자인 시스템 적용 (`--ind`, `--bdr` 등).

---
새 항목은 위쪽에 추가. 날짜 + 사용자 가시 변경 + 짧은 요약.
