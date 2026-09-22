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
              "D": "DV360", "T": "TikTok", "V": "영상(YouTube)", "ALL": "전체(매체통합)",
              "GD": "Google"}
# 매체 묶음 — Google Ads(G) + DV360(D)을 'Google'(GD)로 통합 표시·집계
MEDIA_GROUP = {"GD": ("G", "D")}
MEDIA_GROUP_OF = {m: g for g, ms in MEDIA_GROUP.items() for m in ms}
# KPI 정의: alias → (캠페인단위 SQL식, 낮을수록좋음, 표시포맷)
KPI_EXPR = {
    "cpm": "SAFE_DIVIDE(cost,imp)*1000", "cpc": "SAFE_DIVIDE(cost,clk)",
    "ctr": "SAFE_DIVIDE(clk,imp)*100",
    # CVR 은 '구매 계층' 전환 기준이다. 혼합 conv 로 계산하면 Google 42%·TikTok 76% 처럼
    # 전환율로 읽힐 수 없는 값이 나온다(google 전환의 92.7%가 참여, tiktok 은 목표달성 수).
    # 혼합값은 cvr_all 로 이름을 갈라 참고용으로만 둔다.
    "cvr": "SAFE_DIVIDE(conv_pur,clk)*100",
    "cvr_all": "SAFE_DIVIDE(conv,clk)*100",
    "roas": "SAFE_DIVIDE(rev,cost)",
    # 영상(YouTube) 지표 — 영상 캠페인 분모(vimp=영상노출, vcost=영상비용)로 산출 → 비영상 캠페인 희석 없음.
    "vtr": "SAFE_DIVIDE(vviews,vimp)*100", "cpv": "SAFE_DIVIDE(vcost,vviews)",
    "cr": "SAFE_DIVIDE(vp100,vimp)*100",   # 완전조회율 = 영상노출 대비 끝까지 재생(≤100%)
    "cpv100": "SAFE_DIVIDE(vcost,vp100)",  # 100%(완전)조회 기준 CPV — '기준 지표' 셀렉터(차트 전용)
    "vtr3s": "SAFE_DIVIDE(mv3s,imp)*100", "cpv3s": "SAFE_DIVIDE(cost,mv3s)",   # Meta 3초조회 기준 VTR/CPV
    "vtrthru": "SAFE_DIVIDE(vthru,vimp)*100", "cpvthru": "SAFE_DIVIDE(vcost,vthru)",  # ThruPlay 기준(Meta영상)
    "ctrlk": "SAFE_DIVIDE(mlclk,imp)*100", "cpclk": "SAFE_DIVIDE(cost,mlclk)",  # 링크클릭 기준 CTR/CPC(Meta)
}
KPI_LOWER_BETTER = {"cpm": True, "cpc": True, "ctr": False, "cvr": False, "cvr_all": False, "roas": False,
                    "vtr": False, "cpv": True, "cr": False, "cpv100": True,
                    "vtr3s": False, "cpv3s": True, "vtrthru": False, "cpvthru": True, "ctrlk": False, "cpclk": True}
KPI_FMT = {"cpm": "money2", "cpc": "money2", "ctr": "pct", "cvr": "pct", "cvr_all": "pct", "roas": "x",
           "vtr": "pct", "cpv": "money2", "cr": "pct", "cpv100": "money2",
           "vtr3s": "pct", "cpv3s": "money2", "vtrthru": "pct", "cpvthru": "money2", "ctrlk": "pct", "cpclk": "money2"}
KPIS_DEFAULT = ("cpm", "cpc", "ctr", "cvr", "roas")
# 영상 KPI — 영상 캠페인 보유 매체(Google)에서 커버리지 게이트(≥10%)로 노출. 비영상 매체는 자동 숨김.
VIDEO_KPIS = ("vtr", "cpv")
# 차트 '기준 지표(basis)' 전용 — 4분위/트렌드/비교 시리즈는 계산하되 표·Rate/KPI토글엔 미노출.
# cr=100%조회VTR, vtr3s/cpv3s=Meta 3초조회 기준, ctrlk/cpclk=링크클릭 기준.
CHART_ONLY_KPIS = ("cpv100", "cr", "vtr3s", "cpv3s", "vtrthru", "cpvthru", "ctrlk", "cpclk", "cvr_all")
# 캠페인 마트(TBL)에만 존재하는 보강 집계 컬럼(영상=v*, Meta=m*) — 세그먼트 마트엔 없음.
EXTRA_COLS = ("vimp", "vviews", "vcost", "vp25", "vp50", "vp75", "vp100", "veng", "vthru",
              "mlclk", "mv3s", "meng", "mcmt", "mrct", "mlead", "mshare")
KPIS = KPIS_DEFAULT   # 하위호환(타 모듈 참조)

# 벤치마크로 신뢰할 수 있는 최소 표본(비교군 캠페인 수).
# 이보다 적으면 중앙값·상위25%/10% 가 캠페인 한둘에 흔들린다 → 화면에 경고 배지를 붙인다.
#
# 20 은 A1 솔루션과 맞춘 값이다. 같은 데이터로 한쪽은 '상위 10%', 다른 쪽은 '견줄 수 없음'이
# 나오면 사용자가 혼란스럽기 때문이다. A1 실측 근거: KR 업종 10개 중 20~29 구간은 '게임 23'
# 하나뿐이고, 23개 캠페인은 중앙값을 말할 만하다(캠페인 3개로 '상위 10%'를 말하는 것과 다르다).
#
# ※ 임계값만 맞추고 '무엇을 하는가'는 각자 다르다 —
#   A1 은 문장으로 답하므로 아예 막고(comparable:false), 여기는 표를 보는 화면이라 배지를 붙인다.
N_MIN_RELIABLE = 20

