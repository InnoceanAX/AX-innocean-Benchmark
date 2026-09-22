# -*- coding: utf-8 -*-
"""자동 데이터-갭 요청기 — 벤치마크가 '값이 비어있는 지표'를 감지해 DB 에이전트 요청 큐로 발행.

요청자(requested_by) 명시로 **에이전트 간 요청 충돌/중복 방지**:
  dedupe_key = requested_by|platform|metric  (예: benchmark|meta|revenue)
  → 같은 에이전트의 같은 요청은 한 행으로 유지(중복 누적 X), 타 에이전트(report 등)의 요청은 별개 행.
  → DB 에이전트는 (platform,metric)로 실제 작업을 묶고, requested_by로 '누가 필요로 하는지' 추적.

큐: `apac_kr_ops.agent_data_requests` (DB 에이전트가 모니터링하는 ops 레이어).
흐름: 벤치마크 감지→open 발행 → DB가 연동플랫폼·수집데이터 검토해 채움→dictionary_version↑ → 벤치마크 자동반영,
      해소된 갭은 다음 스캔에서 status='fulfilled' 자동 처리. DB가 'unavailable'로 닫은 건 재요청 안 함(결정 존중).
실행: mart.build() 끝에서 매일 자동 호출 + `python gaps.py` 수동.
"""
from mart import _client, PROJECT, MART_DS

AGENT_ID = "benchmark"
REQ_TBL = f"`{PROJECT}.apac_kr_ops.agent_data_requests`"
MART = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
MEDIA = {"google_ads": "G", "meta": "M", "dv360": "D", "tiktok": "T", "kakao": "K", "naver": "N"}
COVER_MIN = 0.02   # 커버리지 2% 미만이면 '값 없음(갭)'으로 간주

# 벤치마크가 원하는 지표 레지스트리 — (platform, metric, label, mart_col). col=None=마트에 컬럼 자체가 없음(미수집).
WANTED = [
    ("meta",    "conversions", "전환수·CVR (전환 추적)",        "conv"),
    ("meta",    "revenue",     "전환가치·ROAS",                 "rev"),
    ("meta",    "video_6s",    "6초 조회 (영상)",               None),
    ("meta",    "photo_view",  "사진 조회",                     None),
    ("kakao",   "conversions", "전환수·CVR",                    "conv"),
    ("kakao",   "revenue",     "전환가치·ROAS",                 "rev"),
    ("kakao",   "video",       "영상지표(조회/VTR/CPV)",        "vimp"),
    ("tiktok",  "revenue",     "전환가치·ROAS",                 "rev"),
    ("dv360",   "video",       "영상지표(조회/VTR/CPV)",        "vimp"),
    ("naver",   "all",         "네이버 전체(노출·클릭·비용) 수집", "imp"),   # 2026-06-19 합류 → 자동 fulfilled
]

