"""
INNOCEAN Benchmark 백엔드 (FastAPI).
- 기존 프론트(index.html) 를 그대로 서빙 + 프론트의 API_CONFIG 계약(/api/v1/*) 구현.
- 단일 컨테이너(Cloud Run). 정적+API 동일 오리진 → CORS 불필요.
"""
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import bq
import ai
import dplan
import reach

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INDEX = os.path.join(ROOT, "index.html")

app = FastAPI(title="INNOCEAN Benchmark API", version="0.1.0")


# ⚠️ `/healthz` 는 Google 프런트엔드가 가로채 컨테이너까지 오지 않는다(404 HTML 반환).
#    라우트는 등록돼 있는데 밖에서는 죽어 보여서 배포 검증에 상시 오탐이 났다.
#    그래서 실제 확인용 경로는 /api/v1/healthz 다 — deploy.py 도 이쪽을 본다.
@app.get("/healthz")
@app.get("/api/v1/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(INDEX, headers={"Cache-Control": "no-store"})


# ── 프론트 API_CONFIG 계약 ─────────────────────────────────────────
# baseUrl:'/api/v1', endpoints:{ benchmark:'/benchmark', chat:'/ai/chat' }

@app.get("/api/v1/benchmark")
def benchmark(media: str = "G", dim: str = "market",
              date_from: str = "2025-06-01", date_to: str = "2026-06-08",
              currency: str = "KRW", gross: float = 0.0,
              market: str = "", objective: str = "", brand: str = "",
              industry: str = "", agency: str = "", channel: str = ""):
    """다차원 벤치마크 — 기준차원(dim) × 필터 조합 4분위."""
    try:
        data = bq.get_benchmark(media=media, dim=dim, date_from=date_from, date_to=date_to,
                                currency=currency, gross=gross, market=market, objective=objective,
                                brand=brand, industry=industry, agency=agency, channel=channel)
        return JSONResponse(data)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "benchmark": [], "detail": []}, status_code=500)


@app.get("/api/v1/media_summary")
def media_summary(date_from: str = "2025-06-01", date_to: str = "2026-06-08", currency: str = "KRW"):
    """첫 진입 화면 상단 — 매체별 요약(상품수/노출/클릭/조회/예산)."""
    try:
        return JSONResponse(bq.get_media_summary(date_from, date_to, currency))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "media": [], "total": {}}, status_code=500)


@app.get("/api/v1/meta/options")
def filter_options(media: str = "G"):
    """필터 드롭다운용 차원별 distinct 값."""
    try:
        return JSONResponse(bq.get_filter_options(media))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


class ChatReq(BaseModel):
    message: str
    media: str = "G"
    dim: str = "market"
    date_from: str = "2025-06-01"
    date_to: str = "2026-06-08"
    currency: str = "KRW"
    market: str = ""
    objective: str = ""
    brand: str = ""
    industry: str = ""
    history: list = []   # [{role:'user'|'ai', text:str}, ...] 최근 대화


@app.post("/api/v1/ai/chat")
def ai_chat(req: ChatReq):
    """벤치마크 데이터 기반 AI 분석 답변."""
    try:
        context = bq.get_summary_context(
            req.media, req.dim, req.date_from, req.date_to, req.currency,
            market=req.market, objective=req.objective, brand=req.brand, industry=req.industry)
        res = ai.answer(req.message, context, history=req.history)
        # 동일 내용 한/영 동시 반환 — 프론트 토글이 선택 언어만 표시. reply=기존 호환(한국어).
        return {"reply": res["ko"], "reply_ko": res["ko"], "reply_en": res["en"]}
    except Exception as e:  # noqa: BLE001
        ko = f"(오류) {e}"
        en = f"(Error) {e}"
        return JSONResponse({"reply": ko, "reply_ko": ko, "reply_en": en}, status_code=500)


@app.get("/api/v1/percentile")
def percentile(metric: str = "cpm", value: float = 0.0, media: str = "G",
               date_from: str = "2025-06-01", date_to: str = "2026-12-31",
               market: str = "", objective: str = "", brand: str = "",
               industry: str = "", agency: str = "", channel: str = ""):
    """'내 캠페인이 벤치마크 어디쯤인가' — 입력 값의 분포 내 위치."""
    try:
        return JSONResponse(bq.percentile_rank(
            metric=metric, value=value, media=media, date_from=date_from, date_to=date_to,
            market=market, objective=objective, brand=brand, industry=industry,
            agency=agency, channel=channel))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"available": False, "error": str(e)}, status_code=500)


