# -*- coding: utf-8 -*-
"""디플랜 NAS(소재 grain) 데이터 계층 — PPT 요구 (a) 매체탭 확장 · (b) 소재 나열 표.

⚠️ 이 모듈의 데이터는 `v_perf_unified` 계열(bq.py)과 **절대 합산하지 않는다.**
   같은 캠페인이 NAS 와 API 양쪽에 존재한다(DB dictionary_marts.purpose 에도 명시).
   그래서 bq.py 에 섞지 않고 별도 모듈·별도 엔드포인트로 분리했다 — 구조적으로 섞이지 않게.

소스: `apac_kr_benchmark.bm_dplan_creative_monthly` (마트. 원천은 apac_kr_unified.v_dplan_creative)
통화: KRW 고정. NAS 는 원화로만 들어온다.

금액 신뢰도(spend_basis, 파일 단위 enum):
  net_declared / net_estimated → 단가지표(CPM·CPC·CPV) 계산 허용
  gross_suspected              → 마크업·VAT 포함 총액 의심. 단가지표를 내지 않는다(cost_ok=false).
                                 노출·클릭·조회는 API 대조 비율이 1.0 이라 유효.
"""
import os
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
TBL = f"`{PROJECT}.apac_kr_benchmark.bm_dplan_creative_monthly`"
LOCATION = "asia-northeast3"

# PPT 요구 컬럼 순서. '국가'는 원천이 전량 KR 이라 축으로 무의미해 제외(DB QA/31_ 확인).
COLUMNS = [
    ("no",       "NO#",      "num"),
    ("industry", "업종",      "text"),
    ("period",   "집행월",     "text"),
    ("media",    "매체명",     "text"),
    ("product",  "상품명",     "text"),
    ("format",   "소재유형",    "text"),
    ("sec",      "소재초수",    "sec"),
    ("ratio",    "가로/세로",   "text"),
    ("goal",     "전환목표",    "text"),
    ("imp",      "노출",      "int"),
    ("clk",      "클릭",      "int"),
    ("views",    "조회",      "int"),
    ("ctr",      "CTR",      "pct"),
    ("vtr",      "VTR",      "pct"),
    ("cpm",      "CPM",      "money"),
    ("cpc",      "CPC",      "money"),
    ("cpv",      "CPV",      "money"),
    ("cvr",      "CVR",      "pct"),
    ("device",   "디바이스",    "text"),
]
# 광고비 기반 = spend_basis 가 총액 의심이면 내지 않는 지표
COST_METRICS = ("cpm", "cpc", "cpv")
# 필터 가능한 차원 (프론트 드롭다운)
FILTER_DIMS = {
    "media": "media_name", "industry": "industry", "advertiser": "advertiser",
    "brand": "brand", "product": "product", "format": "creative_format",
    "ratio": "creative_ratio", "device": "device", "goal": "conv_goal",
    "objective": "objective", "sec": "creative_sec",
}

for _k in [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
           os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))]:
    if _k and os.path.exists(_k):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
        break


def _client():
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _num(v):
    return f"{int(v):,}" if v is not None else None


def _money(v):
    return f"₩{v:,.0f}" if v is not None else None


def _pct(v):
    return f"{v:.2f}%" if v is not None else None


def _where(p0, p1, f):
    """기간 + 필터 절. f 는 {dim: value} — 빈 값은 무시."""
    w = ["period BETWEEN @p0 AND @p1"]
    prm = [bigquery.ScalarQueryParameter("p0", "STRING", p0),
           bigquery.ScalarQueryParameter("p1", "STRING", p1)]
    for i, (k, col) in enumerate(FILTER_DIMS.items()):
        v = (f or {}).get(k)
        if v in (None, "", "ALL"):
            continue
        if k == "sec":   # 초수는 정수 비교
            try:
                prm.append(bigquery.ScalarQueryParameter(f"f{i}", "INT64", int(v)))
            except (TypeError, ValueError):
                continue
        else:
            prm.append(bigquery.ScalarQueryParameter(f"f{i}", "STRING", str(v)))
        w.append(f"{col} = @f{i}")
    # 매체명·상품명 통합 검색 (PPT: '매체명/상품명 검색 기능')
    q = ((f or {}).get("q") or "").strip()
    if q:
        prm.append(bigquery.ScalarQueryParameter("q", "STRING", f"%{q}%"))
        w.append("(LOWER(media_name) LIKE LOWER(@q) OR LOWER(product) LIKE LOWER(@q) "
                 "OR LOWER(IFNULL(campaign_name,'')) LIKE LOWER(@q) "
                 "OR LOWER(IFNULL(creative_name,'')) LIKE LOWER(@q))")
    return " AND ".join(w), prm


