# -*- coding: utf-8 -*-
"""도달(Reach 1+) 시뮬레이터 — PPT (c) '도달 시뮬레이터' 메뉴.

★ 설계 원칙: **실측하지 않은 것을 실측한 것처럼 그리지 않는다.**

제공자(provider)를 분리해 두고, 응답에 `fitted` 와 근거를 실어 화면이 그 사실을 숨길 수 없게 한다.

  AssumptionProvider   가정 기반. 노출→도달을 NBD 가정식으로 변환. 유니버스·k 를 사용자가 입력.
                       «한 모집단이 포화되는» 모양을 보여 준다. 우리 자료로 적합한 값은 아니다.
  FittedReachProvider  실측 적합. apac_kr_unified.v_meta_campaign_reach (캠페인당 1행,
                       기간 전체 누적 유니크 도달)로 reach = a·imps^b 를 적합. R² 0.86~0.93.
  NetflixReachProvider Netflix Reach Curve API — 이노션 전용 토큰 발급 대기.

왜 일자별 reach 를 안 쓰나
  reach 는 유니크 값이라 날짜를 더할 수 없다. 실증(DB 에이전트): 한 캠페인 42일에서
  일별 reach 합 12.9억 vs 일 최대 3,414만 — **38배**. 같은 사람을 매일 다시 센 값이다.
  일자 자료는 빈도의 93.8%가 1.5 미만이라 포화 구간 자체가 없다.
  (`apac_kr_ops.dictionary_column_notes` 에 severity='do_not_use' 로 박혀 있다)

왜 NBD 가 아니라 멱함수인가
  캠페인마다 타겟 모집단이 달라 «하나의 U» 가 없다. NBD 를 적합하면 k 가 격자 하한에 고착되고
  R² 가 0.09(전체)·0.00(BR·PH·IN)으로 붕괴한다(모형 오설정). 같은 자료에서 멱함수는 0.86~0.93.

⚠️ 적합 곡선이 답하는 질문
  맞음: "이 시장에서 노출 N 을 산 캠페인들은 평균 얼마나 도달했나"  (캠페인 «사이»)
  아님: "이 캠페인에 돈을 더 쓰면 도달이 어떻게 포화되나"          (캠페인 «안»)
  캠페인 간 자료로 캠페인 내 포화를 말하면 생태학적 오류다. 화면에도 이 구분을 적는다.

DV360 은 reach 컬럼 자체가 없어(수집 안 함) 포함되지 않는다. 디바이스별 도달도 원천에 없다.
"""
import math
import os

try:
    from bq import MARKET_NAME
except Exception:
    MARKET_NAME = {}
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
LOCATION = "asia-northeast3"
MART_DS = "apac_kr_benchmark"
CAMP_TBL = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
DPLAN_TBL = f"`{PROJECT}.{MART_DS}.bm_dplan_creative_monthly`"

# 기본 유니버스(도달 가능 모집단). **가정값이다** — 화면에서 사용자가 바꿀 수 있어야 한다.
# 근거: 대한민국 15~69세 인터넷 이용 인구 근사치. 매체별 실제 도달가능 규모는 이보다 작다.
DEFAULT_UNIVERSE = 36_000_000
# 접촉 분포의 집중도(k). NBD/베타이항 계열 파라미터.
#   k → ∞ : 접촉이 무작위(포아송) → 도달이 가장 높게 나옴
#   k  = 1 : 기하분포. 매체 플래닝에서 흔히 쓰는 보수적 기본값
#   k <  1 : 소수에게 반복 노출이 몰림 → 도달이 낮아짐
DEFAULT_K = 1.0

for _k in [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
           os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))]:
    if _k and os.path.exists(_k):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
        break


def _client():
    return bigquery.Client(project=PROJECT, location=LOCATION)