# 행 단위 커버리지 하한 — 전체 게이트(10%)와 같은 값이지만 '행'에 적용한다.
# 전체 게이트만 있으면 개별 업종·브랜드의 구멍을 못 막는다. 측정된 캠페인이 이보다 적으면
# '성과가 0' 이 아니라 '견줄 수 없음' 으로 낸다 — 「없다」와 「0이다」는 다른 말이다.
COVER_MIN_ROW = 0.10


def _agg_kpi(k, imp, clk, cost, conv, rev, vv, vp, vimp=0, vcost=0.0, conv_pur=0):
    """합계 지표로부터 KPI 집계값(표시용). 영상지표는 영상 분모(vimp/vcost) 사용.
    cvr 은 구매 계층(conv_pur), cvr_all 은 혼합(conv) 기준이다."""
    if k == "cpm":  return cost / imp * 1000 if imp else 0
    if k == "cpc":  return cost / clk if clk else 0
    if k == "ctr":  return clk / imp * 100 if imp else 0
    if k == "cvr":  return (conv_pur or 0) / clk * 100 if clk else 0
    if k == "cvr_all": return conv / clk * 100 if clk else 0
    if k == "roas": return rev / cost if cost else 0
    if k == "vtr":  return vv / vimp * 100 if vimp else 0
    if k == "cpv":  return vcost / vv if vv else 0
    if k == "cr":   return vp / vimp * 100 if vimp else 0
    if k == "cpv100": return vcost / vp if vp else 0
    return 0


