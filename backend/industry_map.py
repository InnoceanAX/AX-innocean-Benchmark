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

# 키워드 → 업종 seed. 현재 실데이터는 대부분 현대·기아(자동차=수송/항공).
# 알려진 비-자동차 광고주 일부를 함께 시드(확장 가능, 대소문자 무시).
_KEYWORD_RULES = [
    # (업종, [키워드들])
    ("수송/항공", ["hyundai", "현대", "kia", "기아", "genesis", "제네시스",
                  "korean air", "대한항공", "asiana", "아시아나", "air", "motor", "auto"]),
    ("전자/가전", ["samsung", "삼성", "lg", "엘지", "electronics", "전자", "가전"]),
    ("미용/화장품", ["beauty", "cosmetic", "화장품", "amorepacific", "아모레", "올리브영", "oliveyoung"]),
    ("게임", ["game", "게임", "nexon", "넥슨", "netmarble", "넷마블", "ncsoft", "krafton"]),
    ("유통/쇼핑", ["shopping", "쇼핑", "commerce", "유통", "coupang", "쿠팡", "lotte", "롯데", "emart", "이마트"]),
    ("금융/보험", ["finance", "금융", "bank", "은행", "insurance", "보험", "card", "카드", "kb", "shinhan", "신한"]),
    ("패션", ["fashion", "패션", "apparel", "musinsa", "무신사", "nike", "adidas"]),
    ("앱/사이트", ["naver", "네이버", "kakao", "카카오", "app", "앱", "baemin", "배민", "toss", "토스"]),
    ("주택/건설", ["hanssem", "한샘", "건설", "construction"]),  # 프론트엔 없음 → 기타로 흡수
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