# ── 노출 단가(CPM) — 여기는 실측이다 ────────────────────────────────
@lru_cache(maxsize=32)
def _cpm(media, market, day):
    """매체별 실측 CPM(원). 예산 → 노출 환산에 쓴다. day 는 캐시 일일 무효화 키.

    API 매체는 bm_campaign_monthly, NAS 전용 매체(넷플릭스·티빙 등)는 디플랜 마트에서.
    디플랜은 cost_ok(금액기준 확인) 행만 쓴다.
    """
    c = _client()
    if media and media.upper() in ("G", "M", "D", "T", "K", "N", "GD", "ALL"):
        mf = ""
        if media.upper() == "GD":
            mf = "AND media IN ('G','D')"
        elif media.upper() != "ALL":
            mf = f"AND media = '{media.upper()}'"
        mkt = f"AND market = '{market}'" if market else ""
        q = (f"SELECT SAFE_DIVIDE(SUM(cost),SUM(imp))*1000 cpm, SUM(imp) imp "
             f"FROM {CAMP_TBL} WHERE imp > 0 {mf} {mkt}")
        src = "bm_campaign_monthly (API 실측)"
    else:
        name = (media or "").replace("'", "")
        mf = f"AND media_name = '{name}'" if name else ""
        # ⚠️ 광고주 수를 같이 센다. 1곳이면 그 값은 «벤치마크 통계» 가 아니라 «가정» 이다 —
        #   광고주 간 편차가 «없다» 가 아니라 «모른다» 다(표본이 한 곳뿐이라 잴 수 없다).
        #   넷플릭스가 그 경우이고(DB 실측 광고주 1 · API 교차검증 없음), 화면이 그걸 말해야
        #   플래너가 33,620원을 «넷플릭스 CPM» 으로 인용하지 않는다.
        q = (f"SELECT SAFE_DIVIDE(SUM(IF(cost_ok,cost,0)),SUM(IF(cost_ok,imp,0)))*1000 cpm, "
             f"SUM(IF(cost_ok,imp,0)) imp, COUNT(DISTINCT advertiser) adv "
             f"FROM {DPLAN_TBL} WHERE imp > 0 {mf}")
        src = "bm_dplan_creative_monthly (디플랜 NAS 실측, 금액기준 확인분만)"
    r = list(c.query(q).result())[0]
    cpm = r["cpm"]
    if cpm and cpm > 0 and r["imp"]:
        adv = r.get("adv") if hasattr(r, "get") else None
        if adv is not None and adv < 2:
            src += (f" · ⚠️ 광고주 {adv}곳뿐이라 «가정» 입니다 — 광고주 간 편차는 "
                    "«없다» 가 아니라 «모른다» 입니다. 매체 CPM 으로 인용하지 마십시오")
        return float(cpm), src

    # 🔴 «다른 매체 CPM 으로 후퇴» 를 없앴다 (2026-10-06, DB f5 실측 경고)
    #   예전에는 전체 평균 CPM 으로 후퇴했다. 그러면 넷플릭스를 골랐을 때 구글·메타가
    #   지배하는 평균값이 들어간다 — 같은 기간 실측으로 넷플릭스 33,620원 vs YT 2,263원,
    #   14.9배다. 노출을 15배 크게 잡으면 포화 곡선의 «오른쪽 평평한 구간» 을 읽어
    #   「예산을 15배 적게 써도 같은 도달」로 보인다. 플래닝 판단이 정반대로 뒤집힌다.
    #   그래서 모르면 «모른다» 고 답한다. 틀린 숫자보다 빈 화면이 낫다.
    if (media or "").strip() and "넷플릭스" in (media or ""):
        nf = _netflix_cpm_cached(day)
        if nf:
            return nf["cpm"], (
                f"netflix_cpm_assumption (가정 — 광고주 {nf['advertisers']}곳 · "
                f"{nf['source_rows']}일 · {nf['spend_basis']} · "
                f"API 교차검증 {'있음' if nf['api_cross_checked'] else '없음'})")
    raise RuntimeError(
        f"«{media or '선택한 매체'}» 의 CPM 실측이 없어 예산→노출 환산을 할 수 없습니다. "
        "다른 매체 CPM 으로 대신 쓰면 노출이 자릿수로 틀립니다(같은 기간 넷플릭스와 "
        "YouTube 가 14.9배 차이). 매체를 바꾸거나, 노출을 직접 입력하십시오.")


def _reach_fraction(imps, universe, k):
    """노출 → 도달률. 접촉 횟수가 NBD(음이항)를 따른다는 가정.

        reach_fraction = 1 - (1 + GRP/k) ^ (-k),   GRP = imps / universe

    k→∞ 이면 1-exp(-GRP)(포아송/무작위접촉, 이른바 Sainsbury 식)로 수렴한다.
    **이 식이 '가정'이다.** 우리 데이터로 k 를 적합한 것이 아니다.
    """
    if universe <= 0 or imps <= 0:
        return 0.0
    grp = imps / universe
    if k >= 1e6:
        return 1.0 - math.exp(-grp)
    return 1.0 - (1.0 + grp / k) ** (-k)


def _is_meta(media):
    """도달 적합은 Meta 캠페인 기준이다. CPM 을 다른 매체에서 가져오면 조합이 섞인다."""
    return (media or "").strip().upper() in ("M", "META", "인스타그램", "인스타그램+스레드", "페이스북")


class Provider:
    name = "base"
    label = "base"
    fitted = False

    def available(self):
        return False, "미구현"

    def curve(self, **kw):
        raise NotImplementedError


class AssumptionProvider(Provider):
    name = "assumption"
    label = "가정 기반 추정"
    fitted = False

    def available(self):
        return True, "사용 가능 (실측 적합 아님)"

    def curve(self, budget, media, market, universe, points, k=DEFAULT_K, flight_days=30):
        import datetime
        universe = int(universe or DEFAULT_UNIVERSE)
        cpm, cpm_src = _cpm(media or "ALL", market or "", datetime.date.today().isoformat())
        pts = []
        n = max(int(points), 2)
        for i in range(1, n + 1):
            cost = budget * i / n
            imps = cost / cpm * 1000.0
            rf = _reach_fraction(imps, universe, k)
            reach = rf * universe
            pts.append({
                "cost": round(cost),
                "imps": round(imps),
                "reach": round(reach),
                "reach_pct": round(rf * 100, 2),
                "frequency": round(imps / reach, 2) if reach > 0 else None,
            })
        return {
            "provider": self.name, "provider_label": self.label,
            "fitted": False,
            "estimate_badge": "실측 미적합 — 가정 기반 추정",
            "points": pts,
            "measured": {
                "cpm": round(cpm, 2),
                "cpm_source": cpm_src,
                "note": "예산→노출 환산은 실측 CPM 입니다.",
            },
            "assumptions": [
                {"key": "universe", "label": "도달 가능 모집단", "value": universe,
                 "editable": True,
                 "note": "기본값은 대한민국 15~69세 인터넷 이용 인구 근사치입니다. "
                         "매체별 실제 도달가능 규모는 이보다 작습니다."},
                {"key": "k", "label": "접촉 집중도(k)", "value": k, "editable": True,
                 "note": "노출이 소수에게 몰리는 정도. k가 작을수록 도달이 낮게 나옵니다. "
                         "1.0은 매체 플래닝의 보수적 관행값이며 우리 데이터로 적합한 값이 아닙니다."},
            ],
            "caveats": [
                "노출→도달 변환은 **가정**입니다. 우리 데이터로 적합한 곡선이 아닙니다.",
                "Meta reach 는 일자 단위라 날짜를 더할 수 없어(유니크 값) 곡선 적합에 쓸 수 없습니다. "
                "실증: 한 캠페인 42일에서 일별 reach 합이 일 최대 reach 의 38배였습니다.",
                "DV360 은 reach 를 수집하지 않습니다.",
                "디바이스별(TV/PC/Mobile) 도달은 원천에 reach 가 없어 제공하지 않습니다.",
            ],
        }


