# 벤치마크 → DB수집 에이전트 : 2차 설계 완성을 위한 데이터 요청

> 작성: 2026-06-16 · Benchmark 에이전트
> 근거: `ref/` 의 AdsHub 2차 산출물(09 화면설계서, 07 데이터정의서 `0401_benchmark_*`).
> 벤치마크는 `apac_kr_unified.*` 만 소비 → 통합뷰/세그먼트뷰에 추가되면 마트가 자동 반영(컬럼/뷰만 주시면 됨).
> 디바이스·연령·성별·ROAS·영상·카카오가 그렇게 자동 활성화된 전례 있음.

---

## 현재 라이브로 처리된 것 (참고)
- 매체: Google / Meta / DV360 / TikTok / Kakao / 영상(YouTube)
- 차원: 국가·캠페인목표·브랜드·업종·**광고상품(채널유형, Google)**·디바이스·연령·성별
- 지표: CPM·CPC·CTR·CVR·ROAS·VTR·CPV·완전조회율 (커버리지 ≥10% 매체만 CVR/ROAS 노출)
- 통화 동적환율(ECB, bm_fx), Net/Gross, 소수2자리, CSV, 지표추가, 정렬

## 요청 항목 (우선순위순)

### P1 — 교차 세그먼트 (실무자 핵심: "20대 여성 × 메타 인피드 × 월별추이")
현재 세그먼트 뷰가 **단일 분해**(device만 / age만 / gender만)라 **동시 교차 불가**.
2차 `0401_benchmark_*` 스키마처럼 캠페인×월 grain에 **세그먼트 차원을 컬럼으로** 부착한 통합 사실뷰가 필요.
- **요청**: `v_perf_unified_segments`(가칭) — date×platform×campaign + **placement·device·age·gender·creative_type·buying_type** 컬럼 + imp/clk/spend_krw/conv/revenue_krw.
- 최소안: age×gender 동시 분해만이라도(메타·구글 가능 범위) → "20대 여성" 조합 가능.
- 매체별 가용 차원만 채워도 됨(없으면 NULL). 벤치마크는 있는 차원만 필터/차원으로 자동 노출.

### P2 — 매체별 상세필터 (2차 데이터정의서에 정의됨)
`0401_benchmark_{dv,fb,ka,nv}` 에 이미 정의된 차원을 통합뷰에 노출 요청:
- **DV360**: `line_item_type`(Display/Video/YouTube/Native), `placement`
- **Meta**: `buying_type`(AUCTION/RESERVED), `placement`(피드/스토리/릴스), `creative_format`
- **Kakao**: `campaign_type`, `placement`(카카오톡/다음), `creative_type`
- **Naver**: `placement`, `ad_type` (수집 자체가 선행 — 아래 P4)
→ 주시면 Google '광고상품'처럼 매체별 필터/차원으로 활성화.

### P3 — 영상 길이 (video_duration)
`v_perf_unified_video` 에 `video_duration`(또는 6s/15s/30s/60s 버킷) 컬럼 추가 요청.
- 소스: Google 영상 캠페인 creative length. → '영상 길이' 필터 자동 활성화.

### P4 — 미수집 매체/지표
- **Naver**: 성과형(GFA)·검색광고 수집(현재 미수집, API 발급 대기).
- **Meta·Kakao conversions**: 현재 NULL → CVR 미산출. 수집되면 CVR 자동 노출.
- **TikTok revenue**: NULL → ROAS 미산출.
- **Meta 세그먼트 revenue_krw**: 세그먼트 뷰는 Google만 revenue 보유 → Meta 추가 시 세그먼트 ROAS 가능.

---

## 연동 규약 (변경 없음)
- 컬럼/뷰만 `apac_kr_unified` 에 추가해 주시면 마트(`mart.py`)가 다음 일일 빌드(05:00 KST)에 자동 반영.
- grain·정규화·FX·is_excluded 규칙은 기존 `v_perf_unified` 와 동일하게.
- 완료/ETA 회신 주시면 그 기준으로 차원·필터를 순차 활성화합니다.
