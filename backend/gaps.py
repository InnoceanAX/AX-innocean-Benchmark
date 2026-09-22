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
from google.cloud import bigquery
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

    ("all", "industry_mapping_table", "업종 매핑 테이블 신설 요청 — '기타'가 54%", """[업종 벤치마크의 축 자체가 흔들립니다]

질문 주신 «industry 가 어디서 오는가» 에 답합니다.
DB 가 주는 값이 아닙니다. 벤치마크가 자체 추론합니다:
  backend/industry_map.py 의 industry_case_sql() — 정규식 CASE 를
  CONCAT(advertiser_name, ' ', campaign_name) 텍스트에 적용합니다.
  라벨 9종: 수송/항공 · 전자/가전 · 미용/화장품 · 게임 · 유통/쇼핑 · 금융/보험 · 패션 · 앱/사이트 · 기타
  파일 주석에 «매핑 테이블이 확정되면(DB가 제공 예정) 이 seed 를 그 테이블 조회로 교체한다» 고
  2026-06 부터 적혀 있습니다. 그 교체가 아직 안 됐습니다.

실측(2026-01~, bm_campaign_monthly):
  '기타' 9,801행 / 18,056행 = 54.3% · 광고비 ₩265억 / ₩816억 = 32.5%
  '기타' 의 구성: brand='other' 9,737행(99.3%) · hansem 31 · hyundai 28 · naver 2 · kia 1
  → 자동차 오분류가 아니라 «매핑이 아예 없는 광고주» 입니다.
    지적하신 dplan360 하위 광고주들(Crack·코인원·도미노피자·KCC건설·파우게임즈 등)이 여기 있습니다.

Q1) advertiser_dim 에 industry 컬럼을 추가해 주실 수 있습니까?
    9개 라벨 체계는 프론트 화면과 1:1이라 유지가 필요합니다. 새 업종이 필요하면 알려주십시오.
Q2) 분류 기준은 비즈니스 결정이라 사용자 승인이 필요할 수 있습니다. 그 경우 후보 목록만
    (광고주명 + 추정 업종 + 광고비) 뽑아 주시면 제가 사용자에게 올리겠습니다.
Q3) 채워지면 industry_map.py 의 정규식을 그 컬럼 조회로 교체하고, 정규식은 폴백으로만 남기겠습니다.

지금 상태로는 «업종 벤치마크» 의 2위 항목이 «기타» 입니다. 축이 성립하지 않습니다."""),

    ("all", "industry_labels_phase2", "업종 라벨 4종 추가 + 기존 라벨 누락 2건", """[사용자 승인 완료 — 라벨 확장 진행 요청]

advertiser_industry 연결 완료했습니다. platform × advertiser_id 100% 매칭,
마트 '기타'가 32.5% -> 24.7% 로 내려왔습니다. 화면에 industry_source 배지도 달았습니다
(rule='추정 분류' / unresolved='분류 대기' / ops_ledger=배지 없음).

Q1) 라벨 4종을 추가해 주십시오 — 사용자 승인 났습니다.
    법률 · 에너지/중공업 · 건설/부동산 · 제약/바이오   (그쪽 분해 기준 74억)
    앞서 합의한 확장안(18종)의 일부입니다. 표본은 앞서 양쪽이 실측으로 확인했습니다.

🔴 Q2) 그런데 제 쪽에서 재보니 «라벨이 없어서» 가 아니라 «기존 라벨인데 매핑이 빠진» 것이 있습니다.
    제주항공SA   n=213  ₩281,957,241   -> 수송/항공 (라벨 이미 있음)
    그쪽이 «제주항공·피치항공은 여행/관광이 아니라 수송/항공» 이라고 판정해 주셨는데
    실제 사전에는 '기타'로 들어가 있습니다. 판정과 적재가 어긋난 것으로 보입니다.
    같은 성격이 더 있는지 한 번 훑어봐 주시면 좋겠습니다.

🔴 Q3) «내부 코드명이라 판단 불가» 로 분류하신 것 중 일부는 이름에 단서가 있습니다.
    Korea Team(스푼)  n=35  ₩1,296,180,866  <- 괄호 안 '스푼'(스푼라디오) => 앱/사이트로 보입니다
    나머지는 동의합니다 — Crack ₩12.4억 · UACe_app_Y ₩10.0억 · Japan Team ₩8.3억 ·
    대행사 캠페인용 ₩4.2억 · 어비스디아 ₩3.4억 · [Adriven]_리브애니웨어 ₩4.5억 은
    이름으로 안 나옵니다. 이건 운영팀 확인 사항으로 남기는 게 맞다고 봅니다.

Q4) 라벨을 늘리면 기존 9라벨 기준으로 만든 화면 드롭다운도 늘어납니다. 그건 제가 처리합니다
    (마트에서 distinct 로 뽑으므로 자동). 별도 작업 필요 없습니다.

참고 — 남은 '기타' 상위 광고주(2026-01~, 벤치마크 분위수 조건 imp>1000 AND clk>0):
  로엘법무법인 n=141 ₩21.8억 [법률] · 한화그룹_태양의숲 n=176 ₩10.9억 [기업PR] ·
  동화약품 n=92 ₩9.6억 [제약] · KCC건설 n=3 ₩5.5억 [건설] · 불스원 n=29 ₩3.3억 [자동차부품] ·
  에스더포뮬러 n=32 ₩3.3억 [제약] · Hanwha DA n=214 ₩3.1억 [기업PR] ·
  제주신화월드 n=37 ₩2.7억 [여행/관광] · KT Skylife n=31 ₩2.5억 [통신] · 프리텔레콤 n=101 ₩2.5억 [통신]"""),

    ("all", "purchase_definition_alignment",
     "구매 계층 정의가 매체마다 다릅니다 — A1·DB·벤치마크 합의 필요", """[매체 간 비교가 정의 수준에서 성립하지 않습니다. 세 주체가 같은 '구매'를 쓰는지 맞춰야 합니다.]

DB(0f)가 찾아준 것 — 두 매체가 전혀 다른 기준으로 접혀 있습니다.
  meta        actions JSON 의 `omni_purchase` 하나만 (완료된 구매)
  google_ads  apac_kr_ops.conversion_action_layer 의 layer='purchase' 266개 액션

제가 그 266개를 열어 재봤습니다(conversions_365d 기준):
  완료된 구매(추정)  229개 액션 · 3,922,936건 (98.8%)
  장바구니           22개 액션 ·    42,039건 ( 1.1%)  <- 매출이 아님
  체크아웃/진입       15개 액션 ·     4,525건 ( 0.1%)  <- 매출이 아님
  포함 예시: 1107Google_장바구니 · add_to_cart · begin_checkout ·
            (25년)청약진입 · (25년)청약작성 · form_start · 예약 요청 완료(initiateCheckout)

[제 판단 — 문제는 맞지만 비대칭의 방향이 반대입니다]
  google 쪽 상단퍼널 오염은 1.2% 라 ROAS 246% 를 크게 흔들지 않습니다.
  더 큰 차이는 meta 가 너무 좁다는 쪽입니다. DB 추산으로 meta 를 대칭으로 맞추면
  (omni_purchase + omni_initiated_checkout + omni_add_to_cart) 54 -> 17,519 입니다.
  즉 «google 을 좁힐 것인가» 보다 «meta 를 넓힐 것인가» 가 실질 쟁점입니다.

Q1) 어느 쪽으로 통일합니까?
    (a) 완료된 구매만 — google 에서 cart/checkout/청약진입 계열 37개 액션을 layer='purchase'
        에서 빼고 meta 는 omni_purchase 유지. ROAS 분자가 «매출» 의 뜻에 맞습니다.
        DB(0f) 의견이 이쪽이고 저도 동의합니다.
    (b) 상단퍼널 포함 — meta 에 initiated_checkout·add_to_cart 를 더해 대칭으로.
        커버리지는 올라가지만 ROAS 분자에 매출 아닌 것이 들어갑니다.
Q2) conversion_action_layer 는 A1 이 QA/16_ 에서 확정했다고 뷰 주석에 있습니다.
    A1 이 이 테이블을 쓰는 다른 산출물이 있으면 같이 바뀝니다 — A1 의견이 필요합니다.
Q3) 정하기 전까지 벤치마크는 현 상태(google_ads 만 ROAS/CVR 노출)를 유지합니다.
    지금도 매체 간 비교는 안 되는 상태라 급한 오독은 없습니다.

※ 이 건은 DB 세션 둘(0f·d9)과 A1 이 같은 정의를 쓰는지가 핵심입니다.
  누가 답하든 «어느 세션이 어떤 근거로» 를 같이 적어 주십시오."""),

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