# 캠페인/소재 단위 파생지표 — cost_ok=false 행은 광고비 기반 지표를 NULL 로 만든다.
# 광고비가 0인 행에서 CPM 을 '₩0' 으로 내면 '아주 싸게 샀다'로 읽힌다 — 실제로는 '값 없음'이다.
# cost_ok=false(기준 미확인)와 cost=0(광고비 없음)은 화면에서 다른 말로 표기한다.
_METRIC_SQL = """
  SAFE_DIVIDE(clk, imp)*100                                      AS ctr,
  SAFE_DIVIDE(views, imp)*100                                    AS vtr,
  SAFE_DIVIDE(conv, clk)*100                                     AS cvr,
  IF(cost_ok AND cost > 0, SAFE_DIVIDE(cost, imp)*1000, NULL)    AS cpm,
  IF(cost_ok AND cost > 0, SAFE_DIVIDE(cost, clk), NULL)         AS cpc,
  IF(cost_ok AND cost > 0, SAFE_DIVIDE(cost, views), NULL)       AS cpv,
  (cost = 0)                                                     AS cost_zero
"""


def freshness(c=None):
    """NAS 적재 시각 · 최신 집행월 — 화면 상단에 반드시 노출한다.
    NAS 는 수동·비정기 업로드라 '조회는 되는데 낡은' 상태가 생긴다(14_STALE_MARTS 와 같은 사고)."""
    c = c or _client()
    r = list(c.query(f"SELECT MAX(_src_loaded_at) loaded, MAX(period) latest, "
                     f"MIN(period) earliest, MAX(_built_at) built FROM {TBL}").result())[0]
    return {
        "source_loaded_at": r["loaded"].isoformat() if r["loaded"] else None,
        "latest_period": r["latest"], "earliest_period": r["earliest"],
        "built_at": r["built"].isoformat() if r["built"] else None,
        "note": "디플랜 NAS 는 수동·비정기 업로드입니다. 최신 집행월과 적재일의 간격을 확인하세요.",
    }


def get_summary(date_from="2026-01", date_to="2026-12", c=None):
    """매체별 요약 — PPT (a) 매체 탭. 광고비 신뢰도별로 갈라서 준다."""
    c = c or _client()
    w, prm = _where(date_from, date_to, {})
    rows = list(c.query(f"""
        SELECT media_name, ANY_VALUE(media_type) media_type, ANY_VALUE(api_platform) api_platform,
               COUNT(*) n, SUM(imp) imp, SUM(clk) clk, SUM(views) views,
               SUM(IF(cost_ok, cost, 0)) cost_ok_sum, SUM(cost) cost_all,
               LOGICAL_AND(cost_ok) all_cost_ok, LOGICAL_OR(cost_ok) any_cost_ok,
               STRING_AGG(DISTINCT spend_basis) bases
        FROM {TBL} WHERE {w} GROUP BY media_name ORDER BY cost_all DESC
    """, job_config=bigquery.QueryJobConfig(query_parameters=prm)).result())
    out = []
    for r in rows:
        ok = r["all_cost_ok"]
        cost = r["cost_ok_sum"] if ok else None
        out.append({
            "media": r["media_name"], "media_type": r["media_type"],
            "api_platform": r["api_platform"],      # 비어 있으면 NAS 전용(API 수집 없음)
            "nas_only": not r["api_platform"],
            "n": r["n"], "imp": _num(r["imp"]), "clk": _num(r["clk"]), "views": _num(r["views"]),
            "spend": _money(cost) if cost is not None else None,
            "cpm": _money(r["imp"] and cost and cost / r["imp"] * 1000) if ok else None,
            "cost_ok": ok,
            "cost_note": None if ok else "광고비 기준 미확인(총액 의심) — 단가지표 제외",
            "spend_basis": r["bases"],
        })
    return {"media": out, "n_media": len(out), "currency": "KRW",
            "date_from": date_from, "date_to": date_to,
            "freshness": freshness(c),
            "source": "디플랜 NAS", "never_sum_with_api": True}


