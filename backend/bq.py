"""
BigQuery 데이터 계층 — 벤치마크 전용 마트만 읽어 다차원 4분위 벤치마크 반환.

⚠️ 로우데이터·통합층 직접 접근 금지. 오직 `apac_kr_benchmark.bm_campaign_monthly` 만 소비.

기준차원(dim) × 필터(media·market·objective·brand·industry·기간) 조합으로
캠페인 KPI 분포(평균·중앙·상위25%·상위10%)를 동적 계산. KPI: CPM/CPC/CTR/CVR.
"""
import os
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
TBL = f"`{PROJECT}.apac_kr_benchmark.bm_campaign_monthly`"
# 세그먼트 차원 테이블 (DB 세그먼트 뷰 → 마트). 전부 Google 전용.
SEGMENT_TBL = {
    "device": f"`{PROJECT}.apac_kr_benchmark.bm_device_monthly`",
    "age": f"`{PROJECT}.apac_kr_benchmark.bm_age_monthly`",
    "gender": f"`{PROJECT}.apac_kr_benchmark.bm_gender_monthly`",
}
VIDEO_TBL = f"`{PROJECT}.apac_kr_benchmark.bm_video_monthly`"   # 영상(media='V') 전용 마트
LOCATION = "asia-northeast3"

MEDIA_NAME = {"G": "Google", "M": "Meta", "N": "Naver", "K": "Kakao",
              "D": "DV360", "T": "TikTok", "V": "영상(YouTube)"}
# KPI 정의: alias → (캠페인단위 SQL식, 낮을수록좋음, 표시포맷)
KPI_EXPR = {
    "cpm": "SAFE_DIVIDE(cost,imp)*1000", "cpc": "SAFE_DIVIDE(cost,clk)",
    "ctr": "SAFE_DIVIDE(clk,imp)*100", "cvr": "SAFE_DIVIDE(conv,clk)*100",
    "roas": "SAFE_DIVIDE(rev,cost)",
    "vtr": "SAFE_DIVIDE(vviews,imp)*100", "cpv": "SAFE_DIVIDE(cost,vviews)",
    "cr": "SAFE_DIVIDE(vp100,imp)*100",   # 완전조회율 = 노출 대비 끝까지 재생(≤100%)
}
KPI_LOWER_BETTER = {"cpm": True, "cpc": True, "ctr": False, "cvr": False, "roas": False,
                    "vtr": False, "cpv": True, "cr": False}
KPI_FMT = {"cpm": "money2", "cpc": "money2", "ctr": "pct", "cvr": "pct", "roas": "x",
           "vtr": "pct", "cpv": "money2", "cr": "pct"}
# 매체별 활성 KPI 집합. 영상(V)은 CPM/VTR/CPV/완전조회율, 그 외는 표준 5종.
KPIS_BY_MEDIA = {"V": ("cpm", "vtr", "cpv", "cr")}
KPIS_DEFAULT = ("cpm", "cpc", "ctr", "cvr", "roas")
KPIS = KPIS_DEFAULT   # 하위호환(타 모듈 참조)


def _agg_kpi(k, imp, clk, cost, conv, rev, vv, vp):
    """합계 지표로부터 KPI 집계값(표시용)."""
    if k == "cpm":  return cost / imp * 1000 if imp else 0
    if k == "cpc":  return cost / clk if clk else 0
    if k == "ctr":  return clk / imp * 100 if imp else 0
    if k == "cvr":  return conv / clk * 100 if clk else 0
    if k == "roas": return rev / cost if cost else 0
    if k == "vtr":  return vv / imp * 100 if imp else 0
    if k == "cpv":  return cost / vv if vv else 0
    if k == "cr":   return vp / imp * 100 if imp else 0
    return 0

# 기준차원 화이트리스트 (SQL 컬럼명 안전). device/age/gender는 DB 세그먼트 뷰 추가 시 자동 활성.
# market = 국가(ISO2)로 통합 — 별도 '권역' 중복 제거.
DIMS = {"market": "국가", "objective": "캠페인목표", "brand": "브랜드",
        "industry": "업종", "agency": "대행사", "channel": "광고상품",
        "device": "디바이스", "age": "연령", "gender": "성별"}
