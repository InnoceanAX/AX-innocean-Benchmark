"""
AI 분석 답변 계층.
- 현재: Google AI Studio(Gemini) API 키 사용 (setup/innocean-gemini-api_aistudio.txt).
- 추후: Vertex AI Claude 로 교체 가능 (provider 추상화).
- 키/라이브러리 부재 시: 실데이터 요약 기반 템플릿으로 graceful 폴백.
실데이터(context)를 근거로만 답하도록 시스템 프롬프트로 제약 → 환각 최소화.
"""
import os

_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_KEY_FILES = [
    os.environ.get("GEMINI_API_KEY_FILE", ""),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                 "setup", "innocean-gemini-api_aistudio.txt")),
]


def _api_key():
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    for f in _KEY_FILES:
        if f and os.path.exists(f):
            return open(f, encoding="utf-8").read().strip()
    return None


SYSTEM = (
    "당신은 INNOCEAN의 광고 벤치마크 데이터 분석가입니다. 아래 '데이터'(차원별 CPM·CPC·CTR·CVR·ROAS의 "
    "평균·중앙값·상위25%·상위10%·지출)를 **유일한 근거**로 한국어로 **상세하고 친절하게** 분석해 답하세요. "
    "데이터에 없는 수치는 지어내지 말 것.\n"
    "답변 구성(자연스러운 문장·문단으로, 번호 라벨은 쓰지 말되 필요하면 '•' 불릿 사용 가능):\n"
    "1) 결론 먼저 — 질문에 대한 답을 구체 수치와 함께 한두 문장으로 명확히.\n"
    "2) 근거(핵심) — 데이터에서 관련 수치를 **직접 인용**하며 설명: 해당 항목의 중앙값/상위10%, "
    "전체 평균 대비 몇 배·몇 %인지, 다른 항목·전체와의 비교, 지출 규모(표본 신뢰도)까지. 수치는 데이터 표기(₩,%,$,배) 그대로.\n"
    "3) 해석 — 왜 그런 수치인지 가능한 원인(시장 경쟁도·매체 단가·타겟 규모·캠페인 목표·시즌 등)을 "
    "'해석'으로 풀어주되 단정하지 말 것(‘~로 보입니다/~일 수 있습니다’). 용어(CPM=1,000회 노출당 비용 등)는 처음 1회 짧게 풀어주기.\n"
    "4) 시사점 — 실무자가 참고할 한 줄 인사이트나 다음에 확인하면 좋을 점.\n"
    "분량은 질문 난이도에 맞춰 충분히(보통 5~10문장). 통계 의미는 쉽게: 중앙값='절반 기준', 상위10%='잘한 상위 캠페인 수준'.\n"
    "**철칙: 모든 주장에는 데이터의 구체 수치 근거를 붙인다. 근거 없는 추측·일반론만으로 답하지 말 것.** "
    "데이터에 있는 지표(CPM/CPC/CTR/CVR/ROAS 등)는 '없다'고 회피하지 말고 반드시 수치로 답하라.\n"
    "단, 질문이 현재 데이터로 답할 수 없는 경우(예: 데이터에 없는 항목·차원·매체·기간·세그먼트를 물었거나, "
    "특정 대상이 데이터 목록에 없거나, 질문이 모호해 어떤 수치를 봐야 할지 불명확한 경우)에는 "
    "추측으로 지어내지 말고: ①왜 지금 데이터로 답하기 어려운지 구체적으로 설명하고(무엇이 없는지), "
    "②대신 답할 수 있는 가장 가까운 분석을 데이터 근거와 함께 제시한 뒤, "
    "③정확히 답하려면 무엇을 바꿔/추가해 다시 물어보면 되는지 안내하라 "
    "(예: '매체를 Google로 바꿔주세요', '기준 기간을 좁혀주세요', '○○ 업종/국가를 지정해 주세요', '△△ 지표는 아직 수집 전이라 □□로 대체 분석 가능합니다'). "
    "요컨대 데이터로 뒷받침되면 상세히 답하고, 뒷받침 안 되면 정직하게 한계와 보완요청을 안내한다."
)


def answer(message: str, context: str, history=None) -> str:
    key = _api_key()
    if not key:
        return _fallback(message, context)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        # 대화 기록(최근 6턴) + 현재 데이터·질문
        contents = []
        for h in (history or [])[-6:]:
            txt = (h.get("text") or "").strip()
            if not txt:
                continue
            role = "model" if h.get("role") == "ai" else "user"
            contents.append(types.Content(role=role, parts=[types.Part(text=txt)]))
        contents.append(types.Content(role="user",
                        parts=[types.Part(text=f"[현재 화면 데이터]\n{context}\n\n질문: {message}")]))
        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM, temperature=0.35, max_output_tokens=3500,
            # gemini-2.5-flash의 thinking이 출력토큰을 소진해 답변이 잘리는 것 방지
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        resp = client.models.generate_content(model=_MODEL, contents=contents, config=cfg)
        return (resp.text or "").strip() or _fallback(message, context)
    except Exception as e:  # noqa: BLE001
        return _fallback(message, context, err=str(e))


def _fallback(message, context, err=None):
    head = "현재 조회된 벤치마크 데이터 요약입니다:\n\n" + context
    tail = "\n\n더 구체적인 기준(매체·기간·업종·지표)을 주시면 정밀 분석해 드립니다."
    if err:
        tail += f"\n(참고: AI 모델 호출 미연결 — {err[:80]})"
    return head + tail