# ── 자유질의 레지스트리 — 갭(값없음)이 아니라 '판단/설계 확인'이 필요한 건 ────────────
# 같은 큐(agent_data_requests)를 쓰되 metric 을 질문 키로 삼는다. status='open' 으로 올리면
# DB 에이전트가 폴링해 db_response 에 답을 적고 닫는다. 답이 오면 ask_db() 가 콘솔에 출력.
# (갭 스캔과 달리 자동 해소 판정이 없다 — 사람/DB의 '판단'을 받는 것이 목적이므로 DB가 직접 닫는다.)
ASKS = [
    ("dplan", "view_contract", "v_dplan_benchmark 를 벤치마크가 정식 소비해도 되는지", """\
벤치마크는 원칙적으로 apac_kr_unified.v_perf_unified 만 소비합니다. 디플랜 뷰는 별도 계열인데,
디플랜과 논의된 대시보드 개편 요청(넷플릭스·티빙·토스 매체 탭 추가)을 받아 v_dplan_benchmark 소비가 필요해졌습니다.
Q) (1) 사전(v_data_dictionary / dictionary_marts)에 등재된 정식 소비 대상입니까?
   (2) 갱신 주기는? NAS 엑셀 적재라 수동·비정기면 화면에 '최종 갱신일'을 노출해야 합니다.
   (3) 스키마 append-only 보장됩니까?
※ 14_STALE_MARTS 때 세우신 '사전 등재 = 사용허가 신호' 원칙을 따르려고 확인 없이 쓰지 않았습니다."""),

    ("dplan", "spend_basis", "NAS 광고비 금액기준 — 같은 표에서 CPM·CPC 비교 가능 여부", """\
[가장 급함 — 화면 구조를 좌우합니다]
v_dplan_benchmark.spend_basis 가 3,414행 전량 '미확인 - 순매체비/총액 표기 없음(2026-09-14)' 단일값입니다.
한편 v_dplan_api_reconcile 의 NAS÷API spend 비율은 YT 1.000 / 네이버 1.000 / Google 1.375(중앙) 로,
YT·네이버는 순매체비, Google 은 NAS 가 총액(마크업 포함)으로 보입니다.
문제: 넷플릭스·티빙·토스·SMR·COVI 는 API 수집이 없어 reconcile 로 교차검증이 불가한데, 하필 이들이 새로 노출할 매체입니다.
Q) 전량 순매체비로 확정 가능합니까? 아니면 섞여 있습니까?
   - 순매체비 확정 → 기존 API 매체와 같은 표에서 CPM·CPC 비교 허용
   - 혼재 → NAS-only 매체를 별도 섹션 분리 + '금액기준 미확인' 배지
※ 회신 전까지는 후자(분리+배지)로 구현합니다. 틀린 CPM 비교를 노출하는 것보다 안전하다고 판단했습니다.
※ 디플랜에 기준 확인 중이라 하셨는데 진행 상황도 알려주시면 좋겠습니다."""),

    ("dplan", "creative_grain", "소재 grain 뷰(v_dplan_creative) 노출 요청", """\
PPT 가 요구하는 소재 단위 나열 표(NO#/국가/업종/집행월/매체명/상품명/노출/클릭/조회/CTR/VTR/CPM/CPC/CPV/CVR/
디바이스/소재유형/소재초수/가로·세로/전환목표)의 컬럼이 apac_kr_raw.dplan_archive_rows(200,576행)에 거의 전부 있습니다.
  nc_country 79.2% · industry 79.2% · nc_month 79.2% · media 100% · product 100% · nc_device 79.2% · nc_conv 79.2%
  nc_creative 79.2%  ← '비디오_15s_가로형_홍태준' 처럼 소재유형·초수·가로세로 3개가 한 컬럼에 붙어 있음
  conversions 16.8%  ← 구 양식 일부만. 커버리지 게이트로 CVR 은 숨길 예정
Q) apac_kr_unified.v_dplan_creative (가칭) 를 소재 grain 으로 노출해 주실 수 있습니까?
   [2026-09-22 정정] DB 회신에서 '소재유형·가로/세로는 원천에 없음'이라 하셨으나, creative_element(0.3%)가 아니라
   nc_creative 를 보시면 있습니다. 200,576행 전수 측정:
     creative_format  REGEXP_EXTRACT(nc_creative, r'^(비디오|이미지|영상|동영상|텍스트|GIF)')      76.9% (+category 보완 79.2%)
     creative_ratio   REGEXP_EXTRACT(nc_creative, r'(가로형|세로형|정방형)')                     66.1%
     creative_sec     COALESCE(REGEXP_EXTRACT(nc_creative, r'_(\\d+)s'),
                               REGEXP_EXTRACT(creative_name||' '||campaign_name, r'(\\d+)\\s*초'))  70.5%
   가로/세로 분포: 가로형 74,928 · 정방형 33,268 · 세로형 24,474 · 없음 26,243.
   초수는 '_15s' 패턴과 'N초' 패턴이 서로 다른 행을 덮어 합치면 62.0% → 70.5%.
   파싱 실패분은 누락이 아니라 '해당 없음'입니다(반응형 검색광고 4,739 · 이미지_클렌즈 3,065 등) → 화면에서 '—'.
   creative_format 은 category(이미지/영상/검색) 와 라벨 체계가 '비디오' vs '영상'으로 달라 정규화가 필요합니다.
   부담스러우시면 Meta ext 때처럼(커밋 8966458) 벤치마크 마트 빌더에서 raw 파싱 → 나중에 정식 뷰 전환도 좋습니다."""),

    ("netflix", "reach_curve_api", "넷플릭스 Reach Curve API 수집 주체 확인", """\
넷플릭스가 2026 upfront 에서 Reach Curve API / Audience Insights API(Netflix Ads Suite Planning APIs)를 공개했고,
이노션 전용 토큰을 추후 발급받을 예정입니다(사용자 확인). PPT 요구사항 (c) '도달 시뮬레이터' 메뉴의 전제입니다.
벤치마크는 그때까지 자체 도달 추정 모델(노출·빈도 기반 saturation curve)로 먼저 만들고,
토큰 수령 시 provider 교체만으로 전환되는 구조로 배선하겠습니다.
Q) 토큰 발급·수집을 DB 쪽에서 맡으실 계획입니까? 그렇다면 GEMINI_API_KEY 처럼 Secret Manager 주입을 예상합니다."""),

    ("all", "segment_revenue_purchase", "세그먼트 뷰에 revenue_purchase_krw 추가 요청", """[ROAS 정확도 — 급함]
dictionary_column_notes 에 v_perf_unified.revenue_krw 가 do_not_use 로 등재돼 있고
("google_ads 는 전 카테고리 전환가치 합이라 ROAS 가 1,782% 로 부풀려진다"),
revenue_purchase_krw 를 쓰라고 명시돼 있습니다. 벤치마크도 그대로 따라 고쳤습니다.
실측: google_ads ROAS 1,676% -> 246% (2026-01~).

문제는 세그먼트 뷰에는 그 컬럼이 없다는 것입니다. 아래 4개 뷰가 revenue_krw 만 갖고 있어,
디바이스·연령·성별·영상 차원의 ROAS 는 지금도 부풀려진 값입니다.
  v_perf_unified_device / v_perf_unified_age / v_perf_unified_gender / v_perf_unified_video

Q) 이 4개 뷰에 revenue_purchase_krw 를 추가해 주실 수 있습니까?
   추가되면 벤치마크 마트 빌더가 자동으로 그 컬럼을 집어 씁니다(_rev_expr 가 존재 여부로 분기).
   불가하면 알려 주십시오 — 해당 차원에서 ROAS 지표를 아예 내리겠습니다.
   부풀려진 ROAS 를 노출하느니 안 내는 편이 낫다고 판단합니다."""),

    ("all", "dict_rev_columns", "마트 rev 계열 컬럼 사전 갱신 요청", """[타 솔루션이 막히고 있습니다]
2026-09-22 자로 bm_campaign_monthly 의 rev 컬럼 의미를 바꿨습니다:
  rev      revenue_krw(혼합) -> revenue_purchase_krw(구매 계층)   ※ 이름은 그대로, 뜻만 바뀜
  rev_pur  rev 와 같은 값. '구매 계층'임을 이름으로 드러내려고 신설
  rev_all  예전 rev(혼합 전환가치). 진단·대조용. ROAS 분자로 쓰지 말 것
  conv_pur / conv_lead  구매·리드 계층 전환

A1 솔루션이 이 마트를 읽는데, 사전에 rev 가 아직 caution 으로 남아 있어
("매체별 정의가 다르다 - google=전 카테고리 전환가치, meta=omni_purchase, tiktok/kakao=미추적")
A1 게이트가 rev 를 막습니다. A1 은 rev_pur 만 쓰기로 했고 그게 맞는 동작입니다.

Q) dictionary_column_notes / dictionary_marts 에 아래를 반영해 주실 수 있습니까?
   - bm_campaign_monthly.rev      : caution 해제 또는 '구매 계층으로 변경됨(2026-09-22)' 로 갱신
   - bm_campaign_monthly.rev_pur  : ROAS 분자로 권장 (info)
   - bm_campaign_monthly.rev_all  : do_not_use (ROAS 분자 금지, 진단 전용)
   - bm_campaign_monthly.conv     : do_not_use (혼합 전환 - CPA/ROAS 금지)
   이름이 같은데 뜻이 바뀌는 것이 제일 위험한 변경이라, 사전에 남겨두는 편이 안전합니다."""),

    ("all", "purchase_conversions_other_platforms",
     "Meta·TikTok·Kakao·Naver 구매 계층 전환/매출 수집 가능 여부", """[CVR·ROAS 가 google_ads 전용이 된 근본 원인입니다]

사전 지시대로 ROAS 분자를 revenue_purchase_krw, CVR 분자를 conversions_purchase 로 바꿨습니다.
그 결과 지표가 google_ads 에서만 나옵니다 — 다른 매체는 구매 계층 값이 전부 0 이기 때문입니다.

  매체  conversions(혼합)  conversions_purchase  revenue_purchase_krw
  G       62,648,936          3,054,812            1,314억
  M        1,143,539                  0                  0
  T       11,370,356                  0                  0
  D        1,335,574                  0                  0
  N              716                  0                  0
  K              363                  0                  0

혼합 conv 로 계산하면 Google 42.22% · TikTok 76.02% 같은 값이 나와 '전환율'로 읽힐 수 없습니다
(사전에 적어주신 그대로 — google 전환의 92.7%가 참여, tiktok 은 목표달성 수).
그래서 혼합값은 cvr_all 로 격리하고 주지표에서 내렸습니다.

Q1) Meta 는 actions JSON 에 purchase 액션이 있을 텐데 conversions_purchase 로 분해 가능합니까?
    (omni_purchase 로 revenue 는 이미 뽑고 계신 것으로 압니다)
Q2) TikTok·Kakao·Naver 도 구매 전환을 따로 받을 수 있습니까? 아니면 원천에 계층 구분이 없습니까?
Q3) 불가한 매체는 'unavailable' 로 닫아 주십시오 — 화면에서 해당 매체 CVR·ROAS 를 영구 숨김으로 두겠습니다.

벤치마크는 매체 간 비교가 본질이라, 한 매체만 지표가 나오는 상태가 오래가면 안 됩니다.
가능한 매체부터 순차로 채워 주시면 커버리지 게이트가 알아서 열립니다."""),

    ("all", "deprecate_stale_marts", "bm_benchmark·bm_fact_monthly 삭제 요청", """\
A1 의 14_STALE_MARTS_FROM_A1.md 건 결론입니다.
- 조용한 실패가 아니라 '의도적 제거'였습니다. 커밋 83c9f9b(2026-06-12, 다차원 벤치마크 Phase A)에서
  사전집계 마트 → bm_campaign_monthly(캠페인 grain) + 백엔드 동적 분위수 계산으로 아키텍처를 바꾸며
  두 테이블 생성 코드를 걷어냈습니다. 마지막 빌드가 2026-06-11 인 것은 정상입니다.
- 되살리지 않습니다. 낡은 채로 조회되는 게 위험하다는 A1 지적이 옳으니
  bm_benchmark · bm_fact_monthly 를 삭제하거나 _deprecated 로 rename 해 주십시오.
  벤치마크 백엔드는 두 테이블을 참조하지 않습니다(grep 0건).
- bm_benchmark 를 dictionary_marts 에 일부러 등재하지 않으신 판단은 옳았습니다.
- A1 이 요청한 industry 축은 이미 bm_campaign_monthly 에 있고 /api/v1/benchmark?dim=industry 로 서비스 중입니다."""),
]