# 필터 화이트리스트 (channel/agency는 캠페인 마트에만 존재 — 세그먼트/영상 소스엔 미적용)
FILTERS = {"market", "objective", "brand", "industry", "agency", "channel"}
CAMPAIGN_ONLY_FILTERS = {"agency", "channel"}   # 세그먼트/영상 소스엔 없는 컬럼
CHANNEL_NAME = {"SEARCH": "검색", "DISPLAY": "디스플레이(배너)", "VIDEO": "동영상(YouTube)",
                "DEMAND_GEN": "디맨드젠", "PERFORMANCE_MAX": "실적최대화(PMax)",
                "MULTI_CHANNEL": "멀티채널", "SHOPPING": "쇼핑", "SMART": "스마트",
                "LOCAL": "로컬", "(기타)": "(기타)"}

_FX_SYM = {"KRW": "₩", "USD": "$", "EUR": "€", "JPY": "¥", "CNY": "¥", "INR": "₹"}
# fx_rates_daily 미수록/조회실패 대비 정적 폴백(to_krw)
_FX_FALLBACK = {"KRW": 1.0, "USD": 1520.21, "EUR": 1758.42, "JPY": 9.49, "CNY": 211.5, "INR": 15.98}


@lru_cache(maxsize=2)
def _fx_load(day):   # day = UTC 날짜키 → 매일 자동 무효화
    """최신 환율(to_krw) 로드. 마트 bm_fx 우선(서비스 SA 접근가능) → raw → 정적 폴백."""
    rates = dict(_FX_FALLBACK)
    asof = None
    for q in (
        "SELECT currency, to_krw, CAST(asof AS STRING) d FROM "
        "`innocean-perf-apac-kr.apac_kr_benchmark.bm_fx`",
        "SELECT currency, to_krw, CAST(date AS STRING) d FROM "
        "`innocean-perf-apac-kr.apac_kr_raw.fx_rates_daily` "
        "WHERE date=(SELECT MAX(date) FROM `innocean-perf-apac-kr.apac_kr_raw.fx_rates_daily`)",
    ):
        try:
            rows = list(_client().query(q).result())
            if rows:
                for r in rows:
                    if r["to_krw"]:
                        rates[r["currency"]] = float(r["to_krw"])
                asof = rows[0]["d"]
                break
        except Exception:
            continue
    rates["KRW"] = 1.0
    return rates, asof


def _fx():
    return _fx_load(_dt.datetime.utcnow().strftime("%Y-%m-%d"))


def _currency(cur):
    """(to_krw rate, symbol) — 마트는 KRW 기준, 표시통화로 나눠 환산."""
    cur = (cur or "KRW").upper()
    rates, _ = _fx()
    return rates.get(cur, _FX_FALLBACK.get(cur, 1.0)), _FX_SYM.get(cur, "")


# 하위호환: 정적 참조용(동적 환산은 _currency 사용)
CURRENCY = {c: (_FX_FALLBACK[c], _FX_SYM.get(c, "")) for c in _FX_FALLBACK}

MARKET_NAME = {
    "KR": "한국", "IN": "인도", "BR": "브라질", "ES": "스페인", "SA": "사우디",
    "NL": "네덜란드", "JP": "일본", "PH": "필리핀", "ID": "인도네시아", "AU": "호주",
    "TH": "태국", "AE": "UAE", "GLOBAL": "글로벌", "GB": "영국", "DE": "독일",
    "IT": "이탈리아", "US": "미국", "FR": "프랑스", "IQ": "이라크", "QA": "카타르",
    "MY": "말레이시아", "VN": "베트남", "SG": "싱가포르", "MX": "멕시코", "CL": "칠레",
    "PE": "페루", "CO": "콜롬비아", "ZA": "남아공", "EG": "이집트", "TR": "튀르키예",
    "PL": "폴란드", "CA": "캐나다", "KW": "쿠웨이트", "OM": "오만", "MA": "모로코",
    "KZ": "카자흐스탄", "UA": "우크라이나", "NO": "노르웨이", "TUN": "튀니지",
}
BRAND_NAME = {"hyundai": "현대", "kia": "기아", "genesis": "제네시스",
              "other": "기타", "innocean_internal": "이노션내부"}