def _agg_extra(k, imp, cost, conv, ex):
    """보강(Meta·링크·참여) 표시지표 집계값. ex=EXTRA_COLS 합계 dict."""
    if k == "vtr3s":  return ex["mv3s"] / imp * 100 if imp else 0
    if k == "cpv3s":  return cost / ex["mv3s"] if ex["mv3s"] else 0
    if k == "vtrthru": return ex["vthru"] / ex["vimp"] * 100 if ex["vimp"] else 0
    if k == "cpvthru": return ex["vcost"] / ex["vthru"] if ex["vthru"] else 0
    if k == "ctrlk":  return ex["mlclk"] / imp * 100 if imp else 0
    if k == "cpclk":  return cost / ex["mlclk"] if ex["mlclk"] else 0
    if k == "cpa":    return cost / conv if conv else 0
    if k == "engr":   return (ex["veng"] + ex["meng"]) / imp * 100 if imp else 0
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
    clauses = ["period BETWEEN @p0 AND @p1"]
    params = [
        bigquery.ScalarQueryParameter("p0", "STRING", p0),
        bigquery.ScalarQueryParameter("p1", "STRING", p1),
    ]
    mu = str(media).upper() if media else ""
    if mu and mu != "ALL":   # ALL=전체 매체 통합(매체 필터 생략)
        if mu in MEDIA_GROUP:   # 묶음(예: GD=Google+DV360) → IN 절(코드는 화이트리스트 상수라 안전)
            codes = ",".join("'%s'" % c for c in MEDIA_GROUP[mu])
            clauses.insert(0, f"media IN ({codes})")
        else:
            clauses.insert(0, "media=@media")
            params.append(bigquery.ScalarQueryParameter("media", "STRING", media))
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
    if dim in SEGMENT_TBL and not _segment_available(dim):
        return {"benchmark": [], "detail": [], "charts": None, "meta": {
            "media": media, "media_name": MEDIA_NAME.get(media, media), "available": False,
            "dim": dim, "note": f"{DIMS.get(dim, dim)} 데이터 준비 중입니다 (통합뷰 추가 대기)."}}
    src = SEGMENT_TBL.get(dim, TBL)
    has_video = (src == TBL)   # 영상 집계 컬럼은 캠페인 마트에만 존재(세그먼트 마트엔 없음)
    if src != TBL:   # 세그먼트 소스엔 channel/agency 컬럼 없음 → 해당 필터 제거
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

    # 영상 KPI는 영상캠페인 보유 매체(캠페인 마트=Google)에서만 계산. 노출은 커버리지 게이트로 결정.
    table_kpis = list(KPIS_DEFAULT) + (list(VIDEO_KPIS) if has_video else [])
    calc_kpis = list(table_kpis) + (list(CHART_ONLY_KPIS) if has_video else [])

    def _qblock(k):
        lower = KPI_LOWER_BETTER[k]
        o25, o10 = (25, 10) if lower else (75, 90)
        if k == "roas":
            e = "IF(rev>0,roas,NULL)"
        elif k in ("vtr", "cpv", "cr", "cpv100"):
            e = f"IF(vimp>0,{k},NULL)"      # 영상(Google) 캠페인만
        elif k in ("vtr3s", "cpv3s"):
            e = f"IF(mv3s>0,{k},NULL)"      # Meta 3초조회 캠페인만
        elif k in ("vtrthru", "cpvthru"):
            e = f"IF(vthru>0,{k},NULL)"     # ThruPlay 있는 영상 캠페인만
        elif k in ("ctrlk", "cpclk"):
            e = f"IF(mlclk>0,{k},NULL)"     # 링크클릭 있는 캠페인만
        else:
            e = k
        return (f"AVG({e}) {k}_avg, APPROX_QUANTILES({e},100)[OFFSET(50)] {k}_median, "
                f"APPROX_QUANTILES({e},100)[OFFSET({o25})] {k}_top25, "
                f"APPROX_QUANTILES({e},100)[OFFSET({o10})] {k}_top10")

    cl = _client()
    where, params = _filter_clauses(media, p0, p1, filters)
    qcfg = bigquery.QueryJobConfig(query_parameters=params)

    vcols_sel = (", " + ", ".join(f"SUM({c}) {c}" for c in EXTRA_COLS)) if has_video else ""
    vcols_pass = (", " + ", ".join(EXTRA_COLS)) if has_video else ""
    vcols_out = (", " + ", ".join(f"SUM({c}) {c}" for c in EXTRA_COLS)
                 + ", COUNTIF(vimp>0) nvid, COUNTIF(mv3s>0) nv3s, COUNTIF(vthru>0) nthru, COUNTIF(mlclk>0) nlclk, "
                 + "COUNTIF(veng+meng>0) neng, COUNTIF(meng>0) nmeng") if has_video else ""
    ck_exprs = ", ".join(f"{KPI_EXPR[k]} {k}" for k in calc_kpis)
    qcols = ", ".join(_qblock(k) for k in calc_kpis)
    camp_having = "imp >= 1000 AND clk > 0"

    # 1) 기준차원별 4분위 + 합계 (캠페인 단위 분포)
    bench_sql = f"""
    WITH camp AS (
      SELECT {dim} AS dim, campaign_id,
        SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, SUM(conv) conv, SUM(conv_pur) conv_pur, SUM(rev) rev{vcols_sel}
      FROM {src} WHERE {where}
      GROUP BY dim, campaign_id HAVING {camp_having}
    ),
    ck AS (
      SELECT dim, imp, clk, cost, conv, conv_pur, rev{vcols_pass}, {ck_exprs}
      FROM camp
    )
    SELECT dim, COUNT(*) n, COUNTIF(rev>0) nrev, COUNTIF(conv_pur>0) nconv, COUNTIF(conv>0) nconv_all, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, SUM(conv) conv, SUM(conv_pur) conv_pur, SUM(rev) rev{vcols_out},
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

    def _ex(r):  # EXTRA_COLS 합계 dict (없으면 0)
        return {c: (r.get(c) or 0) for c in EXTRA_COLS}

    def _kval(k, imp, clk, cost, conv, rev, ex, conv_pur=0):  # KPI 집계값(영상=v*, Meta=m* 분모)
        if k in ("vtr3s", "cpv3s", "vtrthru", "cpvthru", "ctrlk", "cpclk"):
            return _agg_extra(k, imp, cost, conv, ex)
        return _agg_kpi(k, imp, clk, cost, conv, rev, ex["vviews"], ex["vp100"],
                        ex["vimp"], ex["vcost"], conv_pur)

    def _extra_disp(ex, imp, cost, conv):  # 지표추가 표시(비KPI 컬럼): 조회수·참여·링크·전환비용 등
        imp = imp or 0; vimp = ex["vimp"] or 0; engsum = (ex["veng"] or 0) + (ex["meng"] or 0)
        return {"views": _num(ex["vviews"]), "v25": _num(ex["vp25"]), "v50": _num(ex["vp50"]),
                "v75": _num(ex["vp75"]), "v100": _num(ex["vp100"]),
                "vtr25": _pct(ex["vp25"] / vimp * 100 if vimp else 0),
                "vtr50": _pct(ex["vp50"] / vimp * 100 if vimp else 0),
                "vtr75": _pct(ex["vp75"] / vimp * 100 if vimp else 0),
                "v3s": _num(ex["mv3s"]), "vthru": _num(ex["vthru"]), "lclk": _num(ex["mlclk"]),
                "eng": _num(engsum), "engr": _pct(engsum / imp * 100 if imp else 0),
                "cmt": _num(ex["mcmt"]), "rct": _num(ex["mrct"]), "lead": _num(ex["mlead"]), "shr": _num(ex["mshare"]),
                "cpa": money2(cost / conv if conv else 0)}

    benchmark = []
    tot = {"imp": 0, "clk": 0, "cost": 0.0, "n": 0, "rev": 0.0, "conv": 0.0, "conv_pur": 0.0}
    tex = {c: 0 for c in EXTRA_COLS}   # EXTRA 컬럼 합계(전체 행)
    tot_nrev = tot_nconv = tot_nvid = tot_nv3s = tot_nthru = tot_nlclk = tot_neng = tot_nmeng = 0
    for r in rows:
        imp, clk, cost, conv, rev = r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0, r.get("conv") or 0.0, r["rev"] or 0.0
        conv_pur = r.get("conv_pur") or 0.0
        ex = _ex(r)
        tot["conv_pur"] += conv_pur
        tot["imp"] += imp; tot["clk"] += clk; tot["cost"] += cost; tot["n"] += r["n"]; tot["rev"] += rev; tot["conv"] += conv
        tot_nrev += (r.get("nrev") or 0); tot_nconv += (r.get("nconv") or 0); tot_nvid += (r.get("nvid") or 0)
        tot_nv3s += (r.get("nv3s") or 0); tot_nthru += (r.get("nthru") or 0); tot_nlclk += (r.get("nlclk") or 0)
        tot_neng += (r.get("neng") or 0); tot_nmeng += (r.get("nmeng") or 0)
        if has_video:
            for c in EXTRA_COLS:
                tex[c] += ex[c]
        row = {"dim": r["dim"], "name": dim_name(dim, r["dim"]), "n": r["n"],
               # 표본이 적으면 중앙값·분위수가 흔들린다. 화면에서 경고 표시를 붙이기 위한 플래그.
               "n_low": (r["n"] or 0) < N_MIN_RELIABLE,
               "imp": _num(imp), "clicks": _num(clk), "spend": money(cost), "conv": _num(conv)}
        # 행 단위 '미측정' 판정 — 커버리지 게이트는 전체 합계 기준이라 개별 행의 구멍을 못 막는다.
        # 예) media=G·dim=industry 의 '게임' 은 전환커버리지 62.5% 로 게이트를 통과하는데 매출이 0이다.
        #     그대로 두면 'ROAS 0.00배' 가 찍히고, 읽는 사람은 '매출이 전혀 없었다' 로 읽는다.
        #     실제로는 '매출을 못 잰다'(추적 미설정)이다. 둘은 다른 말이라 0 대신 null 로 낸다.
        nrev_r, nconv_r = (r.get("nrev") or 0), (r.get("nconv") or 0)
        n_r = r["n"] or 1
        for k in calc_kpis:
            row[k] = qf(k, _kval(k, imp, clk, cost, conv, rev, ex, conv_pur))
            row[k + "_q"] = {q: qf(k, r.get(f"{k}_{q}")) for q in ("avg", "median", "top25", "top10")}
            na = None
            if k == "roas":
                if nrev_r == 0 or not rev:
                    na = "구매 매출이 기록되지 않았습니다(전환은 있으나 매출 추적 미설정으로 보입니다)"
                elif nrev_r / n_r < COVER_MIN_ROW:
                    # 예) naver 브랜드 17개 중 1개만 매출 기록(₩3,624 / ₩106M) → ROAS 0.00배로 찍힌다.
                    #     측정된 캠페인이 너무 적어 '성과가 0' 이 아니라 '견줄 수 없음' 이다.
                    na = (f"매출을 추적한 캠페인이 {nrev_r}/{n_r}개({nrev_r/n_r*100:.0f}%)뿐이라 "
                          f"비교할 수 없습니다")
            elif k == "cvr":
                if nconv_r == 0 or not conv:
                    na = "전환이 기록되지 않았습니다(추적 미설정으로 보입니다)"
                elif nconv_r / n_r < COVER_MIN_ROW:
                    na = (f"전환을 추적한 캠페인이 {nconv_r}/{n_r}개({nconv_r/n_r*100:.0f}%)뿐이라 "
                          f"비교할 수 없습니다")
            if na:
                row[k] = None
                row[k + "_q"] = {q: None for q in ("avg", "median", "top25", "top10")}
                row[k + "_na"] = na
        if has_video:
            row.update(_extra_disp(ex, imp, cost, conv))
        benchmark.append(row)
    total = {"dim": "TOTAL", "name": "전체", "n": tot["n"], "imp": _num(tot["imp"]),
             "clicks": _num(tot["clk"]), "spend": money(tot["cost"]), "conv": _num(tot["conv"]), "cls": "ttl"}
    for k in calc_kpis:
        total[k] = qf(k, _kval(k, tot["imp"], tot["clk"], tot["cost"], tot["conv"], tot["rev"], tex, tot["conv_pur"]))
        # cvr 은 구매 계층(conv_pur) 기준이므로 가드도 그 값을 본다. 혼합 conv 를 보면
        # Meta·TikTok 처럼 혼합 전환은 있고 구매 전환은 0인 매체가 '0.00%' 로 새어 나간다.
        if (k == "roas" and not tot["rev"]) or (k == "cvr" and not tot["conv_pur"])                 or (k == "cvr_all" and not tot["conv"]):
            total[k] = None
            total[k + "_na"] = ("구매 매출이 기록되지 않았습니다" if k == "roas"
                                else "전환이 기록되지 않았습니다")
    if has_video:
        total.update(_extra_disp(tex, tot["imp"], tot["cost"], tot["conv"]))
    # 커버리지 게이트(≥10%) — 추적/보강 캠페인이 충분할 때만 노출(오해성 0값 방지).
    n_all = tot["n"] or 1
    roas_avail = (tot_nrev / n_all) >= 0.10
    cvr_avail = (tot_nconv / n_all) >= 0.10
    # 영상 지표는 영상 캠페인만(IF vimp>0 등)으로 산출 → 희석 없음. %가 아니라 '충분한 캠페인 수' 게이트(≥20).
    vid_avail = has_video and tot_nvid >= 20        # 영상: VTR/CPV/완전조회율/구간(Google·Meta 영상캠페인)
    metaview_avail = has_video and tot_nv3s >= 20   # Meta 3초조회
    thru_avail = has_video and tot_nthru >= 20      # ThruPlay(Meta 영상)
    link_avail = has_video and (tot_nlclk / n_all) >= 0.10      # 링크클릭(Meta)
    engage_avail = has_video and (tot_neng / n_all) >= 0.10     # 게시물 참여(Google영상/Meta)
    meta_eng_avail = has_video and (tot_nmeng / n_all) >= 0.10  # 댓글·공감·잠재고객(Meta 전용)

    # 2) detail (월 × 기준차원)
    detail = []
    det_sql = f"""
      SELECT period, {dim} AS dim, COUNT(*) n, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost,
             SUM(conv) conv, SUM(conv_pur) conv_pur, SUM(rev) rev{vcols_sel}
      FROM {src} WHERE {where} GROUP BY period, dim HAVING imp > 0
      ORDER BY period DESC, cost DESC
    """
    for r in cl.query(det_sql, job_config=qcfg).result():
        imp, clk, cost = r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0
        conv, rev = r.get("conv") or 0.0, r.get("rev") or 0.0
        conv_pur = r.get("conv_pur") or 0.0
        ex = _ex(r)
        d = {"period": r["period"], "name": dim_name(dim, r["dim"]),
             "n": r["n"], "n_low": (r["n"] or 0) < N_MIN_RELIABLE,
             "spend": money(cost), "imps": _num(imp), "clicks": _num(clk),
             "cpm": money2(cost / imp * 1000 if imp else 0),
             "cpc": money2(cost / clk if clk else 0),
             "ctr": _pct(clk / imp * 100 if imp else 0),
             "conv": _num(conv), "cvr": _pct(conv / clk * 100 if clk else 0),
             "roas": (f"{(rev / cost / gf):.2f}배" if cost else "—")}
        if has_video:
            for k in ("vtr", "cpv", "cr", "cpv100", "vtr3s", "cpv3s", "vtrthru", "cpvthru", "ctrlk", "cpclk"):
                d[k] = qf(k, _kval(k, imp, clk, cost, conv, rev, ex, conv_pur))
            d.update(_extra_disp(ex, imp, cost, conv))
        detail.append(d)

    # 3) charts: trend(월별, 조건 전체) + compare(기준차원별 중앙값)
    months = sorted({d["period"] for d in detail})
    trend = {"labels": months}
    for k in calc_kpis:
        trend[k] = []
    mt = {m: None for m in months}
    for r in cl.query(f"SELECT period, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost, "
                      f"SUM(conv) conv, SUM(conv_pur) conv_pur, SUM(rev) rev{vcols_sel} "
                      f"FROM {src} WHERE {where} GROUP BY period", job_config=qcfg).result():
        mt[r["period"]] = (r["imp"] or 0, r["clk"] or 0, r["cost"] or 0.0, r["conv"] or 0.0,
                           r["rev"] or 0.0, _ex(r), r["conv_pur"] or 0.0)
    for m in months:
        if not mt[m]:
            for k in calc_kpis:
                trend[k].append(0)
            continue
        imp, clk, cost, conv, rev, ex, conv_pur_m = mt[m]
        for k in calc_kpis:
            v = _kval(k, imp, clk, cost, conv, rev, ex, conv_pur_m)
            if KPI_FMT[k].startswith("money"):
                trend[k].append(round(v * gf / rate, 2))
            elif k == "roas":
                trend[k].append(round(v / gf, 2))
            else:
                trend[k].append(round(v, 2))
    top = benchmark[:15]   # 비교 차트 노출 항목 수(국가 등) — 시연용 폭넓게
    compare = {"labels": [b["name"] for b in top]}
    for k in calc_kpis:
        med = lambda b, _k=k: next((r[f"{_k}_median"] for r in rows if r["dim"] == b["dim"]), 0) or 0
        if KPI_FMT[k].startswith("money"):
            compare[k] = [round(med(b) * gf / rate, 2) for b in top]
        elif k == "roas":
            compare[k] = [round(med(b) / gf, 2) for b in top]
        else:
            compare[k] = [round(med(b), 2) for b in top]

    # 표·토글 노출 KPI: 코어(CPM/CPC/CTR) + 커버리지 통과한 CVR/ROAS + 영상(VTR/CPV).
    meta_kpis = (["cpm", "cpc", "ctr"] + (["cvr"] if cvr_avail else []) + (["roas"] if roas_avail else [])
                 + (list(VIDEO_KPIS) if vid_avail else []))
    all_kpis = ["cpm", "cpc", "ctr", "cvr", "roas"] + (list(VIDEO_KPIS) if vid_avail else [])
    # 지표추가 카탈로그 가용 키 — 프론트가 이 목록으로 활성/비활성 판정(데이터 없는 건 자동 비활성)
    avail_metrics = ["imp", "cpm", "spend", "clicks", "ctr", "cpc"]
    if cvr_avail: avail_metrics += ["conv", "cvr", "cpa"]
    if roas_avail: avail_metrics += ["roas"]
    if vid_avail: avail_metrics += ["views", "vtr", "cpv", "cr", "cpv100", "v25", "v50", "v75", "v100", "vtr25", "vtr50", "vtr75"]
    if metaview_avail: avail_metrics += ["v3s", "vtr3s", "cpv3s"]
    if thru_avail: avail_metrics += ["vthru", "vtrthru", "cpvthru"]
    if link_avail: avail_metrics += ["lclk", "ctrlk", "cpclk"]
    if engage_avail: avail_metrics += ["eng", "engr"]
    if meta_eng_avail: avail_metrics += ["cmt", "rct", "lead", "shr"]
    # VTR/CPV 기준(basis) 가용: tv/100=영상(Google), 3s=Meta3초; 링크클릭 기준=link_avail
    video_bases = (["tv", "100"] if vid_avail else []) + (["3s"] if metaview_avail else []) + (["thru"] if thru_avail else [])
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
            "kpis": meta_kpis, "all_kpis": all_kpis, "avail_metrics": avail_metrics,
            "roas_available": roas_avail, "cvr_available": cvr_avail, "video_available": vid_avail,
            "metaview_available": metaview_avail, "link_available": link_avail, "engage_available": engage_avail,
            "video_bases": video_bases,
            "roas_coverage": round(tot_nrev / n_all, 3), "conv_coverage": round(tot_nconv / n_all, 3),
            "video_coverage": round(tot_nvid / n_all, 3), "is_video": False,
            # 표본 신뢰도 — 화면이 소표본 경고를 붙일 수 있게 임계와 집계를 함께 준다.
            "n_min_reliable": N_MIN_RELIABLE,
            "n_total": tot["n"],
            "n_low_dims": sum(1 for b in benchmark if b.get("n_low")),
            # 데이터 신선도 — '조회는 되는데 낡은' 상태를 막는다(14_STALE_MARTS 와 같은 사고 방지).
            "freshness": _freshness(),
            # 지표 정의 주의 — 화면이 각주로 띄운다. DB 사전(dictionary_column_notes) 근거.
            "metric_caveats": ({"cvr": "전환수는 매체별 정의가 섞여 있습니다"
                                       "(google_ads 는 구매·리드·참여를 같은 전환으로 더함). "
                                       "매체 간 CVR 직접 비교는 피하십시오."} if cvr_avail else {}),
            "roas_basis": "revenue_purchase_krw (구매 계층 전환가치)",
            "fx": {"asof": fx_asof, "USD": round(fx_rates.get("USD", 0), 2),
                   "EUR": round(fx_rates.get("EUR", 0), 2), "JPY": round(fx_rates.get("JPY", 0), 2),
                   "CNY": round(fx_rates.get("CNY", 0), 2), "INR": round(fx_rates.get("INR", 0), 2)},
            "note": ("다차원 벤치마크. 데이터 ~99% 현대·기아 자동차."
                     + (" 영상(YouTube) 지표는 Google 영상 캠페인 기준." if vid_avail else "")),
        },
    }
    if len(_BENCH_CACHE) > 300:
        _BENCH_CACHE.clear()
    _BENCH_CACHE[ckey] = result
    return result


@lru_cache(maxsize=16)
def _table_cols(tbl_ref):
    """백틱 테이블 참조의 컬럼 집합(소문자). 조회 실패 시 None → 호출부는 스킵 안 함."""
    try:
        ref = tbl_ref.strip("`")
        return {f.name for f in _client().get_table(ref).schema}
    except Exception:
        return None


@lru_cache(maxsize=1)
def _video_media_available():
    try:
        return list(_client().query(f"SELECT COUNT(*) n FROM {VIDEO_TBL}").result())[0]["n"] > 0
    except Exception:
        return False


@lru_cache(maxsize=8)
def get_filter_options(media="G"):
    """필터 드롭다운용 — 매체별 차원 distinct 값."""
    cl = _client()
    out = {}
    is_all = str(media).upper() == "ALL"
    osrc = TBL
    mw = "" if is_all else "media=@m AND "
    mp = [] if is_all else [bigquery.ScalarQueryParameter("m", "STRING", media)]
    cols = ("market", "objective", "brand", "industry", "agency", "channel")
    have = _table_cols(osrc)   # 마트/코드 버전 스큐 방어 — 마트에 없는 컬럼(예: 미빌드 channel)은 건너뜀
    for col in cols:
        out[col] = []
        if have and col not in have:
            continue
        try:
            rows = cl.query(
                f"SELECT {col} v, SUM(cost) s FROM {osrc} WHERE {mw}{col} IS NOT NULL "
                f"GROUP BY 1 ORDER BY s DESC LIMIT 50",
                job_config=bigquery.QueryJobConfig(query_parameters=list(mp))).result()
            out[col] = [{"v": r["v"], "name": dim_name(col, r["v"])} for r in rows if r["v"]]
        except Exception:
            pass
    # 세그먼트 차원 가용성
    for seg, tbl in SEGMENT_TBL.items():
        out[seg + "_available"] = False
        if _segment_available(seg):
            try:
                n = list(cl.query(
                    f"SELECT COUNT(*) n FROM {tbl} WHERE {mw}1=1",
                    job_config=bigquery.QueryJobConfig(query_parameters=list(mp))).result())[0]["n"]
                out[seg + "_available"] = n > 0
            except Exception:
                pass
    # 광고상품(channel) 가용성 — '(기타)' 외 실제 채널유형이 있을 때
    out["channel_available"] = False
    try:
        n = list(cl.query(
            f"SELECT COUNT(DISTINCT channel) n FROM {TBL} WHERE {mw}channel!='(기타)'",
            job_config=bigquery.QueryJobConfig(query_parameters=list(mp))).result())[0]["n"]
        out["channel_available"] = n > 0
    except Exception:
        pass
    return out


@lru_cache(maxsize=4)
def _freshness_cached(day):
    """마트 최신 집행월 · 빌드 시각. day = 날짜키(일일 캐시 무효화)."""
    try:
        r = list(_client().query(
            f"SELECT MAX(period) latest, MIN(period) earliest, MAX(_built_at) built FROM {TBL}"
        ).result())[0]
        return {"latest_period": r["latest"], "earliest_period": r["earliest"],
                "built_at": r["built"].isoformat() if r["built"] else None}
    except Exception:   # 신선도 조회 실패가 본 응답을 깨지 않도록
        return {"latest_period": None, "earliest_period": None, "built_at": None}


def _freshness():
    import datetime
    return _freshness_cached(datetime.date.today().isoformat())


def percentile_rank(metric="cpm", value=0.0, media="G", date_from="2025-01-01",
                    date_to="2026-12-31", market="", objective="", brand="",
                    industry="", agency="", channel=""):
    """'내 캠페인이 벤치마크 어디쯤인가' — 벤치마크 도구의 핵심 기능.

    입력한 지표 값이 같은 조건 캠페인 분포에서 몇 번째인지 돌려준다.
    CPM·CPC 처럼 낮을수록 좋은 지표는 방향을 뒤집어 '상위 %'를 계산한다.
    """
    if metric not in KPI_EXPR:
        raise ValueError(f"지원하지 않는 지표: {metric}")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("value 는 숫자여야 합니다")
    cl = _client()
    p0, p1 = date_from[:7], date_to[:7]
    filters = {"market": market, "objective": objective, "brand": brand,
               "industry": industry, "agency": agency, "channel": channel}
    where, params = _filter_clauses(media, p0, p1, filters)
    expr = KPI_EXPR[metric]
    lower_better = KPI_LOWER_BETTER.get(metric, False)
    # 분모는 '그 지표가 계산되는 캠페인'만 — 0/NULL 캠페인이 섞이면 순위가 부풀려진다.
    better = "<" if lower_better else ">"
    # 중앙값 95% 신뢰구간 — 순서통계량 기반(분포 가정 없음).
    # 표본 n 에서 중앙값의 백분위 위치는 50 ± 1.96*50/sqrt(n) 안에 들어간다.
    #   n=20  → p28~p72 (매우 넓다)   n=100 → p40~p60   n=500 → p46~p54
    # 이것이 N_MIN_RELIABLE 과 커버리지 게이트의 '관행값'을 대체할 실제 근거다.
    q = f"""
      WITH b AS (SELECT {expr} v FROM {TBL} WHERE {where}),
           c AS (SELECT v FROM b WHERE v IS NOT NULL AND NOT IS_NAN(v) AND v > 0),
           k AS (SELECT COUNT(*) n FROM c)
      SELECT (SELECT n FROM k) n,
             (SELECT COUNTIF(v {better} @val) FROM c) n_better,
             APPROX_QUANTILES(v,100)[OFFSET(50)] p50,
             APPROX_QUANTILES(v,100)[OFFSET({75 if lower_better else 25})] p_top25,
             APPROX_QUANTILES(v,100)[OFFSET({90 if lower_better else 10})] p_top10,
             APPROX_QUANTILES(v,100)[OFFSET(
               GREATEST(1, CAST(ROUND(50 - 98/SQRT((SELECT n FROM k))) AS INT64)))] ci_lo,
             APPROX_QUANTILES(v,100)[OFFSET(
               LEAST(99, CAST(ROUND(50 + 98/SQRT((SELECT n FROM k))) AS INT64)))] ci_hi
      FROM c
    """
    params = list(params) + [bigquery.ScalarQueryParameter("val", "FLOAT64", value)]
    r = list(cl.query(q, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())[0]
    n = r["n"] or 0
    if n == 0:
        return {"available": False, "reason": "해당 조건에 비교군 캠페인이 없습니다.",
                "metric": metric, "value": value}
    # '상위 X%' = 나보다 나은 캠페인 비율
    top_pct = round((r["n_better"] or 0) / n * 100, 1)
    if top_pct <= 10:
        grade, grade_label = "top10", "상위 10% 이내"
    elif top_pct <= 25:
        grade, grade_label = "top25", "상위 25% 이내"
    elif top_pct <= 50:
        grade, grade_label = "above", "중앙값 이상"
    else:
        grade, grade_label = "below", "중앙값 미만"
    sym, rate = _currency(None)[0], 1.0
    return {
        "available": True, "metric": metric, "value": value,
        "n": n, "n_low": n < N_MIN_RELIABLE,
        "n_min_reliable": N_MIN_RELIABLE,
        "top_pct": top_pct, "grade": grade, "grade_label": grade_label,
        "lower_better": lower_better,
        "median": r["p50"], "top25": r["p_top25"], "top10": r["p_top10"],
        # 중앙값이 표본 때문에 얼마나 흔들리는지 — '이 아래는 읽지 말라'를 숫자로 보여준다
        "median_ci": {"lo": r["ci_lo"], "hi": r["ci_hi"],
                      "pct_band": round(2 * 98 / (n ** 0.5), 1),
                      "wobble_pct": (round(abs(r["ci_hi"] - r["ci_lo"]) / r["p50"] * 100, 1)
                                     if r["p50"] else None),
                      "method": "순서통계량 95% 신뢰구간 (분포 가정 없음)"},
        "note": ("표본이 적어 순위가 흔들릴 수 있습니다." if n < N_MIN_RELIABLE
                 else None),
        "media": media, "media_name": MEDIA_NAME.get(media, media),
        "date_from": date_from, "date_to": date_to,
    }


def _shift_ym(ym, months):
    y, m = int(ym[:4]), int(ym[5:7])
    t = (y * 12 + (m - 1)) - months
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def period_compare(media="G", dim="industry", date_from="2025-01-01", date_to="2026-12-31",
                   mode="prev", **filters):
    """기간 간 비교 — 전기(prev) 또는 전년 동기(yoy) 대비 증감.

    CPM·CPC 는 계절성이 커서 한 기간만 보면 '지금 어디쯤인가'를 잘못 읽는다.
    (A1 이 14_STALE_MARTS 에서 지적한 그 위험 — 6월 값으로 9월을 판단하는 것.)

    ★ 비교군 수(n)를 함께 낸다. 비교군이 달라지면 증감이 뜻을 잃기 때문이다.
      예) 전기 n=12 → 이번 n=480 이면 'CPM 40% 상승'은 모집단이 바뀐 결과일 수 있다.
    """
    if dim not in DIMS:
        dim = "industry"
    src = SEGMENT_TBL.get(dim, TBL)
    p0, p1 = date_from[:7], date_to[:7]
    span = (int(p1[:4]) * 12 + int(p1[5:7])) - (int(p0[:4]) * 12 + int(p0[5:7])) + 1
    back = 12 if mode == "yoy" else span
    q0, q1 = _shift_ym(p0, back), _shift_ym(p1, back)
    if src != TBL:
        filters = {k: v for k, v in filters.items() if k not in CAMPAIGN_ONLY_FILTERS}
    cl = _client()

    def snap(a, b):
        where, params = _filter_clauses(media, a, b, filters)
        rows = list(cl.query(f"""
          WITH camp AS (
            SELECT {dim} AS dim, campaign_id, SUM(imp) imp, SUM(clk) clk,
                   SUM(cost) cost, SUM(conv_pur) conv_pur, SUM(rev) rev
            FROM {src} WHERE {where}
            GROUP BY dim, campaign_id HAVING imp >= 1000 AND clk > 0
          )
          SELECT dim, COUNT(*) n, SUM(imp) imp, SUM(clk) clk, SUM(cost) cost,
                 SUM(conv_pur) conv_pur, SUM(rev) rev,
                 COUNTIF(rev>0) nrev, COUNTIF(conv_pur>0) nconv
          FROM camp WHERE dim IS NOT NULL GROUP BY dim
        """, job_config=bigquery.QueryJobConfig(query_parameters=list(params))).result())
        out = {}
        for r in rows:
            imp, clk, cost = (r["imp"] or 0), (r["clk"] or 0), (r["cost"] or 0.0)
            n = r["n"] or 0
            m = {"n": n,
                 "cpm": (cost / imp * 1000) if imp else None,
                 "cpc": (cost / clk) if clk else None,
                 "ctr": (clk / imp * 100) if imp else None}
            # 커버리지가 얕으면 증감을 내지 않는다 — 본 응답과 같은 규칙
            m["cvr"] = ((r["conv_pur"] or 0) / clk * 100) if (clk and n and (r["nconv"] or 0) / n >= COVER_MIN_ROW) else None
            m["roas"] = ((r["rev"] or 0) / cost) if (cost and n and (r["nrev"] or 0) / n >= COVER_MIN_ROW) else None
            out[r["dim"]] = m
        return out

    cur, prev = snap(p0, p1), snap(q0, q1)
    res = {}
    for code, c in cur.items():
        pv = prev.get(code)
        row = {"n": c["n"], "n_prev": (pv or {}).get("n", 0)}
        for k in ("cpm", "cpc", "ctr", "cvr", "roas"):
            a, bq_ = c.get(k), (pv or {}).get(k)
            row[k] = {"cur": a, "prev": bq_,
                      "pct": (round((a - bq_) / abs(bq_) * 100, 1) if (a is not None and bq_) else None),
                      "lower_better": KPI_LOWER_BETTER.get(k, False)}
        # 비교군이 크게 달라지면 증감 해석이 위험하다 → 경고 플래그
        n_now, n_bef = c["n"], row["n_prev"]
        row["n_shift"] = (n_bef == 0 or n_now == 0 or
                          max(n_now, n_bef) / max(1, min(n_now, n_bef)) >= 2.0)
        res[code] = row
    return {"mode": mode, "dim": dim, "dim_label": DIMS[dim],
            "current": {"from": p0, "to": p1}, "compare": {"from": q0, "to": q1},
            "rows": {dim_name(dim, k): v for k, v in res.items()},
            "note": ("비교군 수가 2배 이상 달라진 항목은 n_shift=true 입니다 — "
                     "증감이 실제 변화가 아니라 모집단 변화일 수 있습니다.")}


def get_media_summary(date_from="2025-01-01", date_to="2026-12-31", currency="KRW"):
    """첫 진입 화면용 — 매체별 요약(상품수=캠페인수 / 노출 / 클릭 / 조회 / 예산). 단일 쿼리."""
    p0, p1 = date_from[:7], date_to[:7]
    cl = _client()
    rate, sym = _currency(currency)

    def money(v):
        return sym + format(int(round((v or 0) / rate)), ",")

    params = [bigquery.ScalarQueryParameter("p0", "STRING", p0),
              bigquery.ScalarQueryParameter("p1", "STRING", p1)]
    sql = f"""
      SELECT media, COUNT(DISTINCT campaign_id) products, SUM(imp) imp, SUM(clk) clk,
             SUM(vviews) views, SUM(cost) cost
      FROM {TBL} WHERE period BETWEEN @p0 AND @p1
      GROUP BY media HAVING imp > 0 ORDER BY imp DESC
    """
    rows = list(cl.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    # 매체 묶음 집계 (Google+DV360 → Google). 캠페인ID는 매체별 고유라 상품수는 단순 합산.
    grp, tot = {}, {"products": 0, "imp": 0, "clk": 0, "views": 0, "cost": 0.0}
    for r in rows:
        g = MEDIA_GROUP_OF.get(r["media"], r["media"])
        a = grp.setdefault(g, {"products": 0, "imp": 0, "clk": 0, "views": 0, "cost": 0.0})
        for k, col in (("products", "products"), ("imp", "imp"), ("clk", "clk"),
                       ("views", "views"), ("cost", "cost")):
            v = r[col] or 0
            a[k] += v; tot[k] += v
    out = []
    for g, a in sorted(grp.items(), key=lambda kv: kv[1]["imp"], reverse=True):
        out.append({"media": g, "media_name": MEDIA_NAME.get(g, g),
                    "products": _num(a["products"]), "imp": _num(a["imp"]), "clk": _num(a["clk"]),
                    "views": _num(a["views"]), "spend": money(a["cost"])})
    return {"media": out, "n_media": len(out),
            "total": {"products": _num(tot["products"]), "imp": _num(tot["imp"]),
                      "clk": _num(tot["clk"]), "views": _num(tot["views"]), "spend": money(tot["cost"])},
            "currency": (currency or "KRW").upper(), "symbol": sym,
            "date_from": date_from, "date_to": date_to}


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