def get_creatives(date_from="2026-01", date_to="2026-12", filters=None,
                  sort="imp", desc=True, limit=200, offset=0, c=None):
    """PPT (b) 소재 나열 표 + 필터링된 데이터의 합계·평균 행."""
    c = c or _client()
    filters = filters or {}
    w, prm = _where(date_from, date_to, filters)
    sort_col = sort if sort in {x[0] for x in COLUMNS} - {"no"} else "imp"
    sort_sql = {"media": "media_name", "format": "creative_format", "sec": "creative_sec",
                "ratio": "creative_ratio", "goal": "conv_goal"}.get(sort_col, sort_col)
    prm2 = list(prm) + [bigquery.ScalarQueryParameter("lim", "INT64", int(limit)),
                        bigquery.ScalarQueryParameter("off", "INT64", int(offset))]
    q = f"""
    WITH base AS (
      SELECT period, media_name, industry, advertiser, brand, product,
             campaign_name, creative_name, creative_format, creative_sec, creative_ratio,
             device, conv_goal, objective, imp, clk, views, cost, conv, cost_ok, spend_basis,
             {_METRIC_SQL}
      FROM {TBL} WHERE {w}
    )
    SELECT * FROM base ORDER BY {sort_sql} {'DESC' if desc else 'ASC'} NULLS LAST
    LIMIT @lim OFFSET @off
    """
    rows = list(c.query(q, job_config=bigquery.QueryJobConfig(query_parameters=prm2)).result())

    # 합계·평균 — 필터 전체 기준(페이지 아니라). 합계의 단가지표는 cost_ok 행만으로 계산.
    agg = list(c.query(f"""
      WITH base AS (SELECT imp, clk, views, cost, conv, cost_ok, {_METRIC_SQL} FROM {TBL} WHERE {w})
      SELECT COUNT(*) n,
             SUM(imp) imp, SUM(clk) clk, SUM(views) views, SUM(conv) conv,
             SUM(IF(cost_ok, cost, 0)) cost_ok_sum, SUM(IF(cost_ok, imp, 0)) imp_ok,
             SUM(IF(cost_ok, clk, 0)) clk_ok, SUM(IF(cost_ok, views, 0)) views_ok,
             COUNTIF(NOT cost_ok) n_cost_excluded,
             AVG(ctr) a_ctr, AVG(vtr) a_vtr, AVG(cvr) a_cvr,
             AVG(cpm) a_cpm, AVG(cpc) a_cpc, AVG(cpv) a_cpv
      FROM base
    """, job_config=bigquery.QueryJobConfig(query_parameters=prm)).result())[0]

    def row_out(i, r):
        return {
            "no": offset + i + 1,
            "industry": r["industry"], "period": r["period"], "media": r["media_name"],
            "product": r["product"],
            "format": r["creative_format"],
            # '해당 없음'(이미지·검색은 초수·비율 개념이 없음)과 '미상'을 화면에서 구분하기 위한 힌트
            "sec": r["creative_sec"], "ratio": r["creative_ratio"],
            "na": (r["creative_format"] in ("이미지", "반응형 검색광고", "텍스트")),
            "goal": r["conv_goal"], "device": r["device"],
            "imp": _num(r["imp"]), "clk": _num(r["clk"]), "views": _num(r["views"]),
            "ctr": _pct(r["ctr"]), "vtr": _pct(r["vtr"]), "cvr": _pct(r["cvr"]),
            "cpm": _money(r["cpm"]), "cpc": _money(r["cpc"]), "cpv": _money(r["cpv"]),
            "cost_ok": r["cost_ok"], "cost_zero": r["cost_zero"],
            "advertiser": r["advertiser"], "brand": r["brand"],
            "campaign": r["campaign_name"], "creative": r["creative_name"],
        }

    imp_ok, clk_ok, vw_ok, cost_ok_sum = (agg["imp_ok"] or 0), (agg["clk_ok"] or 0), \
                                         (agg["views_ok"] or 0), (agg["cost_ok_sum"] or 0)
    total = {
        "label": "합계", "n": agg["n"],
        "imp": _num(agg["imp"]), "clk": _num(agg["clk"]), "views": _num(agg["views"]),
        "ctr": _pct(agg["clk"] / agg["imp"] * 100) if agg["imp"] else None,
        "vtr": _pct(agg["views"] / agg["imp"] * 100) if agg["imp"] else None,
        "cvr": _pct(agg["conv"] / agg["clk"] * 100) if agg["clk"] else None,
        "cpm": _money(cost_ok_sum / imp_ok * 1000) if imp_ok else None,
        "cpc": _money(cost_ok_sum / clk_ok) if clk_ok else None,
        "cpv": _money(cost_ok_sum / vw_ok) if vw_ok else None,
        "n_cost_excluded": agg["n_cost_excluded"],
        "cost_note": (f"단가지표는 광고비 기준이 확인된 행만으로 계산했습니다"
                      f"(제외 {agg['n_cost_excluded']:,}행)") if agg["n_cost_excluded"] else None,
    }
    average = {
        "label": "평균",
        "imp": _num(round(agg["imp"] / agg["n"])) if agg["n"] else None,
        "clk": _num(round(agg["clk"] / agg["n"])) if agg["n"] else None,
        "views": _num(round(agg["views"] / agg["n"])) if agg["n"] else None,
        "ctr": _pct(agg["a_ctr"]), "vtr": _pct(agg["a_vtr"]), "cvr": _pct(agg["a_cvr"]),
        "cpm": _money(agg["a_cpm"]), "cpc": _money(agg["a_cpc"]), "cpv": _money(agg["a_cpv"]),
    }
    return {
        "columns": [{"key": k, "label": lb, "type": t} for k, lb, t in COLUMNS],
        "rows": [row_out(i, r) for i, r in enumerate(rows)],
        "total": total, "average": average,
        "n_total": agg["n"], "limit": limit, "offset": offset,
        "sort": sort_col, "desc": desc, "currency": "KRW",
        "freshness": freshness(c), "source": "디플랜 NAS", "never_sum_with_api": True,
    }