# 서비스 SA 는 apac_kr_benchmark 만 읽는다 → 마트 복사본을 본다(원본은 빌더가 읽어 옮긴다).
REACH_VIEW = f"`{PROJECT}.{MART_DS}.bm_meta_campaign_reach`"
FIT_MIN_CAMPAIGNS = 12   # 이보다 적으면 그 시장은 적합하지 않는다(과적합 방지)


def _view_exists(fq):
    try:
        _client().get_table(fq.strip("`"))
        return True
    except Exception:
        return False


@lru_cache(maxsize=64)
@lru_cache(maxsize=4)
def _fit_period_basis(day, market):
    """적합에 쓴 캠페인들의 집행기간 분포.

    🔴 2026-10-06 실측으로 드러난 것 — 이 곡선에는 «기간 해상도» 가 없다.
      n=1,106 · 최소 264일 · 중위 264일 · 평균 344일 · 90일 이하 0건.
      즉 이 곡선이 답하는 것은 «9개월 가까이 누적된 도달» 이다. 4주 플라이트에
      그대로 읽으면 도달을 과대평가한다. 같은 노출을 짧게 몰아 넣으면 덜 닿는다.
      (DB f5 지적: 「날짜 입력이 없으면 기간이 화면에 안 적힌 가정이 된다」)

    ⚠️ 기간별로 가를 표본이 없으므로 «보정» 하지 않는다. 보정 계수를 만들면 재지 않은
      것을 잰 것처럼 그리는 셈이다. 대신 기준을 글자로 적고, 사용자가 의도한 기간이
      기준보다 훨씬 짧으면 경고한다. 넷플릭스 곡선이 오면 그쪽이 기간별로 답한다.
    """
    if not _view_exists(REACH_VIEW):
        return None
    c = _client()
    mf = "AND market = @mk" if market else ""
    params = [bigquery.ScalarQueryParameter("mk", "STRING", market)] if market else []
    try:
        r = list(c.query(
            f"SELECT COUNT(*) n, MIN(delivery_days_actual) d_min, "
            f"MAX(delivery_days_actual) d_max, "
            f"APPROX_QUANTILES(delivery_days_actual,100)[OFFSET(50)] d_med, "
            f"ROUND(AVG(delivery_days_actual)) d_avg, "
            f"COUNTIF(delivery_days_actual <= 30) le30, "
            f"COUNTIF(delivery_days_actual <= 90) le90 FROM {REACH_VIEW} "
            f"WHERE delivery_days_actual > 0 AND unique_reach > 0 AND impressions > 0 "
            f"AND {FIT_EXCLUDE} {mf}",
            job_config=bigquery.QueryJobConfig(query_parameters=params)).result())[0]
    except Exception:
        return None
    if not r["n"]:
        return None
    return {"n": r["n"], "days_min": r["d_min"], "days_median": r["d_med"],
            "days_max": r["d_max"], "days_avg": int(r["d_avg"] or 0),
            "n_le_30d": r["le30"], "n_le_90d": r["le90"],
            "caveat": (f"실제 집행일수(노출이 있던 날) 기준입니다 — 중위 {r['d_med']}일 · "
                       f"30일 이하 {r['le30']:,}건 · 90일 이하 {r['le90']:,}건. "
                       "고른 집행기간에 해당하는 캠페인들로만 적합합니다.")}


@lru_cache(maxsize=2)
def _netflix_cpm_cached(day):
    """넷플릭스 환산 CPM — DB 가 매일 갱신하는 테이블에서 읽는다. 숫자를 박지 않는다.

    🔴 이 값은 «벤치마크 통계» 가 아니라 «환산 가정» 이다. 광고주 1곳·52일·순매체비 추정·
      API 교차검증 없음. 다른 매체 CPM 으로 환산하면 자릿수가 틀린다(같은 기간 YT 와 14.9배).
      포화 곡선이라 노출을 15배 크게 잡으면 평평한 구간을 읽어 «예산을 15배 적게 써도 같은
      도달» 로 보인다 — 플래닝 판단이 뒤집힌다.
    """
    tbl = f"`{PROJECT}.{MART_DS}.netflix_cpm_assumption`"
    if not _view_exists(tbl):
        return None
    c = _client()
    try:
        r = list(c.query(f"SELECT * FROM {tbl} LIMIT 1").result())[0]
    except Exception:
        return None
    return {"cpm": float(r["cpm_krw"]), "advertisers": r["advertisers"],
            "source_rows": r["source_rows"], "spend_basis": r["spend_basis"],
            "api_cross_checked": bool(r["api_cross_checked"]),
            "date_from": str(r["date_from"]), "date_to": str(r["date_to"]),
            "daily_min": float(r["cpm_daily_min"]), "daily_max": float(r["cpm_daily_max"]),
            "warning": r["warning"],
            "is_assumption": (r["advertisers"] or 0) < 2 or not r["api_cross_checked"]}


# ── 집행기간 버킷 (DB f5 2026-10-06 제공 컬럼으로 가능해졌다) ────────
# 🔑 period_days 를 적합에 쓰면 안 된다 — period_source='collection_window' 가 말해주듯
#   그것은 «그 캠페인을 마지막으로 건드린 수집 실행» 의 함수다. 1,136행 전부 요청 창과
#   같았다. delivery_days_actual 이 insights 에서 «노출이 있던 날» 을 센 진짜 집행일수다.
#
# ⚠️ 적합에서 빼는 두 가지 (실측 영향: 1,106 → 948)
#   delivery_days_actual IS NULL (101캠)
#     전부 한 계정(Hyundai N Project)이고, 2026-08-30 에 수집에 추가되며 과거가 백필되지
#     않았다. 약 ₩7.66억이 없다. 기간 이름표를 붙일 수 없어 제외한다.
#   reach_window_exceeds_delivery = FALSE (158캠)
#     집행이 요청 창을 벗어난 쪽이다. 도달이 «깎여» 있어 섞으면 긴 집행 구간이
#     과소평가된다. (DB f5 가 처음엔 «부풀림» 이라 했다가 재서 방향을 정정했다)
FIT_BUCKETS = [(1, 10, "~10일"), (11, 20, "~20일"), (21, 35, "~35일"),
               (36, 70, "~70일"), (71, 120, "~120일"), (121, 100000, "120일+")]