GENDER_NAME = {"MALE": "남성", "FEMALE": "여성", "UNDETERMINED": "미상"}
DEVICE_NAME = {"MOBILE": "모바일", "DESKTOP": "데스크톱", "TABLET": "태블릿",
               "CONNECTED_TV": "커넥티드TV", "OTHER": "기타"}


def dim_name(dim, code):
    if dim == "market":
        return MARKET_NAME.get(code, code)
    if dim == "brand":
        return BRAND_NAME.get(code, code)
    if dim == "gender":
        return GENDER_NAME.get(code, code)
    if dim == "device":
        return DEVICE_NAME.get(code, code)
    if dim == "age":
        return "미상" if code == "UNDETERMINED" else code
    if dim == "channel":
        return CHANNEL_NAME.get(code, code)
    return code


# 인메모리 결과 캐시 — 마트는 일 단위 갱신이라 동일 조회는 재계산 불필요.
# 키에 날짜(UTC)를 포함해 매일 자동 무효화. 프로세스 재시작/재배포 시 초기화.
import datetime as _dt
_BENCH_CACHE = {}


def _cache_key(media, dim, date_from, date_to, currency, filters):
    fk = tuple(sorted((k, v) for k, v in filters.items()
                      if v not in (None, "", "all", "전체")))
    day = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    return (day, media, dim, date_from, date_to, (currency or "KRW").upper(), fk)


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


@lru_cache(maxsize=8)
def _segment_available(dim):
    t = SEGMENT_TBL.get(dim)
    if not t:
        return False
    try:
        _client().get_table(t.strip("`"))
        return True
    except Exception:
        return False


def _num(v):
    return format(int(round(v or 0)), ",")


def _pct(v):
    return f"{(v or 0):.2f}%"


def _filter_clauses(media, p0, p1, filters):
    clauses = ["media=@media", "period BETWEEN @p0 AND @p1"]
    params = [
        bigquery.ScalarQueryParameter("media", "STRING", media),
        bigquery.ScalarQueryParameter("p0", "STRING", p0),
        bigquery.ScalarQueryParameter("p1", "STRING", p1),
    ]
    for k, v in (filters or {}).items():
        if k in FILTERS and v not in (None, "", "all", "전체"):
            clauses.append(f"{k}=@f_{k}")
            params.append(bigquery.ScalarQueryParameter(f"f_{k}", "STRING", v))
    return " AND ".join(clauses), params


