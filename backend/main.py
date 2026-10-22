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


@app.get("/api/v1/dplan/pivot")
def dplan_pivot(date_from: str = "2026-01", date_to: str = "2026-12",
                row: str = "media", col: str = "format", metric: str = "cpm",
                media: str = "", industry: str = "", advertiser: str = "",
                brand: str = "", product: str = "", format: str = "",
                ratio: str = "", device: str = "", goal: str = "",
                objective: str = "", sec: str = "", q: str = ""):
    """행x열 교차표 — 디플랜 요청 "구글 맞춤보고서처럼 행/열을 선택".

    ⚠️ 셀 값은 그 칸 소재들의 «합계에서 다시 계산» 한다. 소재별 값을 평균내지 않는다 —
       소재마다 노출이 달라 평균하면 작은 소재가 과대 반영된다.
    """
    try:
        f = {"media": media, "industry": industry, "advertiser": advertiser,
             "brand": brand, "product": product, "format": format, "ratio": ratio,
             "device": device, "goal": goal, "objective": objective, "sec": sec, "q": q}
        return JSONResponse(dplan.get_pivot(date_from, date_to, row=row, col=col,
                                            metric=metric, filters=f))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "rows": [], "cols": [], "cells": {}},
                            status_code=500)


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
                universe: int = 0, points: int = 20, provider: str = "",
                flight_days: int = 30):
    """예산 대비 도달(Reach 1+) 곡선.

    ⚠️ 기본 제공자는 '가정 기반 추정'이다 — 실측 적합이 아니다.
       응답의 fitted=False / assumptions 를 화면에 반드시 노출할 것.
    """
    # flight_days 는 플래너가 의도한 집행기간이다. 자체 모델은 기간 해상도가 없어
    # (적합 표본이 전부 264일 이상) 계산에 쓰지 않고, 응답에 실어 화면이 «곡선의 기간
    # 기준» 과 어긋남을 경고하게 한다. 넷플릭스 곡선이 오면 flight_days 로 격자를 고른다.
    try:
        d = reach.curve(budget=budget, media=media, market=market,
                        universe=universe or None, points=points,
                        provider=provider or None, flight_days=flight_days)
        if isinstance(d, dict):
            d["flight_days_requested"] = int(flight_days)
        return JSONResponse(d)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "points": []}, status_code=500)


@app.get("/api/v1/reach/compare_providers")
def reach_compare_providers(budget: float = 2_000_000_000, media: str = "",
                            market: str = "KR", universe: int = 0, points: int = 20,
                            flight_days: int = 30):
    """쓸 수 있는 도달 제공자 전부를 같은 예산 축에 올려 돌려준다.

    ⚠️ 못 쓰는 제공자도 이유와 함께 내려보낸다 — 빠뜨리면 화면이 지금 가진 것만
       보여주고, 플래너는 그게 전부라고 생각한다.
    """
    try:
        return JSONResponse(reach.compare_curves(
            budget=budget, media=media, market=market,
            universe=universe or None, points=points, flight_days=flight_days))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e), "series": [], "blocked": []},
                            status_code=500)


@app.get("/api/v1/reach/lookup")
def reach_lookup(market: str = "KR", flight_days: int = 30, media: str = "",
                 age: str = "", gender: str = "",
                 budget: float = 2_000_000_000, points: int = 20):
    """고른 타게팅 조합의 곡선 조회 — 없으면 «왜 없는지» 를 답한다.

    ⚠️ «격자에 없음» · «아직 안 부름» · «벤더가 거절» 은 서로 다른 상태다.
       한 칸에 넣으면 플래너가 «안 되는 것» 과 «아직인 것» 을 구분하지 못한다.
    """
    try:
        # ⚠️ v1 은 연령·성별만 지원한다 — 기기 축은 v2 범위다(DB 스펙 실측).
        #   이름으로 받아 서버가 격자 키(targeting_key)를 만든다. 라벨로 맞추지 않는다.
        return JSONResponse(reach.lookup_curve(
            market=market, flight_days=flight_days, media=media,
            age_name=age, gender_name=gender, budget=budget, points=points))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"found": False, "state": "error", "note": str(e)},
                            status_code=500)


@app.get("/api/v1/reach/synth")
def reach_synth(market: str = "KR", flight_days: int = 30, media: str = "",
                cells: str = "", budget: float = 2_000_000_000, points: int = 20):
    """고른 «배타적» 데모 칸들을 더해 조합 곡선을 만든다 — 벤더 호출 없이.

    cells: '25-34:Male,35-49:Male' 처럼 연령:성별 을 쉼표로. 성별만/연령만도 된다.

    ★ 왜 있나 — 넷플릭스 v1 은 일 50회다. 조합마다 부르면 한도가 먼저 끝난다.
      단일 12칸만 받아 두면 그 조합들은 더해서 답할 수 있다(DB 실측 오차 0.2% 이내).

    🔴 겹치는 조합은 거절한다. 「25-34 전성별」 × 「전연령 Male」 은 같은 사람을
      두 번 세고, 그 중복 비율은 «측정되지 않았다».
    """
    try:
        cs = []
        for tok in (cells or "").split(","):
            tok = tok.strip()
            if not tok:
                continue
            a, _, g = tok.partition(":")
            cs.append({"age": a.strip(), "gender": g.strip()})
        if not cs:
            return JSONResponse({"found": False, "state": "no_cells",
                                 "note": "합성할 칸을 고르지 않았습니다."})
        return JSONResponse(reach.synth_curve(
            cs, market=market, flight_days=flight_days, media=media,
            budget=budget, points=points))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"found": False, "state": "error", "note": str(e)},
                            status_code=500)


@app.get("/api/v1/reach/demo_coverage")
def reach_demo_coverage(market: str = "KR", flight_days: int = 30):
    """연령×성별 12칸의 합이 «타게팅 없음» 의 몇 %인가 — 실측 89.6%.

    🔴 이건 결함 보고가 아니라 플래너에게 주는 정보다. 데모 칸을 다 사도 전체의
      89.6% 이고, 나머지 10.4% 는 연령·성별로는 닿을 수 없다.
      «왜» 닿지 않는지는 아직 모른다(연령·성별 미상이거나 18세 미만) — 둘 중
      무엇이냐에 따라 「닿지만 타게팅 불가」와 「광고 대상 아님」으로 해석이 갈린다.
    """
    try:
        return JSONResponse(reach.synth_selfcheck(market=market, flight_days=flight_days))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "note": str(e)}, status_code=500)


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


@app.get("/api/v1/reach/curve_status")
def reach_curve_status(market: str = "KR"):
    """곡선 캐시 상태 내역 — «불가능» 과 «아직 안 됨» 을 갈라 보여주기 위한 값.

    not_permitted(403, 조건을 바꿔야 함) 와 not_requested(아직 호출 전, 기다리면 생김) 는
    플래너가 취할 행동이 다르다. 한 칸에 섞으면 둘 다 «안 됨» 으로 읽힌다.
    """
    try:
        return JSONResponse(reach.curve_status(market=market))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"available": False, "error": str(e), "rows": []},
                            status_code=500)


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