DDL = f"""CREATE TABLE IF NOT EXISTS {REQ_TBL} (
  dedupe_key STRING, requested_by STRING, platform STRING, metric STRING, label STRING,
  status STRING, coverage FLOAT64, detail STRING, db_response STRING,
  created_at TIMESTAMP, updated_at TIMESTAMP, last_seen_at TIMESTAMP
)"""


def _coverage(c):
    cols = sorted({w[3] for w in WANTED if w[3]})
    sel = ", ".join(f"SAFE_DIVIDE(COUNTIF({col}>0),COUNT(*)) cov_{col}" for col in cols)
    out = {}
    for r in c.query(f"SELECT media, {sel} FROM {MART} GROUP BY media").result():
        out[r["media"]] = {col: (r[f"cov_{col}"] or 0) for col in cols}
    return out


def request_gaps(c=None):
    """갭 스캔 후 요청 큐를 멱등 갱신(MERGE). open 갭 수 반환."""
    c = c or _client()
    try:
        c.query(DDL).result()
        cov = _coverage(c)
        present = set(cov.keys())
        recs = []
        for platform, metric, label, col in WANTED:
            media = MEDIA.get(platform)
            if media is None or media not in present or col is None:
                gap, coverage = True, 0.0          # 플랫폼/컬럼 미수집
            else:
                coverage = float(cov.get(media, {}).get(col, 0.0))
                gap = coverage < COVER_MIN
            recs.append((f"{AGENT_ID}|{platform}|{metric}", platform, metric, label, gap, round(coverage, 4)))
        using = " UNION ALL ".join(
            f"SELECT '{dk}' dedupe_key,'{pf}' platform,'{mt}' metric,'{lb}' label,"
            f"{str(gp).upper()} gap,{cv} coverage"
            for (dk, pf, mt, lb, gp, cv) in recs)
        c.query(f"""
        MERGE {REQ_TBL} T USING ({using}) S ON T.dedupe_key=S.dedupe_key
        WHEN MATCHED AND S.gap AND T.status IN ('open','ack') THEN UPDATE SET
          updated_at=CURRENT_TIMESTAMP(), last_seen_at=CURRENT_TIMESTAMP(), coverage=S.coverage
        WHEN MATCHED AND NOT S.gap AND T.status IN ('open','ack') THEN UPDATE SET
          status='fulfilled', db_response='데이터 확인됨(자동 해소)', updated_at=CURRENT_TIMESTAMP(), coverage=S.coverage
        WHEN NOT MATCHED AND S.gap THEN INSERT
          (dedupe_key, requested_by, platform, metric, label, status, coverage, detail, created_at, updated_at, last_seen_at)
          VALUES (S.dedupe_key, '{AGENT_ID}', S.platform, S.metric, S.label, 'open', S.coverage,
            '벤치마크 자동감지: 해당 플랫폼에서 이 지표 값이 비어있음. 연동 플랫폼·수집데이터 검토 후 채워주세요.',
            CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """).result()
        n_open = sum(1 for r in recs if r[4])
        print(f"· agent_data_requests MERGE 완료 — 현재 gap {n_open}건 (requested_by={AGENT_ID})")
        return n_open
    except Exception as e:   # 큐 갱신 실패가 마트 빌드를 깨지 않도록
        print(f"· [경고] 데이터-갭 요청 스킵: {str(e)[:160]}")
        return -1


