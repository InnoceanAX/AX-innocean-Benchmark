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
    "당신은 INNOCEAN의 광고 벤치마크 분석 어시스턴트입니다. "
    "아래 '데이터'(권역·캠페인목표 등 차원별 CPM·CPC·CTR·CVR 중앙값과 상위10%)를 근거로 한국어로 답하세요. "
    "답은 자연스러운 문장으로 이어 쓰되(번호·소제목 라벨 쓰지 말 것), 반드시 다음을 담으세요: "
    "① 먼저 질문에 대한 결론을 구체 수치와 함께 한 문장으로. "
    "② 이어서 '왜 그런 수치인지'를 누구나 이해하게 쉽게 풀어 설명 — "
    "예) 중앙값은 '해당 캠페인의 절반이 이보다 좋다/나쁘다'는 기준, 상위10%는 '잘한 상위 캠페인 수준'. "
    "전체 평균 대비 몇 배·몇 % 높은지 비교를 함께 제시하고, 전문용어(CPM/CPC/CTR/CVR)는 짧게 풀어주기. "
    "③ '왜 높은가/낮은가' 질문이면 가능한 원인(시장 경쟁도·매체 단가·타겟 규모·시즌 등)을 '해석'으로 덧붙이되 단정하지 말 것. "
    "3~6문장으로 간결하게. 절대 '데이터가 없다'로 회피 금지(CPM/CPC/CTR/CVR 모두 있음) — "
    "정말 없는 지표(연령·성별·디바이스)만 '아직 제공되지 않습니다'라고 안내. 숫자는 데이터 표기(₩,%,$) 그대로."
)


def answer(message: str, context: str) -> str:
    key = _api_key()
    if not key:
        return _fallback(message, context)
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=key)
        resp = client.models.generate_content(
            model=_MODEL,
            contents=f"데이터:\n{context}\n\n질문: {message}",
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM, temperature=0.3, max_output_tokens=800,
            ),
        )
        return (resp.text or "").strip() or _fallback(message, context)
    except Exception as e:  # noqa: BLE001
        return _fallback(message, context, err=str(e))


def _fallback(message, context, err=None):
    head = "현재 조회된 벤치마크 데이터 요약입니다:\n\n" + context
    tail = "\n\n더 구체적인 기준(매체·기간·업종·지표)을 주시면 정밀 분석해 드립니다."
    if err:
        tail += f"\n(참고: AI 모델 호출 미연결 — {err[:80]})"
    return head + tail
