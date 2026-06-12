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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
INDEX = os.path.join(ROOT, "index.html")

app = FastAPI(title="INNOCEAN Benchmark API", version="0.1.0")


@app.get("/healthz")
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
              currency: str = "KRW",
              market: str = "", objective: str = "", brand: str = "",
              industry: str = "", agency: str = ""):
    """다차원 벤치마크 — 기준차원(dim) × 필터 조합 4분위."""
    try:
        data = bq.get_benchmark(media=media, dim=dim, date_from=date_from, date_to=date_to,
                                currency=currency, market=market, objective=objective,
                                brand=brand, industry=industry, agency=agency)
        return JSONResponse(data)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "benchmark": [], "detail": []}, status_code=500)


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


@app.post("/api/v1/ai/chat")
def ai_chat(req: ChatReq):
    """벤치마크 데이터 기반 AI 분석 답변."""
    try:
        context = bq.get_summary_context(
            req.media, req.dim, req.date_from, req.date_to, req.currency,
            market=req.market, objective=req.objective, brand=req.brand, industry=req.industry)
        reply = ai.answer(req.message, context)
        return {"reply": reply}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"reply": f"(오류) {e}"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