@lru_cache(maxsize=8)
def _options_cached(date_from, date_to, day):
    c = _client()
    w, prm = _where(date_from, date_to, {})
    out = {}
    for key, col in FILTER_DIMS.items():
        rows = list(c.query(
            f"SELECT {col} v, COUNT(*) n, SUM(imp) imp FROM {TBL} "
            f"WHERE {w} AND {col} IS NOT NULL GROUP BY 1 ORDER BY imp DESC LIMIT 300",
            job_config=bigquery.QueryJobConfig(query_parameters=prm)).result())
        out[key] = [{"v": (str(r["v"]) if r["v"] is not None else ""), "n": r["n"]} for r in rows]
    # 검색 자동완성 사전(매체명·상품명) — 프론트가 초성/자모 매칭에 쓴다
    out["search_terms"] = sorted({o["v"] for o in out.get("media", []) if o["v"]} |
                                 {o["v"] for o in out.get("product", []) if o["v"]})
    out["periods"] = [r["p"] for r in c.query(
        f"SELECT DISTINCT period p FROM {TBL} ORDER BY p").result()]
    return out


def get_options(date_from="2026-01", date_to="2026-12"):
    import datetime
    return _options_cached(date_from, date_to, datetime.date.today().isoformat())


if __name__ == "__main__":
    import json
    cl = _client()
    print(json.dumps(freshness(cl), ensure_ascii=False, indent=2))
    s = get_summary("2026-01", "2026-12", cl)
    for m in s["media"][:8]:
        print(f"  {m['media']:14} NAS전용={m['nas_only']} n={m['n']:>4} 노출={m['imp']:>14} "
              f"지출={m['spend'] or '(제외)':>16} CPM={m['cpm'] or '—'}")
    d = get_creatives("2026-01", "2026-12", limit=3, c=cl)
    print(f"\n소재 {d['n_total']:,}행 · 합계 {d['total']['imp']} 노출 · CPM {d['total']['cpm']}")
    print(f"  {d['total']['cost_note'] or ''}")
    for r in d["rows"]:
        print(f"  #{r['no']} {r['media']:8} {r['product'][:14]:14} {str(r['format']):6} "
              f"{str(r['sec'] or '—'):>4}s {str(r['ratio'] or '—'):6} 노출={r['imp']:>12} CPM={r['cpm'] or '—'}")
