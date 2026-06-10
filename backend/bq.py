"""
BigQuery 데이터 계층 — 벤치마크 전용 마트만 읽어 프론트 계약 모양으로 반환.

⚠️ 로우데이터·통합층 직접 접근 금지. 오직 `apac_kr_benchmark.*` 만 소비.

데이터 현실상 1차 축 = 권역(market). 핵심 산출:
  benchmark : 권역별 4분위(CPM/CPC/CTR 평균·중앙·상위25%·상위10% + 캠페인수)  ← FEATURES 의도
  detail    : 월×권역 집계 (₩·콤마·% 표시문자열)
  charts    : trend(월별 추세) + compare(권역 비교)  ← 실데이터
"""
import os
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
FACT = f"`{PROJECT}.apac_kr_benchmark.bm_fact_monthly`"
BENCH = f"`{PROJECT}.apac_kr_benchmark.bm_benchmark`"
LOCATION = "asia-northeast3"

MEDIA_NAME = {"G": "Google", "M": "Meta", "N": "Naver", "K": "Kakao", "D": "DV360", "T": "TikTok"}
KPIS = ("cpm", "cpc", "ctr")
KPI_LOWER_BETTER = {"cpm": True, "cpc": True, "ctr": False}

# 권역 코드 → 한글명
MARKET_NAME = {
    "KR": "한국", "IN": "인도", "BR": "브라질", "ES": "스페인", "SA": "사우디",
    "NL": "네덜란드", "JP": "일본", "PH": "필리핀", "ID": "인도네시아", "AU": "호주",
    "TH": "태국", "AE": "UAE", "GLOBAL": "글로벌", "GB": "영국", "DE": "독일",
    "IT": "이탈리아", "US": "미국", "FR": "프랑스", "IQ": "이라크", "QA": "카타르",
    "MY": "말레이시아", "VN": "베트남", "SG": "싱가포르", "MX": "멕시코", "CL": "칠레",
    "PE": "페루", "CO": "콜롬비아", "ZA": "남아공", "EG": "이집트", "TR": "튀르키예",
    "RU": "러시아", "PL": "폴란드", "CA": "캐나다", "KW": "쿠웨이트", "OM": "오만",
    "MA": "모로코", "KZ": "카자흐스탄", "UA": "우크라이나",
}


def mkt_name(code):
    return MARKET_NAME.get(code, code)


for _k in [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
           os.path.join(os.path.dirname(__file__), "sa_key.json"),
           os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))]:
    if _k and os.path.exists(_k):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
        break


@lru_cache(maxsize=1)
def _client():
    return bigquery.Client(project=PROJECT, location=LOCATION)


def _won(v):
    return "₩" + format(int(round(v or 0)), ",")


def _num(v):
    return format(int(round(v or 0)), ",")


def _pct(v):
    return f"{(v or 0):.2f}%"


def _kpi_fmt(kpi, v):
    return _pct(v) if kpi == "ctr" else _won(v)


