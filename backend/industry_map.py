"""
광고주(브랜드/캠페인) → 업종(業種) 매핑.

⚠️ 업종 분류 기준은 비즈니스 결정 사항(질문지 F1 / A1, owner: CEO·DB에이전트).
   실데이터(BigQuery)에는 업종 필드가 없으므로, 이 모듈이 advertiser_name /
   account_name / campaign_name 텍스트를 보고 프론트의 업종 라벨로 매핑한다.
   매핑 테이블이 확정되면(DB가 제공 예정) 이 seed 를 그 테이블 조회로 교체한다.

프론트(index.html) 라이브 업종 라벨과 1:1로 맞춘다:
  수송/항공 · 전자/가전 · 미용/화장품 · 게임 · 유통/쇼핑 · 금융/보험 · 패션 · 앱/사이트 · 기타
"""

# 프론트 라이브 소스의 업종 라벨 (frontend-live-contract 기준)
INDUSTRIES = [
    "수송/항공", "전자/가전", "미용/화장품", "게임",
    "유통/쇼핑", "금융/보험", "패션", "앱/사이트", "기타",
]

# 키워드 → 업종. 순서 = 우선순위(위에서부터 먼저 매칭). 대소문자 무시.
# 실데이터는 스펜드 ~95%가 현대·기아·제네시스(자동차) 글로벌. 자동차 코드(HMB/HMID 등) 폭넓게 포함.
_KEYWORD_RULES = [
    ("미용/화장품", ["beauty", "cosmetic", "화장품", "amorepacific", "아모레", "올리브영",
                  "oliveyoung", "클래시스", "classys", "더마", "derma", "skin", "에스티로더"]),
    ("의료/건강", ["자생", "한방", "병원", "hospital", "clinic", "의료", "health", "메디",
                 "medi", "pharma", "제약", "건강", "덴탈", "dental", "심층수", "탱글"]),
    ("게임", ["nexon", "넥슨", "netmarble", "넷마블", "ncsoft", "krafton", "크래프톤",
            "펄어비스", "게임", " game", "gaming", "rpg", "puzzle"]),
    ("금융/보험", ["현대해상", "보험", "insurance", "bank", "은행", "card", "카드", "kb",
                "shinhan", "신한", "토스", "toss", "금융", "finance", "증권", "캐피탈", "capital", "페이"]),
    ("패션", ["에잇세컨즈", "8 seconds", "8seconds", "무신사", "musinsa", "fashion", "패션",
            "apparel", "nike", "adidas", "의류", "shoes", "시계", "watch", "주얼리"]),
    ("교육/취업", ["교육", "edu", "학원", "academy", "사이버평생", "취업", "career", "스쿨", "school"]),
    ("전자/가전", ["samsung", "삼성", "lg전자", "엘지", "electronics", "전자", "가전",
                "스마트카라", "디스플레이", "반도체"]),
    ("유통/쇼핑", ["shopping", "쇼핑", "commerce", "유통", "coupang", "쿠팡", "lotte", "롯데",
                "emart", "이마트", "삼양", "식품", "food", "센골드", "gold", "마켓", "mall", "리테일몰"]),
    ("앱/사이트", ["당근", "danggn", "naver", "네이버", "kakao app", "배민", "baemin",
                "app", " 앱", "플랫폼", "platform", "커넥트", "connect"]),
    ("관광/레저", ["관광", "여행", "travel", "tour", "레저", "호텔", "hotel", "리조트", "resort"]),
    # ── 자동차(수송/항공): 현대·기아·제네시스 + 마켓/브랜드 코드 폭넓게 (마지막 폴백 직전) ──
    ("수송/항공", ["hyundai", "현대", "kia", "기아", "genesis", "제네시스", "hmb", "hmid",
                "hmph", "hmth", "hmmy", "hmgics", "hmcsa", "hmc", "hmth", "hmpv", "hmg",
                "ioniq", "아이오닉", "creta", "venue", "santa", "tucson", "motor",
                "korean air", "대한항공", "asiana", "아시아나", " air", "항공", "모빌리티", "mobility"]),
]

# 프론트에 없는 업종은 '기타'로 접는다
_FRONT_SET = set(INDUSTRIES)


