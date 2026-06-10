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
def benchmark(media: str = "G",
              date_from: str = "2026-01-01",
              date_to: str = "2026-06-08"):
    """매체별 업종 벤치마크 (summary + detail), 프론트 렌더 모양."""
    try:
        data = bq.get_benchmark(media=media, date_from=date_from, date_to=date_to)
        return JSONResponse(data)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "summary": [], "detail": []}, status_code=500)


class ChatReq(BaseModel):
    message: str
    media: str = "G"
    date_from: str = "2026-01-01"
    date_to: str = "2026-06-08"


@app.post("/api/v1/ai/chat")
def ai_chat(req: ChatReq):
    """벤치마크 데이터 기반 AI 분석 답변 (Vertex AI)."""
    try:
        context = bq.get_summary_context(req.media, req.date_from, req.date_to)
        reply = ai.answer(req.message, context)
        return {"reply": reply}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"reply": f"(오류) {e}"}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