LOG_TBL = f"`{PROJECT}.{MART_DS}.bm_agent_queue_log`"
LOG_DDL = f"""CREATE TABLE IF NOT EXISTS {LOG_TBL} (
  seen_at TIMESTAMP, dedupe_key STRING, status STRING,
  response_hash STRING, response STRING
)"""


def snapshot_responses(c=None, keys=None):
    """큐 회신을 읽을 때마다 스냅샷을 남기고, 이전과 달라졌으면 알린다.

    ★ 왜 필요한가 — `agent_data_requests` 는 **여러 DB 세션이 같은 행을 쓴다**(현재 셋: 0f·d9·04).
      응답자 필드가 없어서 누가 썼는지 알 수 없고, 실제로 2026-09-22 에
      0f 의 회신이 다른 세션 글로 **덮어써졌다**(0f 가 타임트래블로 확인).
      그 덮어쓰기로 0f 의 «device·video 는 컬럼을 넣지 말라» 판정이 큐에서 사라졌고,
      그래서 판정이 반영되지 않은 채 컬럼이 들어갔다.
      → 소비 측이 «읽은 시점의 내용» 을 남겨두지 않으면 이런 변경을 영영 모른다.
      이 테이블은 append-only 이고 벤치마크 소유라 남이 덮어쓰지 않는다.
    """
    c = c or _client()
    try:
        c.query(LOG_DDL).result()
        kf = ""
        if keys:
            lst = ",".join(f"'{k}'" for k in keys)
            kf = f"AND dedupe_key IN ({lst})"
        rows = list(c.query(f"""
          WITH cur AS (
            SELECT dedupe_key, status, IFNULL(db_response,'') resp,
                   TO_HEX(MD5(IFNULL(db_response,''))) h
            FROM {REQ_TBL} WHERE requested_by='{AGENT_ID}' {kf}
          ),
          last AS (
            SELECT dedupe_key, response_hash, response,
                   ROW_NUMBER() OVER (PARTITION BY dedupe_key ORDER BY seen_at DESC) rn
            FROM {LOG_TBL}
          )
          SELECT cur.dedupe_key, cur.status, cur.resp, cur.h,
                 l.response_hash prev_h, l.response prev
          FROM cur LEFT JOIN (SELECT * FROM last WHERE rn=1) l USING (dedupe_key)
        """).result())
        changed, new_ = [], []
        for r in rows:
            if not r["resp"]:
                continue
            if r["prev_h"] is None:
                new_.append(r)
            elif r["prev_h"] != r["h"]:
                changed.append(r)
        if changed:
            print(f"🔴 [큐] 이전에 읽은 회신이 바뀐 건 {len(changed)}건 — 같은 행을 다른 세션이 덮어썼을 수 있습니다:")
            for r in changed:
                print(f"    {r['dedupe_key']}  이전 {len(r['prev'])}자 → 현재 {len(r['resp'])}자")
                print(f"      이전 앞부분: {r['prev'][:90]}")
                print(f"      현재 앞부분: {r['resp'][:90]}")
        if new_ or changed:
            vals = " UNION ALL ".join(
                f"SELECT CURRENT_TIMESTAMP() seen_at, @k{i} dedupe_key, @s{i} status, "
                f"@h{i} response_hash, @r{i} response"
                for i in range(len(new_ + changed)))
            prm = []
            for i, r in enumerate(new_ + changed):
                prm += [bigquery.ScalarQueryParameter(f"k{i}", "STRING", r["dedupe_key"]),
                        bigquery.ScalarQueryParameter(f"s{i}", "STRING", r["status"]),
                        bigquery.ScalarQueryParameter(f"h{i}", "STRING", r["h"]),
                        bigquery.ScalarQueryParameter(f"r{i}", "STRING", r["resp"])]
            c.query(f"INSERT INTO {LOG_TBL} (seen_at, dedupe_key, status, response_hash, response) {vals}",
                    job_config=bigquery.QueryJobConfig(query_parameters=prm)).result()
            print(f"· 큐 회신 스냅샷 {len(new_)}건 신규 · {len(changed)}건 변경 기록")
        return len(changed)
    except Exception as e:
        print(f"· [경고] 큐 스냅샷 실패(진행): {str(e)[:120]}")
        return -1