# 🔑 «집행 구간이 창 안에 들어갔나» 로 거른다 (DB f5 정정, 2026-10-06)
#   처음 안내받은 reach_window_exceeds_delivery 는 «창이 집행보다 긴가»(기간 길이)
#   비교였고, 창이 더 길어도 집행이 앞뒤로 삐져나가면 도달이 깎인다.
#   948건 중 166건이 그 경우였고, 깎인 채 적합에 들어가 있었다. 진짜 포함은 782건.
FIT_EXCLUDE = ("delivery_days_actual IS NOT NULL AND delivery_within_reach_window")

# ⚠️ 정정의 대가가 긴 버킷에 쏠린다 — 긴 캠페인이 더 많이 깎인다.
#   ~10일 176 · ~20일 223 · ~35일 306 · ~70일 66 · ~120일 9 · 120일+ 2
#   (DB 가 10-05 사고 후 수집 창을 180일로 묶었고, 집행 180일 초과 74건은 구조적으로 깎인다.
#    창을 넓히면 회복되지만 런타임 위험이 있어 DB 측 사용자 판단 대기 중.)
#   그래서 긴 플라이트는 «36일+ 묶음»(77캠 · b=0.945 · R² 0.94)으로 한 단계만 넓힌다.
#   바로 전체 혼합으로 가면 10일 캠페인까지 같은 선에 올라 기간 효과가 다시 섞인다.
FIT_WIDE = (36, 100000, "36일+ 묶음")
BUCKET_N_THIN = 30   # 이보다 적으면 «표본 적음» 을 붙인다


def _bucket_for(days):
    """플래너가 고른 집행기간 → 버킷. 범위를 벗어나면 가장 가까운 쪽."""
    d = int(days or 30)
    for lo, hi, lb in FIT_BUCKETS:
        if lo <= d <= hi:
            return (lo, hi, lb)
    return FIT_BUCKETS[-1] if d > FIT_BUCKETS[-1][0] else FIT_BUCKETS[0]