def get_benchmark(media="G", dim="market", date_from="2025-01-01", date_to="2026-12-31",
                  currency="KRW", gross=0.0, **filters):
    if dim not in DIMS:
        dim = "market"
    try:
        gross = max(0.0, float(gross or 0))
    except (TypeError, ValueError):
        gross = 0.0
    is_video = (media == "V")
    if is_video and dim not in ("market", "objective", "brand", "industry"):
        dim = "market"   # 영상 마트는 권역/목표/브랜드/업종 차원만 보유
    if not is_video and dim in SEGMENT_TBL and not _segment_available(dim):
        return {"benchmark": [], "detail": [], "charts": None, "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": False,
            "dim": dim, "note": f"{DIMS.get(dim, dim)} 데이터 준비 중입니다 (통합뷰 추가 대기)."}}
    src = VIDEO_TBL if is_video else SEGMENT_TBL.get(dim, TBL)
    if src != TBL:   # 세그먼트/영상 소스엔 channel/agency 컬럼 없음 → 해당 필터 제거
        filters = {k: v for k, v in filters.items() if k not in CAMPAIGN_ONLY_FILTERS}
    ckey = _cache_key(media, dim, date_from, date_to, currency, dict(filters, _g=gross))
    if ckey in _BENCH_CACHE:
        return _BENCH_CACHE[ckey]
    p0, p1 = date_from[:7], date_to[:7]
    rate, sym = _currency(currency)
    gf = 1.0 + gross / 100.0   # Net→Gross 수수료 계수 (gross=수수료율%, 0=Net). 비용계 지표에 적용.

    def money(v):     # 지출 등 큰 금액 — 정수 (Gross 반영)
        return sym + format(int(round((v or 0) * gf / rate)), ",")

    def money2(v):    # 단가(CPM/CPC/CPV) — 소수 2자리 (Gross 반영)
        return sym + format((v or 0) * gf / rate, ",.2f")

    def qf(kpi, v):   # KPI 표시포맷 (통화 환산·Gross)
        f = KPI_FMT.get(kpi, "money")
        if f == "pct":
            return _pct(v)
        if f == "x":   # ROAS — 비용↑이면 ROAS↓
            return f"{((v or 0) / gf):.2f}배"
        if f == "money2":
            return money2(v)
        return money(v)

    calc_kpis = list(KPIS_BY_MEDIA.get(media, KPIS_DEFAULT))

    def _qblock(k):
        lower = KPI_LOWER_BETTER[k]
        o25, o10 = (25, 10) if lower else (75, 90)
        e = "IF(rev>0,roas,NULL)" if k == "roas" else k
        return (f"AVG({e}) {k}_avg, APPROX_QUANTILES({e},100)[OFFSET(50)] {k}_median, "
                f"APPROX_QUANTILES({e},100)[OFFSET({o25})] {k}_top25, "
                f"APPROX_QUANTILES({e},100)[OFFSET({o10})] {k}_top10")

    cl = _client()
    where, params = _filter_clauses(media, p0, p1, filters)
    qcfg = bigquery.QueryJobConfig(query_parameters=params)

    vid_camp = ", SUM(vviews) vviews, SUM(vp100) vp100" if is_video else ""
    vid_pass = ", vviews, vp100" if is_video else ""
    vid_sum = ", SUM(vviews) vviews, SUM(vp100) vp100" if is_video else ""
    ck_exprs = ", ".join(f"{KPI_EXPR[k]} {k}" for k in calc_kpis)
    qcols = ", ".join(_qblock(k) for k in calc_kpis)
    camp_having = "imp >= 1000 AND vviews > 0" if is_video else "imp >= 1000 AND clk > 0"

    # 1) 기준차원별 4분위 + 합계 (캠페인 단위 분포)
    bench_sql = f"""
    WITH camp AS (
      SELECT {dim} AS dim, campaign_id,
        SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, SUM(conv) conv, SUM(rev) rev{vid_camp}
      FROM {src} WHERE {where}
      GROUP BY dim, campaign_id HAVING {camp_having}
    ),
    ck AS (
      SELECT dim, imp, clk, cost, conv, rev{vid_pass}, {ck_exprs}
      FROM camp
    )
    SELECT dim, COUNT(*) n, COUNTIF(rev>0) nrev, COUNTIF(conv>0) nconv, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, SUM(conv) conv, SUM(rev) rev{vid_sum},
      {qcols}
    FROM ck WHERE dim IS NOT NULL GROUP BY dim HAVING n >= 3
    ORDER BY cost DESC
    """
    rows = [dict(r) for r in cl.query(bench_sql, job_config=qcfg).result()]
    if not rows:
        return {"benchmark": [], "detail": [], "charts": None, "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": False,
            "dim": dim, "note": f"{MEDIA_NAME.get(media, media)}·해당 조건의 데이터가 없습니다.",
        }}

    def _vals(r):  # (imp, clk, cost, conv, rev, vviews, vp100)
        return (r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0, r.get("conv") or 0.0,
                r["rev"] or 0.0, r.get("vviews") or 0, r.get("vp100") or 0)

    benchmark = []
    tot = [0, 0, 0.0, 0, 0.0, 0, 0]   # imp, clk, cost, n, rev, vviews, vp100
    tot_nrev = 0                       # 전환가치(rev>0) 캠페인 수 → ROAS 커버리지 게이트용
    tot_nconv = 0                      # 전환(conv>0) 캠페인 수 → CVR 커버리지 게이트용
    for r in rows:
        imp, clk, cost, conv, rev, vv, vp = _vals(r)
        tot[0] += imp; tot[1] += clk; tot[2] += cost; tot[3] += r["n"]
        tot[4] += rev; tot[5] += vv; tot[6] += vp
        tot_nrev += (r.get("nrev") or 0); tot_nconv += (r.get("nconv") or 0)
        row = {"dim": r["dim"], "name": dim_name(dim, r["dim"]), "n": r["n"],
               "imp": _num(imp), "spend": money(cost)}
        for k in calc_kpis:
            row[k] = qf(k, _agg_kpi(k, imp, clk, cost, conv, rev, vv, vp))
            row[k + "_q"] = {q: qf(k, r.get(f"{k}_{q}")) for q in ("avg", "median", "top25", "top10")}
        benchmark.append(row)
    total = {"dim": "TOTAL", "name": "전체", "n": tot[3], "imp": _num(tot[0]),
             "spend": money(tot[2]), "cls": "ttl"}
    for k in calc_kpis:
        total[k] = qf(k, _agg_kpi(k, tot[0], tot[1], tot[2], 0, tot[4], tot[5], tot[6]))
    # ROAS·CVR는 전환(가치) 추적 캠페인이 일정 비율(≥10%) 이상일 때만 노출 — 추적 미흡 매체의
    # 오해성 0값 방지. ROAS: Google 50%/DV360 100% 통과, Meta 0.7%/TikTok·Kakao 0% 제외.
    # CVR: Google 48%/DV360 34%/TikTok 26% 통과, Meta·Kakao 0%(conversions 미수집) 제외.
    roas_cover = (tot_nrev / tot[3]) if tot[3] else 0
    conv_cover = (tot_nconv / tot[3]) if tot[3] else 0
    roas_avail = (not is_video) and roas_cover >= 0.10
    cvr_avail = (not is_video) and conv_cover >= 0.10

    # 2) detail (월 × 기준차원)
    detail = []
    det_sql = f"""
      SELECT period, {dim} AS dim, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost,
             SUM(conv) conv, SUM(rev) rev{vid_camp}
      FROM {src} WHERE {where} GROUP BY period, dim HAVING imp > 0
      ORDER BY period DESC, cost DESC
    """
    for r in cl.query(det_sql, job_config=qcfg).result():
        imp, clk, cost = r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0
        conv, rev = r.get("conv") or 0.0, r.get("rev") or 0.0
        vv, vp = (r.get("vviews") or 0), (r.get("vp100") or 0)
        d = {"period": r["period"], "name": dim_name(dim, r["dim"]),
             "spend": money(cost), "imps": _num(imp), "clicks": _num(clk),
             "cpm": money2(cost / imp * 1000 if imp else 0),
             "cpc": money2(cost / clk if clk else 0),
             "ctr": _pct(clk / imp * 100 if imp else 0)}
        if is_video:
            d.update({"views": _num(vv), "vtr": _pct(vv / imp * 100 if imp else 0),
                      "cpv": money2(cost / vv if vv else 0),
                      "cr": _pct(vp / imp * 100 if imp else 0)})
        else:   # 지표추가 대상: 전환수·CVR·ROAS (커버리지 있을 때만 의미)
            d.update({"conv": _num(conv), "cvr": _pct(conv / clk * 100 if clk else 0),
                      "roas": (f"{(rev / cost / gf):.2f}배" if cost else "—")})
        detail.append(d)

    # 3) charts: trend(월별, 조건 전체) + compare(기준차원별 중앙값)
    months = sorted({d["period"] for d in detail})
    trend = {"labels": months}
    for k in calc_kpis:
        trend[k] = []
    mt = {m: [0, 0, 0.0, 0.0, 0.0, 0, 0] for m in months}
    for r in cl.query(f"SELECT period, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, "
                      f"SUM(conv) conv, SUM(rev) rev{vid_camp} "
                      f"FROM {src} WHERE {where} GROUP BY period", job_config=qcfg).result():
        mt[r["period"]] = [r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0, r["conv"] or 0.0,
                           r["rev"] or 0.0, r.get("vviews") or 0, r.get("vp100") or 0]
    for m in months:
        imp, clk, cost, conv, rev, vv, vp = mt[m]
        for k in calc_kpis:
            v = _agg_kpi(k, imp, clk, cost, conv, rev, vv, vp)
            if KPI_FMT[k].startswith("money"):
                trend[k].append(round(v * gf / rate, 2))
            elif k == "roas":
                trend[k].append(round(v / gf, 2))
            else:
                trend[k].append(round(v, 2))
    top = benchmark[:10]
    compare = {"labels": [b["name"] for b in top]}
    for k in calc_kpis:
        med = lambda b, _k=k: next((r[f"{_k}_median"] for r in rows if r["dim"] == b["dim"]), 0) or 0
        if KPI_FMT[k].startswith("money"):
            compare[k] = [round(med(b) * gf / rate, 2) for b in top]
        elif k == "roas":
            compare[k] = [round(med(b) / gf, 2) for b in top]
        else:
            compare[k] = [round(med(b), 2) for b in top]

    meta_kpis = calc_kpis if is_video else (
        ["cpm", "cpc", "ctr"] + (["cvr"] if cvr_avail else []) + (["roas"] if roas_avail else []))
    # 원안대로 전체 지표를 노출하되, 데이터 없는 지표는 프론트에서 비활성(회색·0). all_kpis ⊇ kpis.
    all_kpis = ["cpm", "vtr", "cpv", "cr"] if is_video else ["cpm", "cpc", "ctr", "cvr", "roas"]
    fx_rates, fx_asof = _fx()
    result = {
        "benchmark": [total] + benchmark,
        "detail": detail,
        "charts": {"trend": trend, "compare": compare},
        "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": True,
            "dim": dim, "dim_label": DIMS[dim], "n_dim": len(benchmark), "rows": len(detail),
            "date_from": date_from, "date_to": date_to,
            "currency": (currency or "KRW").upper(), "symbol": sym, "cached": True,
            "gross": gross, "cost_basis": ("Gross" if gross > 0 else "Net"),
            "kpis": meta_kpis, "all_kpis": all_kpis,
            "roas_available": roas_avail, "cvr_available": cvr_avail,
            "roas_coverage": round(roas_cover, 3), "conv_coverage": round(conv_cover, 3),
            "is_video": is_video,
            "fx": {"asof": fx_asof, "USD": round(fx_rates.get("USD", 0), 2),
                   "EUR": round(fx_rates.get("EUR", 0), 2), "JPY": round(fx_rates.get("JPY", 0), 2),
                   "CNY": round(fx_rates.get("CNY", 0), 2), "INR": round(fx_rates.get("INR", 0), 2)},
            "note": ("영상 벤치마크 — Google 영상(YouTube) 캠페인. VTR=조회율, CPV=조회당비용, 완전조회율=끝까지 본 비율."
                     if is_video else "다차원 벤치마크. 데이터 ~99% 현대·기아 자동차."),
        },
    }
    if len(_BENCH_CACHE) > 300:
        _BENCH_CACHE.clear()
    _BENCH_CACHE[ckey] = result
    return result