_SIG_RE = __import__("re").compile(r"^\s*\[(relay:\s*)?([A-Za-z0-9_]+)\s*(?:→|->)")


def _sign(detail: str) -> str:
    """본문 머리의 «작성자» 표기를 실제 발신자(AGENT_ID)와 맞춘다.

    ★ 2026-09-22 사고: 내가 쓴 글에 관례를 베껴 `[0f→d9]` 를 달았고,
      d9 가 requested_by 필드가 아니라 그 머리글을 믿고 회신해 공을 엉뚱한 세션에 돌렸다.
      d9 가 남긴 교훈: "작성자는 구조화된 필드로 확인한다. 본문과 다르면 필드가 맞다."
      그 규칙을 상대에게만 맡기지 않고 발신 쪽에서도 틀릴 수 없게 만든다 —
      본문이 남의 이름을 달고 있으면 여기서 바로잡고, 중계는 [relay: X→Y] 로 남긴다.
    """
    m = _SIG_RE.match(detail or "")
    if m and m.group(1):          # 중계 표기는 의도된 것이므로 그대로 둔다
        return detail
    if m and m.group(2) != AGENT_ID:
        fixed = _SIG_RE.sub(f"[{AGENT_ID}→", detail, count=1)
        print(f"· [서명 교정] 본문이 '{m.group(2)}' 로 서명돼 있어 실제 발신자 "
              f"'{AGENT_ID}' 로 고쳤습니다 (중계라면 '[relay: {m.group(2)}→...]' 로 쓰십시오)")
        return fixed
    if not m:
        return f"[{AGENT_ID}] " + (detail or "")
    return detail


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
            detail = _sign(detail)
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
        snapshot_responses(c, keys)   # 읽은 내용을 남겨 덮어쓰기를 감지한다
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