def get_benchmark(media="G", date_from="2025-01-01", date_to="2026-12-31", brand=None):
    p0, p1 = date_from[:7], date_to[:7]
    cl = _client()
    params = [
        bigquery.ScalarQueryParameter("media", "STRING", media),
        bigquery.ScalarQueryParameter("p0", "STRING", p0),
        bigquery.ScalarQueryParameter("p1", "STRING", p1),
    ]
    brand_f = ""
    if brand and brand not in ("", "all", "전체"):
        brand_f = "AND brand = @brand"
        params.append(bigquery.ScalarQueryParameter("brand", "STRING", brand))
    qcfg = bigquery.QueryJobConfig(query_parameters=params)

    # 1) 권역별 집계 (fact) — 규모/평균
    agg = {r["market"]: r for r in cl.query(f"""
        SELECT market, SUM(impressions) imp, SUM(clicks) clk, SUM(cost) cost
        FROM {FACT}
        WHERE media=@media AND period BETWEEN @p0 AND @p1 {brand_f}
        GROUP BY market HAVING imp > 0
    """, job_config=qcfg).result()}

    if not agg:
        return {"benchmark": [], "detail": [], "charts": None, "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": False,
            "note": f"{MEDIA_NAME.get(media, media)} 데이터가 아직 없습니다 (네이버/카카오 수집 대기 중).",
        }}

    # 2) 4분위 벤치마크 (bench, 기간 무관 전체 분포)
    bench_rows = {r["market"]: dict(r) for r in cl.query(
        f"SELECT * FROM {BENCH} WHERE media=@media",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("media", "STRING", media)])).result()}

    benchmark = []
    for mk in sorted(agg, key=lambda k: -(agg[k]["cost"] or 0)):
        a = agg[mk]
        imp, clk, cost = a["imp"] or 0, a["clk"] or 0, a["cost"] or 0.0
        b = bench_rows.get(mk, {})
        row = {
            "market": mk, "name": mkt_name(mk),
            "n": b.get("n_campaigns", 0),
            "imp": _num(imp), "spend": _won(cost),
            "cpm": _won(cost / imp * 1000 if imp else 0),
            "cpc": _won(cost / clk if clk else 0),
            "ctr": _pct(clk / imp * 100 if imp else 0),
        }
        # 4분위 (각 KPI: 평균/중앙/상위25/상위10) — 표시문자열
        for k in KPIS:
            row[k + "_q"] = {
                "avg": _kpi_fmt(k, b.get(f"{k}_avg")),
                "median": _kpi_fmt(k, b.get(f"{k}_median")),
                "top25": _kpi_fmt(k, b.get(f"{k}_top25")),
                "top10": _kpi_fmt(k, b.get(f"{k}_top10")),
            }
        benchmark.append(row)

    # Total 행
    timp = sum(a["imp"] or 0 for a in agg.values())
    tclk = sum(a["clk"] or 0 for a in agg.values())
    tcost = sum(a["cost"] or 0.0 for a in agg.values())
    total = {"market": "TOTAL", "name": "전체", "n": sum(b.get("n_campaigns", 0) for b in bench_rows.values()),
             "imp": _num(timp), "spend": _won(tcost),
             "cpm": _won(tcost / timp * 1000 if timp else 0),
             "cpc": _won(tcost / tclk if tclk else 0),
             "ctr": _pct(tclk / timp * 100 if timp else 0), "cls": "ttl"}

    # 3) detail (월×권역)
    detail = []
    for r in cl.query(f"""
        SELECT period, market, SUM(impressions) imp, SUM(clicks) clk, SUM(cost) cost
        FROM {FACT}
        WHERE media=@media AND period BETWEEN @p0 AND @p1 {brand_f}
        GROUP BY period, market HAVING imp > 0
        ORDER BY period DESC, cost DESC
    """, job_config=qcfg).result():
        imp, clk, cost = r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0
        detail.append({
            "period": r["period"], "market": r["market"], "name": mkt_name(r["market"]),
            "spend": _won(cost), "imps": _num(imp), "clicks": _num(clk),
            "cpm": _won(cost / imp * 1000 if imp else 0),
            "cpc": _won(cost / clk if clk else 0),
            "ctr": _pct(clk / imp * 100 if imp else 0),
        })

    # 4) charts — trend(월별, 전체 CPM/CPC/CTR) + compare(권역별 median CPM)
    months = sorted({d["period"] for d in detail})
    trend = {"labels": months, "cpm": [], "cpc": [], "ctr": []}
    mtot = {m: [0, 0, 0.0] for m in months}
    for r in cl.query(f"""
        SELECT period, SUM(impressions) imp, SUM(clicks) clk, SUM(cost) cost
        FROM {FACT} WHERE media=@media AND period BETWEEN @p0 AND @p1 {brand_f}
        GROUP BY period
    """, job_config=qcfg).result():
        mtot[r["period"]] = [r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0]
    for m in months:
        imp, clk, cost = mtot[m]
        trend["cpm"].append(round(cost / imp * 1000, 1) if imp else 0)
        trend["cpc"].append(round(cost / clk, 1) if clk else 0)
        trend["ctr"].append(round(clk / imp * 100, 2) if imp else 0)
    # compare: 상위 10개 권역의 median CPM/CPC/CTR (벤치마크 분포)
    top = benchmark[:10]
    compare = {
        "labels": [b["name"] for b in top],
        "cpm": [bench_rows.get(b["market"], {}).get("cpm_median", 0) for b in top],
        "cpc": [bench_rows.get(b["market"], {}).get("cpc_median", 0) for b in top],
        "ctr": [bench_rows.get(b["market"], {}).get("ctr_median", 0) for b in top],
    }

    return {
        "benchmark": [total] + benchmark,
        "detail": detail,
        "charts": {"trend": trend, "compare": compare},
        "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": True,
            "markets": len(benchmark), "rows": len(detail),
            "date_from": date_from, "date_to": date_to,
            "dimension": "market", "note": "권역(market)별 벤치마크. 데이터 ~99% 현대·기아 자동차.",
        },
    }


def get_summary_context(media="G", date_from="2025-01-01", date_to="2026-12-31", brand=None):
    """AI 채팅이 인용할 실데이터 요약."""
    d = get_benchmark(media, date_from, date_to, brand)
    if not d["meta"]["available"]:
        return d["meta"]["note"]
    lines = [f"[{d['meta']['media_name']} 권역별 벤치마크 {date_from[:7]}~{date_to[:7]} "
             f"(CPM/CPC/CTR 중앙값·상위10%, 캠페인수)]"]
    for r in d["benchmark"][:12]:
        if r.get("cls") == "ttl":
            lines.append(f"- 전체: 노출 {r['imp']}, CPM {r['cpm']}, CTR {r['ctr']}, 지출 {r['spend']}")
            continue
        cpm = r["cpm_q"]; ctr = r["ctr_q"]
        lines.append(f"- {r['name']}(캠페인 {r['n']}): CPM 중앙 {cpm['median']}/상위10% {cpm['top10']}, "
                     f"CTR 중앙 {ctr['median']}/상위10% {ctr['top10']}, 지출 {r['spend']}")
    return "\n".join(lines)
