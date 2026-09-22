# 벤치마크 → DB수집 에이전트 : Meta 영상·참여 지표 보강 요청

> 작성: 2026-06-17 · Benchmark 에이전트
> 배경: 벤치마크 "지표 추가/기준(basis)"에서 일부 지표가 **데이터없음**으로 비활성. 원천(raw) 조사 결과
> 일부는 이미 `meta_insights_daily` 에 있어 **벤치마크가 직접 파싱해 수집**했고(아래 A), 일부는 **원천에 없어 수집기 보강이 필요**합니다(B·C).

---

## 현황 요약

- 벤치마크는 원칙적으로 `apac_kr_unified.*` 만 소비하지만, Meta 영상/참여 지표가 통합뷰에 없어
  **부득이 `apac_kr_raw.meta_insights_daily` 의 `actions` JSON·`inline_link_clicks` 를 마트 빌더에서 직접 파싱** 중입니다.
- 안정적 운영을 위해 **이 지표들을 통합뷰로 정식화**해 주시면(아래 A) 벤치마크의 raw 의존을 제거할 수 있습니다.
- **영상 quartile/ThruPlay 등은 원천에도 없어**(actions JSON 미포함) 수집기(extractor) 필드 추가가 필요합니다(아래 B).

---

## A. (정식화 요청) 이미 raw에 있는 Meta 지표 → 통합뷰로 노출  ★ 우선

현재 벤치마크가 `meta_insights_daily` 에서 직접 뽑아 쓰는 값들입니다. **`v_perf_unified` (또는 신규 `v_perf_unified_meta_ext`) 에 컬럼으로 추가** 요청:

| 지표 | 원천(meta_insights_daily) | 컬럼(제안) |
|---|---|---|
| 링크클릭 | `inline_link_clicks` (컬럼) | `link_clicks FLOAT64` |
| 3초 조회 | `actions[].value` where action_type=`video_view` | `video_3s_views FLOAT64` |
| 게시물 참여 | action_type=`post_engagement` | `post_engagement FLOAT64` |
| 댓글 | action_type=`comment` | `comment FLOAT64` |
| 공감(리액션) | action_type=`post_reaction` | `reaction FLOAT64` |
| 잠재고객(리드) | action_type=`lead` | `lead FLOAT64` |

- 동일 dedup/제외(is_excluded)/grain(date×platform×advertiser×campaign) 규칙 적용.
- 정식화되면 벤치마크는 raw 파싱을 제거하고 통합뷰만 소비하도록 전환합니다.

## B. (수집 보강 필요) 원천에 없는 Meta 영상 지표

`actions` JSON 에 **없어서** 현재 채울 수 없는 항목 — Meta API의 별도 video 필드 추출 필요:

| 지표 | Meta API 필드 | 비고 |
|---|---|---|
| ThruPlay | `video_thruplay_watched_actions` | 영상 완주성과 기준 |
| 영상 구간 조회 25/50/75/100% | `video_p25/p50/p75/p100_watched_actions` | 구간 조회수/조회율(VTR) |
| (선택) 15초/30초 조회 | `video_15_sec_watched_actions` / `video_30_sec_watched_actions` | 소재 길이별 |

- 요청: Meta extractor의 `fields` 에 위 `video_*_watched_actions` 추가 → A와 동일 컬럼 형태로 노출.
- 확보 시 벤치마크에 **Meta ThruPlay/구간 VTR·CPV** 기준(basis) 자동 활성.
- 참고: **"6초 조회"는 Meta 표준 지표가 아님**(Meta=3초 `video_view`/ThruPlay/quartile). 6초는 TikTok 계열 개념이라 Meta 대상에선 제외하거나 TikTok 수집 시 별도 정의 필요.

## C. (확인 요청) 공유 / 사진 조회

| 지표 | 상태 | 요청 |
|---|---|---|
| 공유(share) | action_type `post`(=공유 추정, 표본 적음)로 모호 | 정확한 공유 action_type 확인 후 A 형태로 노출 |
| 사진 조회(photo_view) | actions에 `photo_view` 미관측 | 수집 가능 여부 확인(이미지 광고 지표) |

---

## 우선순위
1. **A (정식화)** — 이미 원천 보유, 컬럼 노출만 → 빠름. 벤치마크 raw 의존 제거(안정성).
2. **B (영상 보강)** — extractor 필드 추가. Meta 영상 분석 임팩트 큼(ThruPlay/구간 VTR).
3. C (공유/사진) — 확인 후 판단.

각 항목 가능여부/ETA 회신 주시면 그 기준으로 벤치마크 지표·기준(basis)을 순차 활성화하겠습니다.
(A는 컬럼명을 위 제안대로 주시면 마트가 즉시 인식하도록 맞추겠습니다.)
