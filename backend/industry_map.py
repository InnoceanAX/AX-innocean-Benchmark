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
