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
    "아래 '데이터'에 주어진 실제 수치만 근거로 한국어로 간결하게 답하세요. "
    "데이터에 없는 값은 추정하지 말고 '해당 데이터가 아직 없습니다'라고 답하세요. "
    "숫자는 데이터의 표기(₩, %)를 그대로 사용하세요."
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