def _fit(market, day, bucket=None):
    """캠페인 누적 unique reach 로 도달–노출 관계를 적합.

    자료  apac_kr_unified.v_meta_campaign_reach — 캠페인당 1행(기간 전체 누적 유니크 도달).
          일자별 reach 는 가법적이지 않아 쓸 수 없다(같은 사람이 매일 다시 세진다).

    ★ 모델 선택 근거 — NBD(단일 유니버스)는 이 자료에 맞지 않는다.
      reach = U*(1-(1+imps/(U*k))^-k) 를 격자탐색으로 적합하면 k 가 하한에 고착되고
      R² 가 0.09(전체)·0.00(BR·PH·IN)로 붕괴한다. 캠페인마다 타겟 모집단이 달라
      «하나의 U» 가 존재하지 않기 때문이다(모형 오설정).
      멱함수 reach = a·imps^b 는 같은 자료에서 R² 0.86~0.93 으로 잘 맞는다.

    ⚠️ 이 곡선이 답하는 질문을 혼동하지 말 것.
      맞음: "이 시장에서 노출을 N 으로 집행한 캠페인들은 평균 얼마나 도달했나" (캠페인 간 관계)
      아님: "지금 이 캠페인에 돈을 더 쓰면 도달이 어떻게 포화되나" (캠페인 내 포화)
      캠페인 간 자료로 캠페인 내 포화를 말하면 생태학적 오류다. 화면에도 이 구분을 적는다.

    반환 (a, b, n, r2) 또는 None(표본 부족)
    """
    c = _client()
    mf = "AND market = @mk" if market else ""
    params = [bigquery.ScalarQueryParameter("mk", "STRING", market)] if market else []
    bf = ""
    if bucket:
        bf = f"AND delivery_days_actual BETWEEN {int(bucket[0])} AND {int(bucket[1])}"
    rows = list(c.query(
        f"SELECT impressions imp, unique_reach rch FROM {REACH_VIEW} "
        f"WHERE unique_reach > 0 AND impressions > 0 AND {FIT_EXCLUDE} {bf} {mf}",
        job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
    pts = [(float(r["imp"]), float(r["rch"])) for r in rows]
    if len(pts) < FIT_MIN_CAMPAIGNS:
        return None
    n = len(pts)
    sx = sy = sxx = sxy = 0.0
    for imp, rch in pts:                       # log-log 최소제곱
        x, y = math.log(imp), math.log(rch)
        sx += x; sy += y; sxx += x * x; sxy += x * y
    den = n * sxx - sx * sx
    if den == 0:
        return None
    b = (n * sxy - sx * sy) / den
    a = math.exp((sy - b * sx) / n)
    mean = sum(p[1] for p in pts) / n
    ss_res = sum((a * p[0] ** b - p[1]) ** 2 for p in pts)
    ss_tot = sum((p[1] - mean) ** 2 for p in pts) or 1.0
    return (a, b, n, max(0.0, 1.0 - ss_res / ss_tot))


class FittedReachProvider(Provider):
    name = "fitted"
    label = "실측 적합 (Meta 캠페인 누적 도달)"
    fitted = True

    def available(self):
        if not _view_exists(REACH_VIEW):
            return False, ("Meta 캠페인 누적 도달 뷰(v_meta_campaign_reach) 대기 중. "
                           "일자별 reach 는 합산이 불가해 적합에 쓸 수 없습니다.")
        import datetime
        if _fit("", datetime.date.today().isoformat()) is None:
            return False, f"적합 표본 부족(캠페인 {FIT_MIN_CAMPAIGNS}개 미만)"
        return True, "사용 가능 (Meta 캠페인 누적 도달로 적합)"

    def curve(self, budget, media, market, universe, points, k=None, flight_days=30):
        import datetime
        day = datetime.date.today().isoformat()
        # 🔑 집행기간 버킷 안에서 적합한다 — 기간을 섞으면 «수확체감» 이 기간 효과와
        #   뒤섞인다(실측: 섞으면 b=0.939, ~35일 버킷만 보면 b=0.991).
        # 단계적 후퇴 — 한 번에 전체 혼합으로 가지 않는다.
        #   ① 그 시장 × 그 집행기간 버킷
        #   ② 전체 시장 × 그 버킷
        #   ③ 전체 시장 × «36일+ 묶음»  (긴 플라이트만 · 기간을 한 단계만 넓힌다)
        #   ④ 전체 기간 혼합            (그 사실과 이유를 화면에 쓴다)
        bk = _bucket_for(flight_days)
        fit = _fit(market or "", day, bucket=bk)
        scope = f"{market} 시장 · 집행 {bk[2]}" if market else f"전체 시장 · 집행 {bk[2]}"
        if fit is None:
            fit, scope = _fit("", day, bucket=bk), f"전체 시장 · 집행 {bk[2]}(해당 시장 표본 부족)"
        if fit is None and int(flight_days or 30) >= FIT_WIDE[0]:
            fit = _fit("", day, bucket=FIT_WIDE)
            if fit is not None:
                bk = FIT_WIDE
                scope = (f"전체 시장 · 집행 {FIT_WIDE[2]}"
                         f"(고른 {int(flight_days)}일 구간의 표본이 적어 한 단계 넓혔습니다)")
        if fit is None:
            fit = _fit(market or "", day)
            scope = ("전체 기간 혼합(해당 집행기간 표본 부족 — 기간 효과가 "
                     "수확체감으로 섞여 보입니다)")
            bk = None
        if fit is None:
            raise RuntimeError("적합 표본이 부족합니다")
        a, b, n_fit, r2 = fit
        cpm, cpm_src = _cpm(media or "M", market or "", day)
        cap = float(universe) if universe else None   # 사용자가 상한(모집단)을 주면 그 위로 안 올라간다
        pts = []
        n = max(int(points), 2)
        for i in range(1, n + 1):
            cost = budget * i / n
            imps = cost / cpm * 1000.0
            reach = a * (imps ** b)
            if cap:
                reach = min(reach, cap)
            pts.append({"cost": round(cost), "imps": round(imps), "reach": round(reach),
                        "reach_pct": round(reach / cap * 100, 2) if cap else None,
                        "frequency": round(imps / reach, 2) if reach > 0 else None})
        return {
            "provider": self.name, "provider_label": self.label,
            "fitted": True,
            "estimate_badge": f"실측 적합 — Meta 캠페인 {n_fit:,}개로 적합 (R²={r2:.2f})",
            "points": pts,
            "measured": {"cpm": round(cpm, 2), "cpm_source": cpm_src,
                         "note": "예산→노출은 실측 CPM, 노출→도달은 실측 캠페인 적합입니다."},
            "fit": {"scope": scope, "n_campaigns": n_fit, "r2": round(r2, 3),
                    "model": "reach = a · imps^b (로그-로그 최소제곱)",
                    # f5 제안 — 비교표에 «무엇을 재는가» 를 한 줄로 못 박는다.
                    # 안 적으면 넷플릭스 곡선과 모양이 다를 때 다음 사람이
                    # 「우리 모델이 틀렸다」로 읽는다. 질문이 다른 것이 결함이 아니다.
                    "measures": ("캠페인 사이 — 예산 규모가 다른 캠페인들을 가로질러 본 "
                                 "관계입니다. 「한 조건 안에서 노출만 늘리면」 어떻게 되는지는 "
                                 "이 자료가 답하지 못합니다(예산이 큰 캠페인은 타겟도 넓게 잡습니다)."),
                    "a": round(a, 4), "b": round(b, 4),
                    # 🔴 «수확체감» 이라고 단정하던 문구를 고쳤다(2026-10-06).
                    #   기간을 섞은 적합은 b=0.939 였는데, 집행기간 버킷 안에서 다시 재면
                    #   b=0.991(~35일·362캠) 로 거의 비례다. 즉 그 수확체감의 상당 부분은
                    #   «기간이 짧은 캠페인과 긴 캠페인을 같은 선에 올린» 탓이었다.
                    #   b 를 보고 말을 고르게 한다 — 없는 포화를 있다고 말하지 않는다.
                    "interpretation": (
                        f"노출을 2배로 늘리면 도달은 약 {2**b:.2f}배가 됩니다 (b={b:.3f}). "
                        + ("이 구간에서는 거의 비례합니다 — 뚜렷한 포화는 보이지 않습니다."
                           if b >= 0.97 else
                           "노출을 늘릴수록 도달 증가분이 줄어듭니다(수확체감).")),
                    "source": "apac_kr_unified.v_meta_campaign_reach (캠페인 기간 전체 누적 유니크 도달)",
                    # 🔴 기간은 «안 적힌 가정» 이 되기 쉽다 — 반드시 응답에 실어 화면이 숨길 수 없게 한다
                    "period_basis": _fit_period_basis(day, market or ""),
                    # 적합이 약한 구간을 화면이 말해야 한다 — R² 가 낮으면 그 버킷의
                    # 캠페인들이 노출만으로 설명되지 않는다는 뜻이다(브랜드·타겟 차이).
                    "fit_weak": r2 < 0.70,
                    "fit_weak_note": ("이 집행기간 구간은 적합이 약합니다(R²="
                                      f"{r2:.2f}) — 노출 말고 다른 요인이 도달을 크게 "
                                      "가릅니다. 구간 안 캠페인들의 타겟 범위가 서로 "
                                      "달라서일 수 있습니다. 중앙값으로 읽으십시오."
                                      if r2 < 0.70 else ""),
                    "bucket": ({"label": bk[2], "days_from": bk[0], "days_to": bk[1],
                                "n": n_fit, "thin": n_fit < BUCKET_N_THIN,
                                "requested_days": int(flight_days)} if bk else
                               {"label": "전체 기간 혼합", "n": n_fit, "thin": False,
                                "requested_days": int(flight_days)})},
            "assumptions": [
                {"key": "universe", "label": "도달 상한(모집단) — 선택", "value": round(cap) if cap else 0,
                 "editable": True,
                 "note": "비워 두면 상한 없이 적합식 그대로 그립니다. 값을 넣으면 그 위로 올라가지 않도록 자릅니다."},
            ],
            "caveats": ([
                (f"🔴 **매체가 섞여 있습니다** — 도달 곡선은 **Meta** 캠페인으로 적합했는데 "
                 f"예산→노출 환산 CPM 은 **{media}** 의 실측값({cpm_src})입니다. "
                 f"매체가 다르면 도달 특성도 다르므로, 이 조합은 «Meta 의 도달 패턴을 "
                 f"{media} 단가에 적용하면» 이라는 가정으로만 읽으십시오. "
                 f"Meta 를 선택하면 이 경고가 사라집니다.")
            ] if not _is_meta(media) else []) + [
                f"**Meta 캠페인 {n_fit:,}개**({scope})로 적합했습니다. 다른 매체에는 그대로 적용되지 않습니다.",
                "🔴 이 곡선은 **«캠페인 사이»의 관계**입니다 — «이 정도 노출을 산 캠페인들은 평균 이만큼 도달했다». "
                "**«지금 이 캠페인에 돈을 더 쓰면»** 의 답이 아닙니다. 한 캠페인 안의 포화는 같은 사람에게 반복 노출되며 "
                "훨씬 빨리 꺾이므로, 이 곡선보다 보수적으로 보셔야 합니다.",
                "NBD(단일 모집단 포화) 모형은 이 자료에 맞지 않아 쓰지 않았습니다 — 캠페인마다 타겟 모집단이 달라 "
                "R² 가 0 으로 붕괴합니다. «가정 기반 추정» 제공자가 그 형태를 대신 보여 줍니다.",
                "DV360 은 reach 를 수집하지 않아 포함되지 않았습니다.",
                "디바이스별(TV/PC/Mobile) 도달은 원천에 reach 가 없어 제공하지 않습니다.",
            ],
        }


# ── 넷플릭스 타게팅 사전 + 도달 곡선 캐시 ───────────────────────────
# DB(f5) 계약: QA/38_DB_NETFLIX_CURVE_CACHE_CONTRACT.md
#
# ★ 이름 규약 — 접두 없는 것은 DB 가 넣는다. 우리가 만들지도 지우지도 않는다.
#   한때 bm_netflix_targeting 으로 복사했는데 뺐다. DB 가 apac_kr_benchmark 에 직접
#   넣어주므로(우리가 OWNER · benchmark-app 이 READER) 복사하면 «DB 훅이 먼저 도는지»
#   에 의존하게 되고, 먼저 안 돌면 조용히 하루 묵은 값을 쓴다.
TARGET_TBL = f"`{PROJECT}.{MART_DS}.netflix_targeting`"

# 🔴 곡선은 이 «뷰» 만 읽는다. 격자 LEFT JOIN 결과라 «아직 안 채운 칸» 도 행으로 나온다.
#   채워진 것 안에서 커버리지를 재면 늘 100% 다 — 분모가 결과에 따라 줄어드는 함정이다.
CURVE_VIEW = f"`{PROJECT}.{MART_DS}.v_netflix_reach`"

# 🔴 도달 곡선 API 호출 한도 — 실제 호출은 DB 훅이 한다. 이 상수는 «왜 실시간이 아닌가»
#    를 코드에 남기는 역할이다. 한국만 연령 6 × 성별 2 × 기기 3 = 36가지이고 장르 22 ·
#    관심사 115 를 곱하면 수만 가지라, 사용자가 몇 번 조작하면 하루치를 태운다.
#    구조: 야간 격자 선계산 → BQ 캐시 → UI 는 캐시만 조회.
#    ⚠️ 이 모듈은 요청 경로에서 넷플릭스 API 를 호출하지 않는다.
NETFLIX_RATE_LIMITS = {"month": 1500, "day": 50, "hour": 20, "minute": 5}

# ⚠️ curve_status 다섯 값 — «불가능» 과 «아직 안 됨» 을 같게 보여주면 안 된다.
#    일 50회라 격자가 며칠에 걸쳐 채워진다. not_permitted 는 플래너가 조건을 바꿔야 하고,
#    not_requested 는 기다리면 생긴다 — 다른 행동이다.
#    suppressed 를 «도달 0» 으로 그리면 플래너가 «효과 없는 조건» 으로 읽는다.
CURVE_STATUS = {
    "ok":            {"label": "곡선 있음", "usable": True,  "note": ""},
    "suppressed":    {"label": "넷플릭스 미공개", "usable": False,
                      "note": "모집단이 작아 넷플릭스가 출력을 억제한 조건입니다(422). "
                              "도달이 0 이라는 뜻이 아닙니다."},
    "not_permitted": {"label": "사용 불가 조건", "usable": False,
                      "note": "이 계정·국가로는 쓸 수 없는 조건입니다(403). "
                              "조건을 바꿔야 합니다 — 기다려도 생기지 않습니다."},
    "fetch_failed":  {"label": "수집 실패", "usable": False,
                      "note": "호출이 실패한 조건입니다(429/5xx/타임아웃). 우리 쪽 문제이고, "
                              "다시 부르면 값이 생길 수 있습니다."},
    "not_requested": {"label": "아직 조회 전", "usable": False,
                      "note": "격자에는 있으나 아직 호출하지 않았습니다. 호출 한도가 일 50회라 "
                              "격자가 며칠에 걸쳐 채워집니다 — 기다리면 생깁니다."},
}


@lru_cache(maxsize=16)
def _curve_status_cached(day, market):
    """곡선 캐시의 상태 내역. v_netflix_reach 만 읽는다(격자 LEFT JOIN 결과).

    ★ 분모를 «채워진 것» 으로 잡지 않는다 — 그러면 커버리지가 늘 100% 로 나온다.
      격자 전체가 분모고, 그 안에서 ok 가 몇인지가 진행률이다.
    """
    if not _view_exists(CURVE_VIEW):
        return []
    c = _client()
    where = "WHERE country = @mk" if market else ""
    job = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mk", "STRING", market or "KR")])
    try:
        rows = list(c.query(
            f"SELECT IFNULL(curve_status,'not_requested') st, COUNT(*) n "
            f"FROM {CURVE_VIEW} {where} GROUP BY st ORDER BY n DESC",
            job_config=job).result())
    except Exception:
        return []
    return [(r["st"], r["n"]) for r in rows]


def _curve_counts(market=""):
    """(쓸 수 있는 곡선 수, 격자 전체 수)."""
    from datetime import date
    st = _curve_status_cached(date.today().isoformat(), (market or "").upper())
    n_all = sum(n for _, n in st)
    n_ok = sum(n for k, n in st if CURVE_STATUS.get(k, {}).get("usable"))
    return n_ok, n_all


def curve_status(market=""):
    """상태별 내역 — 화면이 «불가능» 과 «아직 안 됨» 을 갈라 보여줄 수 있게."""
    from datetime import date
    st = _curve_status_cached(date.today().isoformat(), (market or "").upper())
    if not st:
        return {"available": False, "rows": [], "note": "곡선 캐시가 아직 없습니다."}
    out = []
    for k, n in st:
        meta = CURVE_STATUS.get(k, {"label": k, "usable": False, "note": "정의되지 않은 상태입니다."})
        out.append({"status": k, "label": meta["label"], "usable": meta["usable"],
                    "note": meta["note"], "n": n})
    n_all = sum(n for _, n in st)
    n_ok = sum(x["n"] for x in out if x["usable"])
    return {"available": True, "market": (market or "").upper(), "rows": out,
            "n_grid": n_all, "n_usable": n_ok,
            "progress": round(n_ok / n_all * 100, 1) if n_all else 0.0,
            "note": ("진행률의 분모는 격자 전체입니다 — 채워진 것만 세면 늘 100%% 가 됩니다. "
                     f"호출 한도가 일 {NETFLIX_RATE_LIMITS['day']}회라 며칠에 걸쳐 채워집니다.")}


@lru_cache(maxsize=8)
def _targeting_cached(day, market):
    """넷플릭스 선택 항목 — 차원별 목록. market='' 이면 전 국가.

    ★ 국가는 countries 배열로 건다(country_scope LIKE 는 'KR-SEOUL' 류가 오면 틀린다).
    ★ 총량을 숫자로 박지 않는다 — 일일 훅이 원천 시트를 읽어 갱신하므로 매번 센다.
    """
    if not _view_exists(TARGET_TBL):
        return None
    c = _client()
    where = "WHERE @mk IN UNNEST(countries)" if market else ""
    job = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mk", "STRING", market or "KR")])
    rows = list(c.query(f"""
        SELECT targeting_dimension dim, targeting_value_id id,
               targeting_value_name name, targeting_value_description descr
        FROM {TARGET_TBL} {where}
        ORDER BY targeting_dimension, targeting_value_name""", job_config=job).result())
    dims = {}
    for r in rows:
        dims.setdefault(r["dim"], []).append(
            {"id": r["id"], "name": r["name"], "description": r["descr"] or ""})
    return dims


@lru_cache(maxsize=2)
def _target_markets_cached(day):
    """국가별 가용 조건 수. 국가마다 집합이 달라(US 839 · JP 354 · KR 251) 화면이
    «이 나라에서는 못 쓰는 조건» 을 알려줘야 한다."""
    if not _view_exists(TARGET_TBL):
        return []
    c = _client()
    rows = list(c.query(f"""
        SELECT ct market, COUNT(*) n,
               COUNT(DISTINCT targeting_dimension) dims
        FROM {TARGET_TBL}, UNNEST(countries) ct
        GROUP BY ct ORDER BY n DESC""").result())
    return [{"market": r["market"], "name": MARKET_NAME.get(r["market"], r["market"]),
             "n": r["n"], "dimensions": r["dims"]} for r in rows]


def targeting(market=""):
    """시뮬레이터 선택 항목. 다국가 플랜이면 market 을 비우고 전체를 받아
    화면이 국가별 가용 여부를 함께 보여 준다."""
    from datetime import date
    day = date.today().isoformat()
    dims = _targeting_cached(day, (market or "").upper())
    if dims is None:
        return {"available": False, "dimensions": {}, "markets": [],
                "note": ("넷플릭스 타게팅 사전이 아직 마트에 없습니다. "
                         "mart.build_netflix_targeting() 이 돌면 자동으로 채워집니다.")}
    mks = _target_markets_cached(day)
    return {
        "available": True,
        "market": (market or "").upper(),
        "dimensions": dims,
        "counts": {k: len(v) for k, v in dims.items()},
        "total": sum(len(v) for v in dims.values()),
        "markets": mks,
        "note": ("국가마다 가용 조건이 다릅니다 — 다국가 플랜에서는 국가별로 확인하십시오. "
                 "조건 ID 는 넷플릭스가 갱신하면 바뀌므로 저장해 두지 말고 매번 조회하십시오."),
    }


class NetflixReachProvider(Provider):
    name = "netflix"
    label = "Netflix Reach Curve API"
    fitted = True

    # 🔴 토큰이 아니라 «캐시» 가 가용 조건이다(DB f5 2026-10-06).
    #    호출 한도가 일 50 이라 요청 경로에서 부를 수 없다. 토큰만 보고 available=True 를
    #    돌려주면 화면이 실시간 조회를 전제로 짜이고, 그러면 다시 짜야 한다.
    # 🔴 «테이블이 있나» 가 아니라 «쓸 수 있는 곡선이 있나» 로 묻는다(DB f5 지적).
    #    DB 는 토큰이 오기 전에 스키마를 굳히려고 격자·캐시를 먼저 만든다. 테이블 존재로
    #    게이트를 걸면 그 시점에 제공자가 켜지고 화면이 곡선 0개로 열린다.
    #    0 은 «깨끗해서» 일 수도 «아직 아무것도 없어서» 일 수도 있다 — 구분해야 한다.
    def available(self, market=""):
        if not _view_exists(CURVE_VIEW):
            return False, ("넷플릭스 도달 곡선 캐시가 아직 없습니다. 호출 한도가 일 "
                           f"{NETFLIX_RATE_LIMITS['day']}회라 야간에 격자를 선계산해 "
                           "캐시에 넣는 구조가 전제입니다 — 준비되면 자동 활성화됩니다.")
        n_ok, n_all = _curve_counts(market)
        if n_ok:
            return True, f"사용 가능 — 곡선 {n_ok:,}개" + (f" / 격자 {n_all:,}" if n_all else "")
        if n_all:
            return False, (f"격자 {n_all:,}개가 등록됐지만 아직 쓸 수 있는 곡선이 없습니다. "
                           f"한도가 일 {NETFLIX_RATE_LIMITS['day']}회라 며칠에 걸쳐 채워집니다.")
        return False, "격자가 아직 등록되지 않았습니다(캐시할 조합이 미디어플래닝 협의 중)."

    def curve(self, **kw):
        ok, why = self.available(kw.get("market", ""))
        if not ok:
            raise RuntimeError(why)
        raise RuntimeError(
            "쓸 수 있는 곡선이 있으나 조회 구현이 남았습니다. 어떤 조합을 캐시할지는 "
            "미디어플래닝 협의 사항으로 올라가 있습니다. "
            "⚠️ 여기에 넷플릭스 API 직접 호출을 넣지 마십시오 — 한도가 일 "
            f"{NETFLIX_RATE_LIMITS['day']}회입니다.")


_PROVIDERS = [AssumptionProvider(), FittedReachProvider(), NetflixReachProvider()]


def providers():
    out = []
    for p in _PROVIDERS:
        ok, why = p.available()
        out.append({"name": p.name, "label": p.label, "fitted": p.fitted,
                    "available": ok, "status": why})
    return {"providers": out,
            "default": "assumption",
            "note": "fitted=true 인 제공자가 활성화되면 기본값을 그쪽으로 옮기세요."}


@lru_cache(maxsize=2)
def _markets_cached(day):
    """적합 가능한 시장 목록 — 화면 '국가' 셀렉터용. 표본이 하한 미만인 시장은 뺀다."""
    if not _view_exists(REACH_VIEW):
        return []
    c = _client()
    rows = list(c.query(
        f"SELECT market, COUNT(*) n FROM {REACH_VIEW} "
        f"WHERE unique_reach > 0 AND impressions > 0 AND market IS NOT NULL "
        f"GROUP BY market HAVING n >= {FIT_MIN_CAMPAIGNS} ORDER BY n DESC").result())
    return [{"market": r["market"], "n": r["n"],
             "name": MARKET_NAME.get(r["market"], r["market"])} for r in rows]


def markets():
    import datetime
    return {"markets": _markets_cached(datetime.date.today().isoformat()),
            "min_campaigns": FIT_MIN_CAMPAIGNS,
            "note": "표본이 적은 시장은 적합이 흔들려 목록에서 제외했습니다."}


def curve(budget=2_000_000_000, media="", market="KR", universe=None, points=20,
          provider=None, k=DEFAULT_K, flight_days=30):
    """요청한 제공자(없으면 사용 가능한 것 중 fitted 우선)로 도달 곡선 반환."""
    chosen = None
    if provider:
        chosen = next((p for p in _PROVIDERS if p.name == provider), None)
        if chosen and not chosen.available()[0]:
            raise RuntimeError(chosen.available()[1])
    if chosen is None:
        # 실측 적합 제공자가 쓸 수 있으면 그걸 먼저, 아니면 가정 기반
        chosen = next((p for p in _PROVIDERS if p.fitted and p.available()[0]),
                      _PROVIDERS[0])
    res = chosen.curve(budget=budget, media=media, market=market,
                       universe=universe, points=points, k=k,
                       flight_days=flight_days)
    res["providers"] = providers()["providers"]
    return res


if __name__ == "__main__":
    import json
    print(json.dumps(providers(), ensure_ascii=False, indent=2))
    r = curve(budget=2_000_000_000, media="넷플릭스", points=8)
    print(f"\n{r['estimate_badge']} · CPM ₩{r['measured']['cpm']:,.0f} ({r['measured']['cpm_source']})")
    for p in r["points"]:
        print(f"  ₩{p['cost']:>13,}  노출 {p['imps']:>12,}  도달 {p['reach_pct']:>6}%  빈도 {p['frequency']}")