@lru_cache(maxsize=1)
def _video_media_available():
    try:
        return list(_client().query(f"SELECT COUNT(*) n FROM {VIDEO_TBL}").result())[0]["n"] > 0
    except Exception:
        return False


@lru_cache(maxsize=8)
def get_filter_options(media="G"):
    """필터 드롭다운용 — 매체별 차원 distinct 값. 영상(V)은 영상 마트에서 조회."""
    cl = _client()
    out = {}
    is_video = (media == "V")
    osrc = VIDEO_TBL if is_video else TBL
    cols = ("market", "objective", "brand", "industry") if is_video \
        else ("market", "objective", "brand", "industry", "agency", "channel")
    for col in cols:
        rows = cl.query(
            f"SELECT {col} v, SUM(cost) s FROM {osrc} WHERE media=@m AND {col} IS NOT NULL "
            f"GROUP BY 1 ORDER BY s DESC LIMIT 50",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("m", "STRING", media)])).result()
        out[col] = [{"v": r["v"], "name": dim_name(col, r["v"])} for r in rows if r["v"]]
    # 세그먼트 차원 가용성 = 해당 매체에 데이터가 있을 때만 (영상엔 세그먼트 없음)
    for seg, tbl in SEGMENT_TBL.items():
        out[seg + "_available"] = False
        if not is_video and _segment_available(seg):
            try:
                n = list(cl.query(
                    f"SELECT COUNT(*) n FROM {tbl} WHERE media=@m",
                    job_config=bigquery.QueryJobConfig(query_parameters=[
                        bigquery.ScalarQueryParameter("m", "STRING", media)])).result())[0]["n"]
                out[seg + "_available"] = n > 0
            except Exception:
                pass
    out["video_media_available"] = _video_media_available()   # 영상(V) 매체 탭 노출 여부
    # 광고상품(channel) 가용성 — '(기타)' 외 실제 채널유형이 있는 매체만(현재 Google)
    out["channel_available"] = False
    if not is_video:
        try:
            n = list(cl.query(
                f"SELECT COUNT(DISTINCT channel) n FROM {TBL} WHERE media=@m AND channel!='(기타)'",
                job_config=bigquery.QueryJobConfig(query_parameters=[
                    bigquery.ScalarQueryParameter("m", "STRING", media)])).result())[0]["n"]
            out["channel_available"] = n > 0
        except Exception:
            pass
    return out