def industry_of(*texts: str) -> str:
    """advertiser_name, account_name, campaign_name 등 임의 텍스트들로 업종 추정."""
    blob = " ".join(t for t in texts if t).lower()
    if not blob.strip():
        return "기타"
    for industry, keywords in _KEYWORD_RULES:
        for kw in keywords:
            if kw.lower() in blob:
                return industry if industry in _FRONT_SET else "기타"
    return "기타"


# ── 캠페인 목표/유형 (campaign_name 파싱) ──────────────────────────
# 실무자 친화 버킷. 순서=우선매칭(채널성격 먼저, 목표 다음). 대소문자 무시.
OBJECTIVES = ["영상조회", "검색", "퍼포먼스", "트래픽", "앱", "브랜딩", "기타"]
_OBJECTIVE_RULES = [
    ("영상조회", ["vvc", "trueview", "_video", "video_", "youtube", "_yt_", "vtr", "_view", "조회"]),
    ("검색", ["search", "_sem", "_rsa", "keyword", "pmax", "검색"]),
    ("퍼포먼스", ["lead", "conv", "_cov", "sales", "purchase", "perf", "conquer",
               "demand_gen", "demandgen", "전환", "리드"]),
    ("트래픽", ["trf", "traffic", "_lpv", "click", "트래픽"]),
    ("앱", ["_app_", "install", "앱설치"]),
    ("브랜딩", ["brand", "awareness", "reach", "_anc", "nsn", "브랜드", "인지"]),
]


def objective_of(campaign_name: str) -> str:
    blob = (campaign_name or "").lower()
    if not blob.strip():
        return "기타"
    for obj, kws in _OBJECTIVE_RULES:
        for kw in kws:
            if kw in blob:
                return obj
    return "기타"


def objective_case_sql(text_expr: str) -> str:
    whens = []
    for obj, kws in _OBJECTIVE_RULES:
        likes = " OR ".join([f"LOWER({text_expr}) LIKE '%{kw}%'" for kw in kws])
        whens.append(f"WHEN {likes} THEN '{obj}'")
    return "CASE\n      " + "\n      ".join(whens) + "\n      ELSE '기타'\n    END"


# BigQuery SQL 안에서 업종을 만들기 위한 CASE 식 생성기.
# (raw 텍스트 컬럼 표현식을 받아 업종 STRING 을 반환하는 SQL 조각)
# 2026-09-22 사용자 승인 — 업종 라벨 9 → 19 확장(A-①), 저장은 advertiser_dim.industry(B-①),
# 채우는 주체는 DB 추정 + 승인(C-①), 자동차는 브랜드 축에서 이미 갈리므로 쪼개지 않음(D).
# 신규 9종은 DB 실측에서 «기존 9라벨로 흡수 불가» 로 나온 것들이다(brand='other' 광고비의 43.9%).
# 벤치마크 표본 게이트(n>=20) 통과 여부도 확인했다 — 여행/관광을 뺀 8종이 31~481행으로 통과.
# 라벨명과 행수는 DB 전수 측정(brand='other' 405광고주 · 캠페인×월 · imp>1000) 기준.
EXTENDED_INDUSTRIES = [
    "교육",            # YBM·파고다·종로학원                  1,128행 ₩20.9억
    "기업PR/그룹",      # 한화그룹·삼양그룹                       729행 ₩23.1억
    "자동차부품/타이어",  # 한국타이어·타이어뱅크·불스원              666행 ₩24.9억
    "제약/화장품/건강",  # 동화약품·정관장·지르텍·자생한방            564행 ₩33.1억
    "건설/부동산",      # KCC건설 등                          1,012행 ₩15.3억
    "통신",            # SKT·KT·LGU+                         488행 ₩ 8.0억
    "비영리/기부",      #                                      294행 ₩ 1.9억
    "식음료",          # 도미노피자·롯데칠성·청정원·칼스버그         271행 ₩20.6억
    "법률",            # 로엘법무법인 — 단일 최대 광고주            194행 ₩21.9억
    "여행/관광",        # 제주신화월드·LA관광청·마리나베이            67행 ₩ 4.2억
]
# ⚠️ 제주항공·피치항공은 '여행/관광'이 아니라 '수송/항공'이다(DB 판정).
#    항공사와 관광지는 단가 구조가 달라 같은 칸에 넣으면 또 섞인다.
#
# 🔴 확정 매핑은 이 파일의 정규식이 아니라 `advertiser_dim.industry` 에 advertiser_id 로
#    못박는다. 정규식을 사전으로 옮기자는 게 이 작업의 요지인데 옮기면서 또 정규식을 만들면
#    3개월 뒤 같은 문제가 반복된다(DB 지적). 아래 _KEYWORD_RULES 는 사전이 비었을 때의
#    폴백으로만 남긴다 — 사전이 오면 industry_expr() 가 자동으로 그쪽을 쓴다.
#
# 매핑 완료 시 예상 효과(DB 실측): 기타 11,894행 → 2,993행, 광고비 32.5% → 약 3.7%
#   행의 74.8% · 광고비의 92.4% 해소. 잔여는 광고비 1천만원 미만 롱테일(405 중 186개).
#   상위 150 광고주가 광고비의 95.3% 를 덮으므로 매핑은 150개 선에서 끊는 것이 합리적.


