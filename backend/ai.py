"""
AI 분석 답변 계층.
- 현재: Google AI Studio(Gemini) API 키 사용 (setup/innocean-gemini-api_aistudio.txt).
- 추후: Vertex AI Claude 로 교체 가능 (provider 추상화).
- 키/라이브러리 부재 시: 실데이터 요약 기반 템플릿으로 graceful 폴백.
실데이터(context)를 근거로만 답하도록 시스템 프롬프트로 제약 → 환각 최소화.
"""
import os
import re

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
    "답변은 **마크다운 구조**로 가독성 높게 작성한다. 긴 문단 나열 금지 — 핵심을 짧은 불릿으로 끊어서 스캔하기 쉽게. 형식:\n"
    "- 맨 위 한 줄: `**결론:** <질문의 답 + 핵심 수치 한두 개>` (한 문장).\n"
    "- `### 핵심 근거` 머리말 뒤 불릿 3~5개. 각 불릿은 `- **항목/지표**: 설명` 한 줄씩, "
    "중앙값·상위10%·전체평균 대비 배수/%·지출(표본 신뢰도)을 **수치는 굵게**로 직접 인용. 표기(₩,%,$,배)는 데이터 그대로.\n"
    "- `### 해석` 머리말 뒤 불릿 1~3개. 왜 그런 수치인지 가능한 원인(시장 경쟁도·매체 단가·타겟 규모·캠페인 목표·시즌 등)을 "
    "단정하지 말고('~로 보입니다/~일 수 있습니다') 풀어준다.\n"
    "- `### 시사점` 머리말 뒤 불릿 1~2개. 실무자가 바로 참고할 액션이나 다음에 확인하면 좋을 점.\n"
    "각 불릿은 한 문장으로 짧고 명확하게(만연체 금지). 용어는 처음 1회만 짧게 풀어주기(예: CPM=1,000회 노출당 비용). "
    "통계 의미는 쉽게: 중앙값='절반 기준', 상위10%='잘한 상위 캠페인 수준'.\n"
    "**철칙: 모든 주장에는 데이터의 구체 수치 근거를 붙인다. 근거 없는 추측·일반론만으로 답하지 말 것.** "
    "데이터에 있는 지표(CPM/CPC/CTR/CVR/ROAS 등)는 '없다'고 회피하지 말고 반드시 수치로 답하라.\n"
    "단, 질문이 현재 데이터로 답할 수 없는 경우(예: 데이터에 없는 항목·차원·매체·기간·세그먼트를 물었거나, "
    "특정 대상이 데이터 목록에 없거나, 질문이 모호해 어떤 수치를 봐야 할지 불명확한 경우)에는 "
    "추측으로 지어내지 말고: ①왜 지금 데이터로 답하기 어려운지 구체적으로 설명하고(무엇이 없는지), "
    "②대신 답할 수 있는 가장 가까운 분석을 데이터 근거와 함께 제시한 뒤, "
    "③정확히 답하려면 무엇을 바꿔/추가해 다시 물어보면 되는지 안내하라 "
    "(예: '매체를 Google로 바꿔주세요', '기준 기간을 좁혀주세요', '○○ 업종/국가를 지정해 주세요', '△△ 지표는 아직 수집 전이라 □□로 대체 분석 가능합니다'). "
    "요컨대 데이터로 뒷받침되면 상세히 답하고, 뒷받침 안 되면 정직하게 한계와 보완요청을 안내한다.\n\n"
    # ── 출력 형식: 동일 내용을 한국어/영어 두 버전으로 (프론트 토글이 선택 언어만 표시) ──
    "**출력 형식(반드시 준수)**: 먼저 한국어 분석을 쓰고, 이어서 같은 내용의 영어 번역을 쓴다. "
    "정확히 아래 구분자만 사용하라(다른 머리말 금지):\n"
    "[KO]\n<한국어 분석>\n[EN]\n<English translation — same numbers, structure, and conclusions>\n"
    "두 버전 모두 위의 마크다운 구조(**결론:** 한 줄 → `### 핵심 근거`/`### 해석`/`### 시사점` + `-` 불릿)를 그대로 유지하고, "
    "수치·구조·결론이 완전히 동일해야 하며, 영어는 광고 실무 용어(CPM/CPC/CTR/CVR/ROAS/VTR/CPV 등)를 자연스럽게 사용한다."
)


def answer(message: str, context: str, history=None) -> dict:
    """동일 내용을 한국어/영어로 반환: {"ko": ..., "en": ...}."""
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
            system_instruction=SYSTEM, temperature=0.35, max_output_tokens=6000,
            # gemini-2.5-flash의 thinking이 출력토큰을 소진해 답변이 잘리는 것 방지
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        resp = client.models.generate_content(model=_MODEL, contents=contents, config=cfg)
        text = (resp.text or "").strip()
        if not text:
            return _fallback(message, context)
        ko, en = _split_bilingual(text)
        if not ko:
            return _fallback(message, context)
        return {"ko": ko, "en": en or ko}
    except Exception as e:  # noqa: BLE001
        return _fallback(message, context, err=str(e))


def _clean(s: str) -> str:
    # 잔여 코드펜스(```)·[KO]/[EN] 토큰 제거
    s = re.sub(r"```[a-zA-Z]*", "", s)
    s = re.sub(r"\[(KO|EN)\]", "", s)
    return s.strip()


def _split_bilingual(text: str):
    """'[KO]...[EN]...' 형식을 (ko, en)으로 분리. 구분자 없으면 (text, '')."""
    parts = re.split(r"\n?\s*\[EN\]\s*\n?", text, maxsplit=1)
    ko = _clean(parts[0])
    en = _clean(parts[1]) if len(parts) > 1 else ""
    return ko, en


def _fallback(message, context, err=None) -> dict:
    head_ko = "현재 조회된 벤치마크 데이터 요약입니다:\n\n" + context
    tail_ko = "\n\n더 구체적인 기준(매체·기간·업종·지표)을 주시면 정밀 분석해 드립니다."
    head_en = "Here is a summary of the currently loaded benchmark data:\n\n" + context
    tail_en = "\n\nShare more specific criteria (media, period, industry, metric) for a precise analysis."
    if err:
        tail_ko += f"\n(참고: AI 모델 호출 미연결 — {err[:80]})"
        tail_en += f"\n(Note: AI model not connected — {err[:80]})"
    return {"ko": head_ko + tail_ko, "en": head_en + tail_en}