def get_summary_context(media="G", dim="market", date_from="2025-01-01", date_to="2026-12-31",
                        currency="KRW", **filters):
    d = get_benchmark(media, dim, date_from, date_to, currency, **filters)
    if not d["meta"]["available"]:
        return d["meta"]["note"]
    lbl = d["meta"]["dim_label"]
    cur = d["meta"].get("currency", "KRW")
    kpis = d["meta"]["kpis"]
    KU = {"cpm": "CPM", "cpc": "CPC", "ctr": "CTR", "cvr": "CVR", "roas": "ROAS",
          "vtr": "VTR(조회율)", "cpv": "CPV(조회당비용)", "cr": "완전조회율"}
    better = ", ".join(f"{KU[k]}={'낮을수록' if KPI_LOWER_BETTER.get(k) else '높을수록'} 좋음" for k in kpis)
    cb = d["meta"].get("cost_basis", "Net")
    lines = [f"[{d['meta']['media_name']} · {lbl}별 벤치마크 · {date_from[:7]}~{date_to[:7]} · 통화 {cur} · 비용기준 {cb}]",
             f"(각 {lbl}의 캠페인 KPI 분포 = 평균/중앙값/상위25%/상위10%. {better}. "
             f"중앙값=절반 기준, 상위10%=잘한 상위 캠페인 수준)"]
    for r in d["benchmark"][:18]:
        if r.get("cls") == "ttl":
            agg = ", ".join(f"{KU[k]} {r.get(k)}" for k in kpis)
            lines.append(f"- [전체평균] {agg} · 노출 {r['imp']} · 지출 {r['spend']}")
            continue
        parts = []
        for k in kpis:
            q = r.get(k + "_q")
            if q:
                parts.append(f"{KU[k]}(평균 {q['avg']}/중앙 {q['median']}/상위25% {q['top25']}/상위10% {q['top10']})")
        lines.append(f"- {r['name']} (캠페인 {r['n']}개, 지출 {r['spend']}): " + ", ".join(parts))
    return "\n".join(lines)