def industry_expr(has_dim_column: bool, text_expr: str, alias: str = "u",
                  brand_expr: str = "") -> str:
    """업종 표현식 — **DB 사전(advertiser_dim.industry)이 있으면 그것을 쓴다.**

    정규식은 광고주가 늘 때마다 코드를 고쳐야 하고, 실제로 2026-06 부터 3개월간
    방치되면서 '기타'가 전체 행의 54.3%·광고비의 32.5%까지 커졌다.
    DB 가 v_perf_unified 에 industry 를 노출하면(승인 완료, 작업 대기) 여기서 자동 전환되고
    정규식은 그 값이 비었을 때만 쓰이는 폴백으로 내려간다.

    ★ 폴백의 «입력» 순서 — 광고주/브랜드가 먼저, 캠페인명은 최후 (2026-09-22)
      업종은 «누가 광고하는가» 의 속성이지 «그 캠페인을 뭐라 불렀는가» 가 아니다.
      캠페인명을 먼저 읽으면 이런 일이 난다(실측):
        `hmb | creta | lead | conversion | whatsapp`  ← 현대차 브라질 리드 캠페인
        → 'app' 토큰이 whats«app» 에 걸려 업종이 '앱/사이트' 로 잡힘. 자동차 ₩6.2억이
          앱 업종 벤치마크의 분모로 들어가 CPM·CPC 를 같이 흔들었다.
      DB(d9) 가 정확히 이 함정을 예고했다: "구분자 변형과 붙여쓰기는 같은 함정의 양면."
      토큰에 경계를 덧대는 대신 «틀릴 수 있는 입력» 을 뒤로 뺐다 — 정규식을 또 만들지
      않는다는 원칙(위 🔴)을 지키면서 같은 오분류를 막는 방법이다.
      실측 효과: 갈리는 ₩11.4억 전부가 수송/항공으로 교정(기타였던 ₩4.9억 포함),
      브랜드 기준이 더 뭉뚱그려지는 사례는 0건.
    """
    order = []
    if has_dim_column:
        order.append(f"NULLIF({alias}.industry,'')")
    if brand_expr:
        order.append(f"NULLIF({industry_case_sql(brand_expr)},'기타')")
    order.append(industry_case_sql(text_expr))
    return f"COALESCE({', '.join(order)})"


def industry_case_sql(text_expr: str) -> str:
    """text_expr: 소문자 정규화 전 텍스트 컬럼/식 (예: campaign_name)."""
    whens = []
    for industry, keywords in _KEYWORD_RULES:
        target = industry if industry in _FRONT_SET else "기타"
        likes = " OR ".join(
            [f"LOWER({text_expr}) LIKE '%{kw.lower()}%'" for kw in keywords]
        )
        whens.append(f"WHEN {likes} THEN '{target}'")
    whens_sql = "\n      ".join(whens)
    return f"CASE\n      {whens_sql}\n      ELSE '기타'\n    END"
