"""
BigQuery 데이터 계층 — 벤치마크 전용 마트만 읽어 프론트 계약 모양으로 반환.

⚠️ 이 모듈은 로우데이터(apac_kr_raw)·공용 통합층을 직접 읽지 않는다.
   오직 벤치마크 전용 마트 `apac_kr_benchmark.bm_fact_monthly` 만 소비한다.
   마트 생성/갱신은 mart.py (스케줄 배치) 담당.

반환 모양 (frontend-live-contract):
  summary: [{ind, imp, cpm, cpc, ctr, spend, cls?}]  # Total 행 먼저
  detail : [{period, industry, currency, spend, imps, clicks, cpm, cpc, ctr}]
값은 프론트가 그대로 렌더하도록 표시문자열(₩·콤마·%)로 내려준다.
"""
import os
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
MART = f"`{PROJECT}.apac_kr_benchmark.bm_fact_monthly`"   # 벤치마크 전용 마트 (유일한 소스)
LOCATION = "asia-northeast3"

MEDIA_NAME = {"G": "Google", "M": "Meta", "N": "Naver", "K": "Kakao",
              "D": "DV360", "T": "TikTok"}

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


def get_benchmark(media="G", date_from="2026-01-01", date_to="2026-12-31"):
    p0, p1 = date_from[:7], date_to[:7]   # YYYY-MM 비교 (마트 grain = 월)
    sql = f"""
      SELECT period, industry,
        SUM(impressions) imp, SUM(clicks) clk, SUM(cost) cost, SUM(conversions) conv
      FROM {MART}
      WHERE media = @media AND period BETWEEN @p0 AND @p1
      GROUP BY period, industry
      HAVING imp > 0
      ORDER BY period DESC, cost DESC
    """
    job = _client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("media", "STRING", media),
        bigquery.ScalarQueryParameter("p0", "STRING", p0),
        bigquery.ScalarQueryParameter("p1", "STRING", p1),
    ]))
    rows = [dict(r) for r in job.result()]

    if not rows:
        return {"summary": [], "detail": [], "meta": {
            "media": media, "available": False,
            "note": f"{MEDIA_NAME.get(media, media)} 데이터가 마트에 아직 없습니다 "
                    f"(네이버/카카오는 키 대기 중).",
        }}

    # detail (월×업종)
    detail = []
    for r in rows:
        imp, clk, cost = r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0
        detail.append({
            "period": r["period"], "industry": r["industry"], "currency": "KRW",
            "spend": _won(cost), "imps": _num(imp), "clicks": _num(clk),
            "cpm": _won(cost / imp * 1000 if imp else 0),
            "cpc": _won(cost / clk if clk else 0),
            "ctr": _pct(clk / imp * 100 if imp else 0),
        })

    # summary (업종 합계 + Total)
    by_ind = {}
    for r in rows:
        a = by_ind.setdefault(r["industry"], [0, 0, 0.0])
        a[0] += r["imp"] or 0; a[1] += r["clk"] or 0; a[2] += r["cost"] or 0.0
    summary, tot = [], [0, 0, 0.0]
    for ind, (imp, clk, cost) in sorted(by_ind.items(), key=lambda x: -x[1][2]):
        tot[0] += imp; tot[1] += clk; tot[2] += cost
        summary.append({
            "ind": ind, "imp": _num(imp),
            "cpm": _won(cost / imp * 1000 if imp else 0),
            "cpc": _won(cost / clk if clk else 0),
            "ctr": _pct(clk / imp * 100 if imp else 0),
            "spend": _won(cost),
        })
    total_row = {
        "ind": "Total", "imp": _num(tot[0]),
        "cpm": _won(tot[2] / tot[0] * 1000 if tot[0] else 0),
        "cpc": _won(tot[2] / tot[1] if tot[1] else 0),
        "ctr": _pct(tot[1] / tot[0] * 100 if tot[0] else 0),
        "spend": _won(tot[2]), "cls": "ttl",
    }
    return {
        "summary": [total_row] + summary,
        "detail": detail,
        "meta": {
            "media": media, "source": "apac_kr_benchmark.bm_fact_monthly",
            "available": True, "rows": len(detail),
            "date_from": date_from, "date_to": date_to,
            "note": "벤치마크 전용 마트 소비(로우 미접근). 통화/업종은 임시(C1/F1 결정 대기).",
        },
    }


def get_summary_context(media="G", date_from="2026-01-01", date_to="2026-12-31"):
    """AI 채팅이 인용할 실데이터 요약 텍스트."""
    d = get_benchmark(media, date_from, date_to)
    if not d["meta"]["available"]:
        return d["meta"]["note"]
    lines = [f"[{MEDIA_NAME.get(media, media)} 벤치마크 {date_from}~{date_to}]"]
    for r in d["summary"][:9]:
        lines.append(f"- {r['ind']}: 노출 {r['imp']}, CPM {r['cpm']}, "
                     f"CPC {r['cpc']}, CTR {r['ctr']}, 지출 {r['spend']}")
    return "\n".join(lines)