@app.get("/api/v1/compare")
def compare(media: str = "G", dim: str = "industry", mode: str = "prev",
            date_from: str = "2025-06-01", date_to: str = "2026-12-31",
            market: str = "", objective: str = "", brand: str = "",
            industry: str = "", agency: str = "", channel: str = ""):
    """기간 비교 — 전기(prev) / 전년 동기(yoy) 대비 증감 + 각 기간의 비교군 수."""
    try:
        return JSONResponse(bq.period_compare(
            media=media, dim=dim, date_from=date_from, date_to=date_to,
            mode=("yoy" if mode == "yoy" else "prev"),
            market=market, objective=objective, brand=brand, industry=industry,
            agency=agency, channel=channel))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "rows": {}}, status_code=500)


# ── 디플랜 NAS (소재 grain) ─────────────────────────────────────────
# ⚠️ 별도 엔드포인트로 분리. v_perf_unified 계열과 같은 캠페인이 양쪽에 있어 합산 금지.
#    프론트도 이 응답을 /api/v1/benchmark 결과와 섞어 더하지 않는다.

@app.get("/api/v1/dplan/summary")
def dplan_summary(date_from: str = "2026-01", date_to: str = "2026-12"):
    """디플랜 매체별 요약 — 넷플릭스·티빙·토스 등 NAS 전용 매체 포함."""
    try:
        return JSONResponse(dplan.get_summary(date_from, date_to))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "media": []}, status_code=500)


@app.get("/api/v1/dplan/creatives")
def dplan_creatives(date_from: str = "2026-01", date_to: str = "2026-12",
                    media: str = "", industry: str = "", advertiser: str = "",
                    brand: str = "", product: str = "", format: str = "",
                    ratio: str = "", device: str = "", goal: str = "",
                    objective: str = "", sec: str = "", q: str = "",
                    sort: str = "imp", desc: int = 1,
                    limit: int = 200, offset: int = 0):
    """소재 단위 나열 표 + 필터링된 데이터의 합계·평균 행."""
    try:
        f = {"media": media, "industry": industry, "advertiser": advertiser, "brand": brand,
             "product": product, "format": format, "ratio": ratio, "device": device,
             "goal": goal, "objective": objective, "sec": sec, "q": q}
        return JSONResponse(dplan.get_creatives(
            date_from, date_to, filters=f, sort=sort, desc=bool(desc),
            limit=min(max(int(limit), 1), 1000), offset=max(int(offset), 0)))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "rows": [], "columns": []}, status_code=500)


@app.get("/api/v1/dplan/options")
def dplan_options(date_from: str = "2026-01", date_to: str = "2026-12"):
    """디플랜 표 필터 드롭다운 + 검색 자동완성 사전."""
    try:
        return JSONResponse(dplan.get_options(date_from, date_to))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


# ── 도달 시뮬레이터 (PPT (c)) ────────────────────────────────────────

@app.get("/api/v1/reach/curve")
def reach_curve(budget: float = 2_000_000_000, media: str = "", market: str = "KR",
                universe: int = 0, points: int = 20, provider: str = ""):
    """예산 대비 도달(Reach 1+) 곡선.

    ⚠️ 기본 제공자는 '가정 기반 추정'이다 — 실측 적합이 아니다.
       응답의 fitted=False / assumptions 를 화면에 반드시 노출할 것.
    """
    try:
        return JSONResponse(reach.curve(budget=budget, media=media, market=market,
                                        universe=universe or None, points=points,
                                        provider=provider or None))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "points": []}, status_code=500)


@app.get("/api/v1/reach/targeting")
def reach_targeting(market: str = ""):
    """넷플릭스 도달 시뮬레이터의 선택 항목(연령·성별·기기·장르·관심사 등).

    market 을 비우면 전 국가 집합 + 국가별 가용 조건 수를 함께 준다 —
    국가마다 쓸 수 있는 조건이 달라서(US 839 · JP 354 · KR 251) 다국가 플랜에서는
    «이 나라에서는 못 쓰는 조건» 을 화면이 알려줘야 한다.
    """
    try:
        return JSONResponse(reach.targeting(market=market))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"available": False, "error": str(e),
                             "dimensions": {}}, status_code=500)


@app.get("/api/v1/reach/markets")
def reach_markets():
    """도달 곡선을 적합할 수 있는 시장 목록(표본 하한 이상)."""
    try:
        return JSONResponse(reach.markets())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "markets": []}, status_code=500)


@app.get("/api/v1/reach/providers")
def reach_providers():
    """사용 가능한 도달 추정 제공자 목록 + 각각의 준비 상태."""
    try:
        return JSONResponse(reach.providers())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