def ask_db(c=None, asks=None):
    """자유질의를 같은 큐에 발행(멱등) + 이미 도착한 회신 출력. (발행건수, 회신건수) 반환.

    갭 스캔과 다른 점: 자동 해소 판정을 하지 않는다. '판단'을 구하는 질문이므로
    DB 에이전트가 db_response 를 적고 status 를 직접 닫아야 종료된다.
    이미 닫힌(fulfilled/unavailable) 질문은 다시 열지 않는다 — 결정 존중.
    """
    from google.cloud import bigquery
    c = c or _client()
    asks = asks if asks is not None else ASKS
    sent = 0
    try:
        c.query(DDL).result()
        for platform, metric, label, detail in asks:
            dk = f"{AGENT_ID}|{platform}|{metric}"
            job = bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter(n, "STRING", v) for n, v in
                [("dk", dk), ("by", AGENT_ID), ("pf", platform), ("mt", metric),
                 ("lb", label), ("dt", detail)]])
            c.query(f"""
            MERGE {REQ_TBL} T
            USING (SELECT @dk dedupe_key) S ON T.dedupe_key=S.dedupe_key
            WHEN MATCHED AND T.status IN ('open','ack') THEN UPDATE SET
              label=@lb, detail=@dt, updated_at=CURRENT_TIMESTAMP(), last_seen_at=CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
              (dedupe_key, requested_by, platform, metric, label, status, coverage, detail,
               created_at, updated_at, last_seen_at)
              VALUES (@dk, @by, @pf, @mt, @lb, 'open', NULL, @dt,
                      CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
            """, job_config=job).result()
            sent += 1
        print(f"· agent_data_requests 질의 발행 {sent}건 (requested_by={AGENT_ID})")

        keys = [f"{AGENT_ID}|{p}|{m}" for p, m, _, _ in asks]
        job = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ArrayQueryParameter("ks", "STRING", keys)])
        got = 0
        for r in c.query(f"""SELECT platform, metric, status, db_response, updated_at
                             FROM {REQ_TBL} WHERE dedupe_key IN UNNEST(@ks)
                             AND db_response IS NOT NULL AND db_response!=''
                             ORDER BY updated_at DESC""", job_config=job).result():
            got += 1
            print(f"  ← [{r['status']}] {r['platform']}|{r['metric']}: {(r['db_response'] or '')[:300]}")
        if not got:
            print("  ← 아직 회신 없음 (DB 에이전트가 큐를 폴링하면 db_response 에 답이 들어옵니다)")
        return sent, got
    except Exception as e:   # 질의 실패가 마트 빌드를 깨지 않도록
        print(f"· [경고] DB 질의 스킵: {str(e)[:160]}")
        return -1, -1


def answers(c=None):
    """이 에이전트가 올린 모든 요청/질의의 현재 상태를 출력(수동 확인용)."""
    c = c or _client()
    for r in c.query(f"""SELECT platform, metric, status, label, db_response, updated_at
                         FROM {REQ_TBL} WHERE requested_by='{AGENT_ID}'
                         ORDER BY status, platform, metric""").result():
        resp = (r["db_response"] or "").replace("\n", " ")[:160]
        print(f"[{r['status']:<11}] {r['platform']:<10} {r['metric']:<22} {r['label'][:34]:<34} {resp}")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    cl = _client()
    if cmd in ("all", "gaps"):
        request_gaps(cl)
    if cmd in ("all", "ask"):
        ask_db(cl)
    if cmd == "answers":
        answers(cl)
