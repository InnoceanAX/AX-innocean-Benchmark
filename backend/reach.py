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

구글·DV360 이 왜 실측 적합에 없나 — «컬럼이 없어서» 가 아니다(2026-10-21 정정)
  CM360 데이터 전송은 들어와 있다: apac_kr_raw.p_impression_464224 (93,113,676행 · 71.1GB)에
  User_ID · Campaign_ID · DBM_*(DV360) 가 다 있다. 그런데 User_ID 가 «전부 비어 있다» —
  하루치 실측에서 식별불가 100.0% · 유니크 1(2026-10-17~20 네 날 모두). 개인정보 정책으로
  데이터 전송에서 식별자가 제거된 것으로 보인다.
  → 이 로그로는 유니크 도달을 셀 수 없다. DBM_* 도 같은 로그라 DV360 도 마찬가지다.
  → 구글·메타의 «예측» 곡선은 각자 별도 API(ReachPlanService · Reach & Frequency)로
    받아야 하며, 그쪽은 아직 연동되지 않았다.
디바이스별 도달도 원천에 없다.
"""
import math
import os
import random

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
    # 🔑 «예측» 인가 «실측» 인가, 그리고 «무엇을 재는가».
    #   세 벤더 곡선을 나란히 놓으면 모양이 서로 다르게 나온다. 그게 결함이 아니라
    #   재는 대상이 달라서라는 것을 제공자마다 글자로 들고 있어야 한다 —
    #   안 적으면 보는 사람이 «어느 쪽이 틀렸다» 로 읽는다(DB 와 서로 열 번 겪은 자리).
    kind = "forecast"       # forecast = 벤더 예측 · measured = 우리 실측 · assumption = 가정식
    measures = ""           # 한 줄: 이 곡선이 답하는 질문

    def available(self):
        return False, "미구현"

    def curve(self, **kw):
        raise NotImplementedError


class AssumptionProvider(Provider):
    name = "assumption"
    label = "가정 기반 추정"
    kind = "assumption"
    measures = ("가정식 — 한 모집단이 포화되는 «모양» 을 보여줍니다. "
                "우리 자료로 적합한 값이 아니라, 모집단과 접촉 집중도를 직접 넣어 보는 것입니다.")
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
                "구글·DV360 은 이 적합에 포함되지 않았습니다 — 수집을 안 해서가 아니라, "
                "CM360 노출 로그의 식별자(User_ID)가 전부 비어 있어 유니크 도달을 셀 수 "
                "없기 때문입니다(실측 식별불가 100%). 구글의 «예측» 곡선은 별도 API"
                "(ReachPlanService) 연동이 필요합니다.",
                "디바이스별(TV/PC/Mobile) 도달은 원천에 reach 가 없어 제공하지 않습니다.",
            ],
        }


# 서비스 SA 는 apac_kr_benchmark 만 읽는다 → 마트 복사본을 본다(원본은 빌더가 읽어 옮긴다).
REACH_VIEW = f"`{PROJECT}.{MART_DS}.bm_meta_campaign_reach`"
FIT_MIN_CAMPAIGNS = 12   # 이보다 적으면 그 시장은 적합하지 않는다(과적합 방지)


# 존재 확인은 조회마다 BQ 메타데이터를 때린다 — 느린 건 통계가 아니라 BQ 왕복이다
# (실측: 재부표본 600회 15ms vs BQ 쿼리 1.56s).
@lru_cache(maxsize=16)
def _view_exists(fq):
    try:
        _client().get_table(fq.strip("`"))
        return True
    except Exception:
        return False


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
#   그래서 긴 플라이트는 한 단계만 넓힌다. 바로 전체 혼합으로 가면 10일 캠페인까지
#   같은 선에 올라 기간 효과가 다시 섞인다.
#
# 🔑 «묶음의 대가» 가 둘이다 (DB f5 와 서로 재서 정리, 2026-10-06)
#   ① 교란 되살림  묶은 축이 설명변수와 함께 움직이면 «없는 기울기» 가 생긴다
#   ② 신호 희석    표본 적고 범위 넓은 꼬리가 섞이면 «있는 신호» 가 지워진다
#   ①은 없는 것을 만들고 ②는 있는 것을 없앤다 — 증상이 반대라 같은 검사로 못 잡는다.
#   묶을 때는 둘 다 본다: 묶은 축이 설명변수와 같이 움직이는가(①),
#   그리고 «쪼갠 조각끼리 결론이 다른가»(②).
#
# ⚠️ 처음엔 FIT_WIDE 를 (36, 무한) 으로 뒀다가 ②에 걸렸다. 쪼개 보니:
#     36~70일  n=66  b 0.92±0.03  → 1 과 구분됨
#     71일+    n=11  범위 305배    → 아무 값이나 나올 수 있다(NL_Hyundai 203·173일과
#                                   Hyundai_IND 5.45억 노출이 섞인다)
#     36일+전체 n=77  b 0.94±0.03  → 구분이 «지워진다»
#   꼬리 11건이 있는 신호를 희석했다. 그래서 넓히는 상한을 70일로 끊는다.
FIT_WIDE = (36, 70, "36~70일")
BUCKET_N_THIN = 30   # 이보다 적으면 «표본 적음» 을 붙인다


def _bucket_for(days):
    """플래너가 고른 집행기간 → 버킷. 범위를 벗어나면 가장 가까운 쪽."""
    d = int(days or 30)
    for lo, hi, lb in FIT_BUCKETS:
        if lo <= d <= hi:
            return (lo, hi, lb)
    return FIT_BUCKETS[-1] if d > FIT_BUCKETS[-1][0] else FIT_BUCKETS[0]


def _slope_log(lp):
    """로그 공간 점들의 기울기. 로그는 호출 전에 한 번만 계산한다 —
    재부표본 1,000회 × Theil-Sen 쌍마다 log() 를 다시 부르면 조회가 10초를 넘는다."""
    m = len(lp)
    if m < 3:
        return None
    mx = sum(x for x, _ in lp) / m
    sxx = sum((x - mx) ** 2 for x, _ in lp)
    if sxx <= 0:
        return None
    my = sum(y for _, y in lp) / m
    return sum((x - mx) * (y - my) for x, y in lp) / sxx


def _boot_ci(lp, reps=600, seed=20261006):
    """재부표본 95% 구간 (로그 공간 입력).

    ★ 왜 닫힌식 표준오차를 그대로 쓰지 않는가 (2026-10-06)
      닫힌식 SE 는 잔차가 등분산이라고 가정한다. 실측으로 재 보니 ~35일에서
      닫힌식 ±0.01 인데 부트 구간은 [0.96, 1.03] 으로 «더 넓다» — 공식이 불확실성을
      과소평가한다. 판정(«1 과 구분되는가»)을 과소평가된 구간으로 하면 안 된다.
      DB f5 가 자기 추정량을 재부표본으로 재서 «검정력이 애초에 없었다» 를 찾아냈고,
      같은 검사를 제 추정량에도 걸었다. ~70일은 세 방법(닫힌식·부트·Theil-Sen)에서
      모두 1 아래로 살아남았다.
      ⚠️ 씨앗을 고정한다 — 같은 조회가 화면마다 다른 구간을 내면 읽는 사람이 흔들림을
        신호로 읽는다.
    """
    n = len(lp)
    if n < 8:
        return None
    rr = random.Random(seed).randrange
    out = []
    for _ in range(reps):
        b = _slope_log([lp[rr(n)] for _ in range(n)])
        if b is not None:
            out.append(b)
    if len(out) < reps // 2:
        return None
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def _theil_sen(lp, cap=160, seed=20261006):
    """쌍별 기울기의 중위값 — 이상점에 끌리지 않는 추정량(분포 가정 없음).

    최소제곱과 «같은 자료, 다른 추정량» 이라 결론이 갈리면 이상점이 끌고 있다는 뜻이다.
    쌍이 n² 이라 큰 버킷은 표본을 줄여 센다(씨앗 고정 — 조회마다 값이 바뀌면 안 된다).
    """
    if len(lp) < 3:
        return None
    w = lp if len(lp) <= cap else random.Random(seed).sample(lp, cap)
    sl = []
    for i in range(len(w)):
        xi, yi = w[i]
        for j in range(i + 1, len(w)):
            dx = w[j][0] - xi
            if abs(dx) > 1e-9:
                sl.append((w[j][1] - yi) / dx)
    if not sl:
        return None
    sl.sort()
    return sl[len(sl) // 2]


# ⚠️ 이 데코레이터를 떼지 말 것. 2026-10-06 에 헬퍼를 «def _fit» 앵커로 끼워 넣으면서
#   이 줄이 아래 함수 위로 떨어져 나가 _fit 이 캐시 없이 돌았다. 조회마다 BQ 쿼리와
#   재부표본 600회를 다시 해서 응답이 8~12초가 됐다(day 를 인자로 받는 이유가 캐시다).
@lru_cache(maxsize=64)
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
        f"SELECT impressions imp, unique_reach rch, advertiser_name adv FROM {REACH_VIEW} "
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
    r2 = max(0.0, 1.0 - ss_res / ss_tot)
    # 합산 기울기의 표준오차 — 점추정만 내면 자리수가 정밀도로 읽힌다
    _mx = sx / n
    _sxx2 = sum((math.log(p0) - _mx) ** 2 for p0, _ in pts)
    _res = sum((math.log(p1) - (math.log(a) + b * math.log(p0))) ** 2 for p0, p1 in pts)
    b_se = math.sqrt(_res / (n - 2) / _sxx2) if (n > 2 and _sxx2 > 0) else float("inf")

    # 🔴 광고주 합산이 b 를 올릴 수 있다 — 같은 쿼리의 행으로 «광고주 안에서만» 다시 잰다.
    #   실측(2026-10-06): ~35일 합산 b=0.992 인데 광고주별로는 0.736(HMMY n=17) ·
    #   0.935(HMTH n=44) · 0.969(HMPH n=184) 로 전부 더 낮았다. 즉 「거의 비례」가
    #   광고주를 섞은 탓일 수 있다. 심슨 역설과 같은 모양이고, 오늘 기간에서 겪은 것과
    #   같은 종류다 — 교란을 하나 걷어내면 다음 교란이 드러난다.
    #   ⚠️ 어느 쪽이 맞다고 단정하지 않는다. 범위를 화면에 적어 사용자가 알게 한다.
    byadv = {}
    for r in rows:
        k = r.get("adv") if hasattr(r, "get") else None
        if k:
            byadv.setdefault(k, []).append((float(r["imp"]), float(r["rch"])))
    # 🔑 컷오프를 두지 않는다 — 대신 «구분되는가» 를 표준오차로 답한다.
    #   한때 노출 범위 20배 미만 광고주를 뺐다. DB f5 가 21칸을 재 보니 그 선이 의도한
    #   일을 하지 않았다: 내 주장을 만들던 칸(R 9.2·9.4)은 빼면서 가장 극단적인 추정
    #   (R=78 · b=0.587 · n=10)은 남긴다. 퍼짐이 R 과 단조로 줄지 않는다 —
    #   말이 안 되는 값은 R≈3 에서만 났고, R 이 10을 넘으면 이질성과 n 이 지배한다.
    #   ⚠️ 주석에 «판단이다» 라고 적어도 다음 사람은 코드의 20 을 측정값으로 읽는다.
    #     임계는 아예 세우지 않는 편이 낫다(f5).
    #
    #   그래서 점추정끼리 크기를 비교하지 않고, 기울기의 표준오차로
    #   «합산값과 구분되는가» 를 묻는다. n=10~13 에서 0.845 와 0.942 를 나란히 쓰면
    #   읽는 사람이 그 차이를 실재로 받는다 — 구분 가능 여부를 말해야 한다.
    sub = []
    for k, v in byadv.items():
        m = len(v)
        if m < FIT_MIN_CAMPAIGNS:
            continue
        xs = [math.log(p0) for p0, _ in v]
        ys = [math.log(p1) for _, p1 in v]
        mx = sum(xs) / m
        sxx2 = sum((x - mx) ** 2 for x in xs)
        if sxx2 <= 0:
            continue
        my = sum(ys) / m
        bi = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx2
        ai = my - bi * mx
        resid = sum((y - (ai + bi * x)) ** 2 for x, y in zip(xs, ys))
        se = math.sqrt(resid / (m - 2) / sxx2) if m > 2 else float("inf")
        rng = max(p0 for p0, _ in v) / max(min(p0 for p0, _ in v), 1.0)
        sub.append({"advertiser": k, "b": round(bi, 2), "se": round(se, 2),
                    "n": m, "imp_range": round(rng, 1),
                    # 합산값이 이 광고주의 ±2SE 밖인가 = 구분되는가
                    "differs_from_pooled": abs(bi - b) > 2 * se})

    within = None
    if len(sub) >= 2:
        sub.sort(key=lambda d: d["b"])
        n_diff = sum(1 for d in sub if d["differs_from_pooled"])
        lo_s, hi_s = sub[0], sub[-1]
        note = (f"광고주 {len(sub)}곳을 각각 따로 적합하면 b 가 {lo_s['b']:.2f}~{hi_s['b']:.2f} "
                f"입니다(표본 {min(d['n'] for d in sub)}~{max(d['n'] for d in sub)}개 · "
                f"노출 범위 {min(d['imp_range'] for d in sub):.0f}~"
                f"{max(d['imp_range'] for d in sub):.0f}배). ")
        note += (f"그중 {n_diff}곳이 합산값 {b:.2f} 와 구분됩니다(±2표준오차 기준)."
                 if n_diff else
                 f"다만 합산값 {b:.2f} 와 «구분되는» 곳은 없습니다 — 표본이 적어 "
                 "광고주를 섞은 것이 b 를 올리는지 지금 자료로는 말할 수 없습니다. "
                 "«차이가 없다» 는 뜻이 아니라 «가릴 수 없다» 는 뜻입니다.")
        within = {"n_advertisers": len(sub), "b_min": lo_s["b"], "b_max": hi_s["b"],
                  "n_differs": n_diff, "pooled_b": round(b, 2),
                  "inconclusive": n_diff == 0, "rows": sub, "note": note}
    _lp = [(math.log(p0), math.log(p1)) for p0, p1 in pts]
    ci = _boot_ci(_lp)
    ts = _theil_sen(_lp)
    return (a, b, n, r2, within, b_se, ci, ts)


class FittedReachProvider(Provider):
    kind = "measured"
    measures = ("캠페인 사이 — 예산 규모가 다른 «실제 집행 캠페인들» 을 가로질러 본 "
                "관계입니다. 메타 Insights 누적 유니크 도달이 근거입니다. "
                "«한 조건 안에서 노출만 늘리면» 의 답이 아닙니다.")
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
        # 🔴 120일 초과는 답하지 않는다 (DB 권고 + 실측, 2026-10-21)
        #   온전한 표본이 2건이다. 그 구간 캠페인은 사실상 «상시» 라(평균 플라이트
        #   555~661일) 2년 누적 도달은 이미 포화돼 새 플라이트에 대해 아무것도 말하지
        #   않는다. 36~70일에서 늘려 그리면 «없는 정밀도» 를 만든다 —
        #   population_source 로 가정과 실측을 가른 것과 같은 종류다.
        #   ⚠️ 수집 창을 늘려도 회복되지 않는다. 창 1,028일이면 사고를 재현하면서
        #     쓸모없는 값을 얻는다(DB 측정).
        if int(flight_days or 30) > 120:
            raise RuntimeError(
                f"집행 {int(flight_days)}일은 답하지 않습니다 — 120일을 넘는 구간의 "
                "온전한 표본이 2건뿐입니다. 그 구간 캠페인은 사실상 상시 집행이라 "
                "누적 도달이 이미 포화돼 새 플라이트의 도달을 말해주지 않습니다. "
                "더 짧은 집행기간을 고르시거나, 벤더 예측 곡선(넷플릭스·구글·메타)이 "
                "준비되면 그쪽에서 확인하십시오.")
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
        a, b, n_fit, r2, within, b_se, b_ci, b_ts = fit
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
                    "a": round(a, 4),
                    # 두 자리로 줄인다 — 광고주별 n=12~13 에서 넷째 자리는
                    # 의미가 없고, 자리수가 정밀도로 읽힌다.
                    "b": round(b, 2), "b_se": round(b_se, 2),
                    # 🔴 «수확체감» 이라고 단정하던 문구를 고쳤다(2026-10-06).
                    #   기간을 섞은 적합은 b=0.939 였는데, 집행기간 버킷 안에서 다시 재면
                    #   b=0.991(~35일·362캠) 로 거의 비례다. 즉 그 수확체감의 상당 부분은
                    #   «기간이 짧은 캠페인과 긴 캠페인을 같은 선에 올린» 탓이었다.
                    #   b 를 보고 말을 고르게 한다 — 없는 포화를 있다고 말하지 않는다.
                    # 🔴 «포화인가» 를 내 임의 기준(b>=0.97)으로 가르지 않는다 —
                    #   표준오차로 «1 과 구분되는가» 를 묻는다. 안 그러면 b=0.94 를
                    #   포화라고 쓰는데, ±0.03 이면 1 이 아직 구간 안이다.
                    #   «구분되지 않는다» 와 «포화가 없다» 는 다른 말이다(f5 가 자기
                    #   「~35일 포화 없음」을 바로 그 이유로 정정했다).
                    # 판정은 «부트 구간» 으로 한다 — 닫힌식 SE 는 등분산을 가정해
                    # 불확실성을 과소평가한다(~35일 ±0.01 vs 부트 [0.96,1.03]).
                    "b_ci": [round(b_ci[0], 2), round(b_ci[1], 2)] if b_ci else None,
                    "b_theil_sen": round(b_ts, 2) if b_ts is not None else None,
                    "interpretation": (
                        f"노출을 2배로 늘리면 도달은 약 {2**b:.2f}배입니다 "
                        + (f"(b={b:.2f} · 95% {b_ci[0]:.2f}~{b_ci[1]:.2f}). " if b_ci
                           else f"(b={b:.2f}±{b_se:.2f}). ")
                        + ("노출을 늘릴수록 도달 증가분이 줄어듭니다(수확체감) — "
                           "95% 구간이 1 아래에 있습니다."
                           if (b_ci and b_ci[1] < 1.0) else
                           "b 가 1 과 «구분되지 않습니다» — 이 표본으로는 포화가 있는지 "
                           "가릴 수 없습니다. 포화가 없다는 뜻이 아닙니다.")
                        + (f" 이상점에 끌리지 않는 추정량으로도 {b_ts:.2f} 입니다."
                           if b_ts is not None else "")),
                    "source": "apac_kr_unified.v_meta_campaign_reach (캠페인 기간 전체 누적 유니크 도달)",
                    # 🔴 기간은 «안 적힌 가정» 이 되기 쉽다 — 반드시 응답에 실어 화면이 숨길 수 없게 한다
                    "period_basis": _fit_period_basis(day, market or ""),
                    # 적합이 약한 구간을 화면이 말해야 한다 — R² 가 낮으면 그 버킷의
                    # 캠페인들이 노출만으로 설명되지 않는다는 뜻이다(브랜드·타겟 차이).
                    # 광고주 안에서만 다시 잰 b 범위 — 합산이 b 를 올리는지 사용자가 알게 한다
                    "within_advertiser": within,
                    "fit_weak": r2 < 0.70,
                    "fit_weak_note": ("이 집행기간 구간은 적합이 약합니다(R²="
                                      f"{r2:.2f}) — 노출 말고 다른 요인이 도달을 크게 "
                                      "가릅니다. 구간 안 캠페인들의 타겟 범위가 서로 "
                                      "달라서일 수 있습니다. 중앙값으로 읽으십시오."
                                      if r2 < 0.70 else ""),
                    "bucket": ({"label": bk[2], "days_from": bk[0], "days_to": bk[1],
                                "n": n_fit, "thin": n_fit < BUCKET_N_THIN,
                                "requested_days": int(flight_days),
                                # 고른 기간이 적합 구간 «밖» 이면 외삽이다. 말하지 않으면
                                # 읽는 사람이 그 기간에서 재어진 값으로 받는다.
                                "extrapolated": int(flight_days) > bk[1],
                                "extrapolate_note": (
                                    f"고른 {int(flight_days)}일은 이 곡선을 적합한 구간"
                                    f"({bk[0]}~{bk[1]}일) 밖입니다 — 그 바깥으로 늘려 읽은 "
                                    "값입니다. 더 긴 집행은 표본이 10여 건뿐이라 따로 "
                                    "적합하면 아무 값이나 나옵니다(노출 범위 305배)."
                                    if int(flight_days) > bk[1] else "")} if bk else
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
                "구글·DV360 은 이 적합에 포함되지 않았습니다 — 수집을 안 해서가 아니라, "
                "CM360 노출 로그의 식별자(User_ID)가 전부 비어 있어 유니크 도달을 셀 수 "
                "없기 때문입니다(실측 식별불가 100%). 구글의 «예측» 곡선은 별도 API"
                "(ReachPlanService) 연동이 필요합니다.",
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
    # ⚠️ 벤더 중립 문구로 적는다. 처음엔 "넷플릭스가 억제했다" 로 박아뒀는데,
    #   이 상태값을 구글·메타 곡선도 쓰게 되면서 틀린 벤더 이름이 화면에 나간다.
    #   테스트(test_reach_cache.py)가 이걸 드러냈다.
    "suppressed":    {"label": "벤더 미공개", "usable": False,
                      "note": "모집단이 작아 매체가 출력을 억제한 조건입니다(미공개, 422). "
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
    kind = "forecast"
    measures = ("한 조건 안 — 같은 타게팅에서 노출을 늘렸을 때 넷플릭스가 예측하는 "
                "도달입니다. 우리 실측이 아니라 «넷플릭스의 예측» 입니다.")

    # 🔴 토큰이 아니라 «캐시» 가 가용 조건이다(DB f5 2026-10-06).
    #    호출 한도가 일 50 이라 요청 경로에서 부를 수 없다. 토큰만 보고 available=True 를
    #    돌려주면 화면이 실시간 조회를 전제로 짜이고, 그러면 다시 짜야 한다.
    # 🔴 «테이블이 있나» 가 아니라 «쓸 수 있는 곡선이 있나» 로 묻는다(DB f5 지적).
    #    DB 는 토큰이 오기 전에 스키마를 굳히려고 격자·캐시를 먼저 만든다. 테이블 존재로
    #    게이트를 걸면 그 시점에 제공자가 켜지고 화면이 곡선 0개로 열린다.
    #    0 은 «깨끗해서» 일 수도 «아직 아무것도 없어서» 일 수도 있다 — 구분해야 한다.
    def available(self, market=""):
        if not _view_exists(CURVE_VIEW):
            # 🔴 «내가 보지 않는 시스템의 상태를 단정하지 않는다» (2026-10-22)
            #   전에는 "토큰이 Secret Manager 에 없습니다" 라고 적어뒀다. 토큰이 들어온
            #   뒤에도 그 문장이 그대로 떠서 «이미 끝난 일» 을 시키고 있었다.
            #   이 서비스는 apac_kr_benchmark 만 읽는다 — 금고를 볼 수 없다. 못 보는
            #   것을 단정하면 반드시 틀린 때가 온다.
            #   그래서 내가 «보는 것»(곡선 캐시)만 말하고, 금고 상태는 그걸 실제로
            #   확인하는 도구(backend/status.py)로 넘긴다.
            return False, ("넷플릭스 도달 곡선이 아직 캐시에 없습니다. 호출 한도가 일 "
                           f"{NETFLIX_RATE_LIMITS['day']}회라 «야간 격자 선계산 → 캐시 → "
                           "화면은 캐시만 조회» 가 전제입니다. "
                           "준비 순서: ① NetKey 주입 ② 첫 호출로 Staging 이 실제 값인지 확인 "
                           "③ 격자 선계산 ④ 여기가 자동으로 켜집니다. "
                           "지금 어디까지 됐는지는 backend/status.py 가 알려줍니다.")
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
        # ⚠️ 여기에 넷플릭스 API 직접 호출을 넣지 마십시오 — 한도가 일
        #   NETFLIX_RATE_LIMITS['day']회입니다. 선계산 캐시만 읽습니다.
        return _cache_curve_response(
            self, CURVE_VIEW, kw.get("media", ""), kw.get("market", ""),
            kw.get("flight_days", 30), kw.get("budget", 2_000_000_000),
            kw.get("points", 20))


# ── 벤더 예측 곡선 — 구글 · 메타 ────────────────────────────────────
# 🔑 세 벤더 모두 «예측 API» 를 가지고 있다. 넷플릭스만 특별한 게 아니다.
#     넷플릭스  Reach Curve API
#     구글      Google Ads ReachPlanService  (YouTube·디스플레이)
#     메타      Reach & Frequency API
#   셋은 모두 «한 조건 안에서 노출을 늘리면» 을 답하고, 우리 멱함수 적합은
#   «캠페인 사이» 를 답한다. 질문이 달라 모양이 다른 것이 정상이다.
#
# ⚠️ 구글·메타 예측 API 는 아직 «요청도 안 된» 상태다. 수집되면 DB 가 아래 테이블을
#   채우고, 그러면 이 제공자들이 스스로 켜진다. 넷플릭스와 같은 구조로 둔 이유는
#   토큰·권한이 오는 순서를 우리가 통제할 수 없기 때문이다.
# ⚠️ 게이트는 «테이블 존재» 가 아니라 «쓸 수 있는 곡선이 있나» 다. 빈 스키마가 먼저
#   만들어져도 화면이 곡선 0개로 열리지 않는다(넷플릭스에서 겪은 그 함정).
GOOGLE_CURVE_VIEW = f"`{PROJECT}.{MART_DS}.v_google_reach_plan`"
META_RF_CURVE_VIEW = f"`{PROJECT}.{MART_DS}.v_meta_rf_curve`"


def _vendor_curve_counts(view, market=""):
    """(쓸 수 있는 곡선 수, 격자 전체 수). 뷰가 없으면 (0, 0)."""
    if not _view_exists(view):
        return 0, 0
    c = _client()
    where = "WHERE country = @mk" if market else ""
    job = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("mk", "STRING", market or "KR")])
    try:
        rows = list(c.query(f"SELECT IFNULL(curve_status,'not_requested') st, COUNT(*) n "
                            f"FROM {view} {where} GROUP BY st", job_config=job).result())
    except Exception:
        return 0, 0
    n_all = sum(r["n"] for r in rows)
    n_ok = sum(r["n"] for r in rows if CURVE_STATUS.get(r["st"], {}).get("usable"))
    return n_ok, n_all


# ── 캐시 곡선 해석기 ───────────────────────────────────────────────
# 🔑 «질의» 와 «해석» 을 나눈다. 해석기는 순수 함수라 실제 데이터가 오기 전에
#   가짜 행으로 검증할 수 있다. 안 나누면 「구현했다」가 검증되지 않은 주장이 된다.
#   (DB 와 서로 열 번 겪은 자리 — 못 쟀으면 못 쟀다고 적는다)
CURVE_SCOPE_NOTE = {
    "advertiser": "",
    "agency_shared": ("⚠️ 이 곡선은 «대행사 공용 계정» 기준입니다. 미등록 광고주 여러 곳이 "
                      "한 계정을 쓰므로 특정 광고주의 도달로 읽지 마십시오 — "
                      "분모가 다른 집단입니다."),
    "unknown": ("⚠️ 이 곡선이 어느 광고 계정 기준인지 기록되지 않았습니다. "
                "광고주별 플래닝 근거로 쓰기 전에 출처를 확인하십시오."),
}


def points_from_cache(rows, cpm, budget, points=20):
    """캐시 행 → 화면이 그릴 점 목록. 순수 함수(쿼리 없음).

    rows: v_*_reach 한 조건의 행들. 각 행은
          curve_status · impression_reach[{impression, reach}] · points · ad_account_scope
    cpm:  예산→노출 환산 단가(원). 곡선은 «노출» 축이고 화면은 «예산» 축이다.

    ⚠️ 상태가 ok 가 아닌 행은 점을 만들지 않고 이유를 돌려준다.
      suppressed(미공개) · not_permitted(불가) · fetch_failed(실패) · not_requested(미조회)
      를 한 칸에 섞으면 화면이 «도달 0» 으로 그린다 — 플래너가 «효과 없는 조건» 으로 읽는다.
    ⚠️ impression_reach 는 REPEATED STRUCT 다. 스칼라로 받으면 한 행 때문에 전체가 깨진다
      (카카오 status 가 ["OFF"] 였던 그 함정).
    """
    if not rows:
        return None, "격자에 이 조건이 없습니다(아직 등록되지 않았습니다)."
    ok = [r for r in rows if (r.get("curve_status") or "") == "ok"]
    if not ok:
        st = (rows[0].get("curve_status") or "not_requested")
        meta = CURVE_STATUS.get(st, {"note": f"알 수 없는 상태입니다({st})."})
        return None, meta.get("note") or st
    row = ok[0]
    arr = row.get("impression_reach") or []
    if not isinstance(arr, (list, tuple)) or not arr:
        return None, "곡선 점이 비어 있습니다(수집은 성공했는데 값이 없습니다)."
    npts = row.get("points")
    warn = []
    # 🔴 «100점» 은 스펙 문서의 값이고 실제가 아니다 (DB 첫 실호출, 2026-10-22)
    #   실호출 결과는 86점이고 노출 간격도 등간격이 아니라 «로그 간격»(1,000~5,656,000)이다.
    #   전에는 points != 100 이면 경고했는데, 그러면 «멀쩡한 곡선마다» 경고가 뜬다 —
    #   그런 경고는 곧 무시되고, 진짜 문제가 섞여 들어와도 안 보인다.
    #   그래서 «개수» 가 아니라 «쓸 수 있는가» 로 본다: 점이 너무 적은가 · 단조증가하는가.
    if npts is not None and npts < 4:
        warn.append(f"곡선 점이 {npts}개뿐입니다 — 보간할 수 없어 믿기 어렵습니다.")
    scope = (row.get("ad_account_scope") or "unknown")
    if CURVE_SCOPE_NOTE.get(scope):
        warn.append(CURVE_SCOPE_NOTE[scope])

    pairs = []
    for x in arr:
        try:
            imp = float(x["impression"] if isinstance(x, dict) else x.impression)
            rch = float(x["reach"] if isinstance(x, dict) else x.reach)
        except (KeyError, TypeError, AttributeError, ValueError):
            continue
        if imp > 0 and rch >= 0:
            pairs.append((imp, rch))
    if not pairs:
        return None, "곡선 점을 읽을 수 없습니다(impression_reach 구조가 예상과 다릅니다)."
    pairs.sort()
    # 노출이 늘었는데 도달이 «줄면» 곡선이 아니다. 이것이 쓸 수 있는가의 실제 조건이다.
    #   ⚠️ 오목성(한계도달 체감)은 검사하지 않는다 — DB 실측으로 위반 7건이 나오는데
    #     전부 «정확히 한 양자화 단계» 다(노출 간격 1,000 · 도달 정수 → 기울기 해상도
    #     0.001, 지역 기울기의 0.12%). 허용오차 없이 걸면 멀쩡한 곡선이 매번 걸린다.
    drops = sum(1 for i in range(1, len(pairs)) if pairs[i][1] < pairs[i - 1][1])
    if drops:
        return None, (f"노출이 늘었는데 도달이 줄어드는 지점이 {drops}곳 있습니다 — "
                      "곡선으로 쓸 수 없습니다(수집 오류로 보입니다).")

    # 예산 축으로 자른다. 곡선은 노출 축이므로 예산 = 노출 × CPM / 1000.
    # ⚠️ 캐시의 최대 노출을 넘는 예산은 «외삽» 이다 — 늘려 그리지 않고 거기서 멈춘다.
    max_imp = pairs[-1][0]
    cap_cost = max_imp * (cpm or 0) / 1000.0
    hi = min(float(budget or 0), cap_cost) if cap_cost > 0 else 0.0
    if hi <= 0:
        return None, "예산→노출 환산 CPM 이 없어 예산 축에 올릴 수 없습니다."
    out, n = [], max(2, int(points))
    for i in range(1, n + 1):
        cost = hi * i / n
        imps = cost / (cpm / 1000.0)
        # 선형 보간 — 점이 100개면 구간이 촘촘해 보간 오차가 작다
        rch = pairs[0][1]
        for j in range(1, len(pairs)):
            x0, y0 = pairs[j - 1]
            x1, y1 = pairs[j]
            if imps <= x1:
                rch = y0 + (y1 - y0) * ((imps - x0) / (x1 - x0)) if x1 > x0 else y1
                break
            rch = y1
        out.append({"cost": round(cost), "imps": round(imps), "reach": round(rch),
                    "frequency": round(imps / rch, 2) if rch > 0 else None})
    truncated = cap_cost > 0 and float(budget or 0) > cap_cost
    if truncated:
        warn.append(f"요청 예산이 캐시된 곡선의 끝(약 {cap_cost/1e8:.1f}억)을 넘어 "
                    "거기서 멈췄습니다 — 그 바깥은 캐시에 없습니다.")

    # 🔑 도달률(%)의 분모 — «행마다» 들고 있는 모집단을 쓴다 (DB f5/d9 계약, 2026-10-21)
    #   메타는 delivery_estimate 로 모집단을 «권한 없이» 준다(실측: ES 25~54 →
    #   2,090만~2,460만). 넷플릭스는 가입자 규모를 몰라 한동안 assumed 다.
    #   ⚠️ 점이 아니라 «범위» 다(폭 ±8%). 단일 %를 내면 그 자체가 가정이 되므로
    #     밴드로 그린다. 상·하한을 각 점에 실어 화면이 띠로 칠할 수 있게 한다.
    #   ⚠️ 🔴 타게팅마다 모집단이 다르다. 그래서 전역 상수가 아니라 행에서 읽는다 —
    #     전역으로 두면 뒤에 다시 부를 때 과거 도달률이 «조용히» 바뀐다.
    #   ⚠️ population_source 를 반드시 끌고 올라간다. vendor_api 와 assumed 를 한 칸에
    #     섞으면 화면에서 둘이 같은 신뢰도로 보인다.
    # 🔴 «마지막 점» 을 모집단이라고 부르지 않는다 (DB 정정, 2026-10-22)
    #   첫 호출의 도달 6,482,607 은 점근선이 아니라 «API 가 돌려준 마지막 점» 이다.
    #   그 구간의 한계도달이 아직 3.4/천 이라 0 이 아니고, 멱 모형(b≈0.89)에는
    #   점근선 자체가 없다. 분모로 쓰면 도달률이 약 +0.3% 과대해진다.
    #   작지만, «모집단» 이라고 이름 붙이면 넷플릭스가 실제 가입자 수를 주는 순간
    #   둘이 어긋나고 다음 사람이 결함으로 읽는다.
    pop_lo = row.get("population_lower")
    pop_hi = row.get("population_upper")
    if pop_lo is None and pop_hi is None:
        clr = row.get("curve_last_reach")
        if clr:
            pop_lo = pop_hi = clr
            row = dict(row, population_source="derived_from_curve")
    pop_src = (row.get("population_source") or "").strip() or None
    pop = None
    if pop_lo or pop_hi:
        lo = float(pop_lo or pop_hi or 0)
        hi = float(pop_hi or pop_lo or 0)
        if lo > 0 and hi > 0:
            if lo > hi:
                lo, hi = hi, lo
            for p in out:
                # 분모가 크면 비율이 작다 — 상한 모집단이 «하한 도달률» 을 만든다
                p["reach_pct_lower"] = round(p["reach"] / hi * 100, 2)
                p["reach_pct_upper"] = round(p["reach"] / lo * 100, 2)
            pop = {"lower": round(lo), "upper": round(hi), "source": pop_src or "unknown",
                   "is_vendor": pop_src == "vendor_api",
                   "unit": "프로필",   # 사람·가구가 아니다 (넷플릭스 스펙)
                   "is_curve_last": pop_src == "derived_from_curve",
                   "spread_pct": round((hi - lo) / ((hi + lo) / 2) * 100, 1)}
            # 🔴 단위가 «사람» 이 아니라 «프로필» 이다 (넷플릭스 스펙 명시).
            #   가구도 사람도 아니다. «도달률 %» 로만 띄우면 보는 사람은 사람 비율로 읽는다.
            if pop_src == "derived_from_curve":
                warn.append(f"도달률의 분모는 «곡선의 마지막 점»({round(hi):,} 프로필)입니다 "
                            "— 모집단이 아닙니다. 그 지점에서도 도달이 아직 늘고 있어 "
                            "실제 비율은 이보다 조금 낮습니다. 단위는 «프로필» 이고 "
                            "사람 수가 아닙니다.")
            elif pop_src != "vendor_api":
                warn.append(f"도달률의 분모가 «가정» 입니다(출처 {pop_src or '미기록'}) — "
                            "벤더가 준 모집단이 아닙니다. 비율보다 절대 도달로 읽으십시오. "
                            "단위는 «프로필» 이고 사람 수가 아닙니다.")
            else:
                warn.append(f"도달률은 범위입니다 — 모집단 {round(lo):,}~{round(hi):,} "
                            f"«프로필»(벤더 제공, 폭 ±{pop['spread_pct']/2:.0f}%). "
                            "단일 %가 아니라 띠로 읽으십시오. 프로필은 사람 수가 아닙니다.")
    return {"points": out, "warnings": warn, "scope": scope,
            "max_imp": round(max_imp), "max_reach": round(pairs[-1][1]),
            "n_cache_points": len(pairs), "population": pop}, None


def _fetch_cache_rows(view, market, flight_days, targeting_label=""):
    """캐시 조회 — v_*_reach «뷰만» 읽는다(격자 LEFT JOIN 결과라 미조회 칸도 행으로 온다).

    ⚠️ 테이블을 직접 읽으면 채워진 것만 보여 커버리지가 늘 100% 로 나온다(DB 지침).
    """
    if not _view_exists(view):
        return []
    c = _client()
    w = ["TRUE"]
    prm = []
    if market:
        w.append("country = @mk")
        prm.append(bigquery.ScalarQueryParameter("mk", "STRING", market.upper()))
    if flight_days:
        w.append("flight_days = @fd")
        prm.append(bigquery.ScalarQueryParameter("fd", "INT64", int(flight_days)))
    if targeting_label:
        w.append("targeting_label = @tl")
        prm.append(bigquery.ScalarQueryParameter("tl", "STRING", targeting_label))
    try:
        return [dict(r) for r in c.query(
            f"SELECT * FROM {view} WHERE {' AND '.join(w)} "
            f"ORDER BY IF(IFNULL(curve_status,'')='ok',0,1) LIMIT 20",
            job_config=bigquery.QueryJobConfig(query_parameters=prm)).result()]
    except Exception:
        return []


def _cache_curve_response(provider, view, media, market, flight_days, budget, points):
    """캐시에서 곡선을 읽어 화면 응답 형태로 돌려준다. 못 읽으면 이유를 들고 올린다."""
    import datetime
    day = datetime.date.today().isoformat()
    cpm, cpm_src = _cpm(media or "", market or "", day)
    rows = _fetch_cache_rows(view, market, flight_days)
    parsed, why = points_from_cache(rows, cpm, budget, points)
    if parsed is None:
        raise RuntimeError(why)
    pts = parsed["points"]
    pop = parsed.get("population")
    return {
        "provider": provider.name, "provider_label": provider.label,
        "fitted": True, "kind": provider.kind,
        "estimate_badge": f"{provider.label} — 선계산 캐시 {parsed['n_cache_points']}점",
        "points": pts,
        "measured": {"cpm": round(cpm, 2), "cpm_source": cpm_src,
                     "note": "예산→노출은 실측 CPM, 노출→도달은 벤더 예측 곡선입니다."},
        "cache": {"scope": parsed["scope"], "max_imp": parsed["max_imp"],
                  "max_reach": parsed["max_reach"], "flight_days": int(flight_days or 0),
                  "population": pop},
        "measures": provider.measures,
        "caveats": ([
            "이 곡선은 벤더의 «예측» 입니다 — 우리가 집행해서 관찰한 값이 아닙니다.",
            provider.measures,
        ] + parsed["warnings"] + ([
            "도달률(%)은 이 조건의 모집단이 캐시에 없어 표시하지 않습니다 — "
            "절대 도달로 읽으십시오."
        ] if not pop else [])),
    }


# ⚠️ 폐기 — 전역 모집단을 쓰지 않는다.
#   한때 시장 단위로 모집단을 하나 두려 했는데, DB 실측으로 «타게팅마다 모집단이 다르다»
#   가 확인됐다(메타 delivery_estimate). 전역으로 두면 조건을 바꿔도 분모가 그대로여서
#   도달률이 틀리고, 나중에 값을 갱신하면 과거 도달률이 «조용히» 바뀐다.
#   분모는 곡선 캐시 «행» 에서 읽는다(population_lower/upper/source).


class _VendorForecastProvider(Provider):
    """벤더 예측 곡선의 공통 틀. 캐시가 채워지면 켜진다."""
    kind = "forecast"
    view = ""
    api_name = ""
    blocked_reason = ""

    def available(self, market=""):
        n_ok, n_all = _vendor_curve_counts(self.view, market)
        if n_ok:
            return True, f"사용 가능 — 곡선 {n_ok:,}개" + (f" / 격자 {n_all:,}" if n_all else "")
        if n_all:
            return False, (f"격자 {n_all:,}개가 등록됐지만 아직 쓸 수 있는 곡선이 없습니다.")
        return False, self.blocked_reason

    def curve(self, **kw):
        ok, why = self.available(kw.get("market", ""))
        if not ok:
            raise RuntimeError(why)
        return _cache_curve_response(
            self, self.view, kw.get("media", ""), kw.get("market", ""),
            kw.get("flight_days", 30), kw.get("budget", 2_000_000_000),
            kw.get("points", 20))


class GoogleReachPlanProvider(_VendorForecastProvider):
    name = "google_reachplan"
    label = "Google Ads ReachPlanService"
    fitted = True
    view = GOOGLE_CURVE_VIEW
    api_name = "ReachPlanService"
    measures = ("한 조건 안 — 같은 타게팅에서 예산을 늘렸을 때 구글이 예측하는 "
                "YouTube·디스플레이 도달입니다. 우리 실측이 아니라 «구글의 예측» 입니다.")
    # 실측(2026-10-21): Secret Manager 29개 전수에 구글 광고 토큰이 없다.
    #   1PDAuto 의 developer_token 은 «마이크로소프트 광고» 용이고 캐나다에서 별도로 받은
    #   것이라 재사용 불가다 — 본사 전용으로 새로 받아야 한다(사용자 확인).
    #   데이터 전송 설정에서 관리자 계정(MCC) 3개를 확인했다.
    blocked_reason = ("구글 개발자 토큰이 없습니다. 우리 구글 데이터는 Data Transfer"
                      "(실적 테이블)로 들어오고, 도달 예측은 Google Ads API 의 별도 "
                      "서비스입니다 — 구글 광고 API 는 개발자 토큰 없이는 호출 자체가 "
                      "불가능합니다. 해제 조건: 관리자 계정(MCC) API 센터에서 "
                      "기본(Basic) 등급 토큰을 확인하십시오 — 후보 MCC "
                      "8286530634(lc2015) · 7297527650(dplan360) · 9958618792(hmca). "
                      "없으면 기본 등급을 신청해야 하고 승인 심사가 걸립니다. "
                      "⚠️ 테스트 등급은 즉시 발급되지만 테스트 계정만 되어 실제 도달 예측이 "
                      "나오지 않습니다 — 기본 등급이어야 합니다.")


class MetaRFProvider(_VendorForecastProvider):
    name = "meta_rf"
    label = "Meta Reach & Frequency API"
    fitted = True
    view = META_RF_CURVE_VIEW
    api_name = "Reach & Frequency API"
    measures = ("한 조건 안 — 같은 타게팅에서 예산을 늘렸을 때 메타가 예측하는 "
                "도달입니다. 아래 «메타 실측 적합» 과 다른 것입니다(이쪽은 예측).")
    # 실측(2026-10-21): 기존 토큰으로 R&F 엔드포인트를 읽으니 메타가 직접 답했다.
    #   GET act_<id>/reachfrequencypredictions
    #   → HTTP 403 (#200) Requires ads_management permission to manage the object
    #   토큰 권한: read_insights · ads_read · pages_* (SYSTEM_USER · 유효)
    #   즉 «새 연동» 이 아니라 «권한 한 줄» 이다 — 호스트·토큰·계정이 모두 그대로다.
    blocked_reason = ("메타 토큰에 ads_management 권한이 없습니다(현재 ads_read 까지). "
                      "R&F 예측 생성에 그 권한이 필요하며 메타가 직접 그렇게 답합니다 — "
                      "HTTP 403 (#200) Requires ads_management permission. "
                      "해제 조건: 비즈니스 관리자에서 ads_management 를 부여해야 합니다"
                      "(새 앱·새 연동 불필요 — 호스트·토큰·계정 그대로). "
                      "⚠️ 다만 교환이 비대칭입니다(DB 실측 180일): R&F 가 덮는 예약형"
                      "(RESERVED) 집행은 ₩1.82억으로 메타 지출의 1.9% 인데, 이 토큰이 "
                      "덮는 범위는 13계정 ₩94.5억입니다 — 1.9% 를 계획하려고 전체에 "
                      "«쓰기» 권한을 붙이는 셈입니다. 그래서 R&F 전용 시스템 사용자를 "
                      "따로 만들어 해당 7계정만 묶는 쪽을 권합니다. "
                      "자격 자체는 충족돼 보입니다 — 예약형 캠페인 172개가 "
                      "도달·인지 목표군으로 실제 돌고 있습니다. "
                      "다만 R&F 예측 가능 여부는 계정·국가·목표 조합별이라 불러봐야 압니다.")


_PROVIDERS = [AssumptionProvider(), FittedReachProvider(), NetflixReachProvider(),
              GoogleReachPlanProvider(), MetaRFProvider()]


def providers():
    out = []
    for p in _PROVIDERS:
        ok, why = p.available()
        out.append({"name": p.name, "label": p.label, "fitted": p.fitted,
                    "kind": getattr(p, "kind", "forecast"),
                    "measures": getattr(p, "measures", ""),
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


def lookup_curve(market="KR", flight_days=30, value_ids=None, media="",
                 budget=2_000_000_000, points=20):
    """고른 타게팅 조합의 곡선을 캐시에서 찾는다. 없으면 «왜 없는지» 를 돌려준다.

    ★ 조합을 «라벨» 이 아니라 «타게팅 값 ID 배열» 로 맞춘다.
      라벨 형식(문자열 조합 규칙)은 DB 가 정하는 것이고, 내가 추측하면 한쪽이 바꿀 때
      조용히 안 맞는다. ID 배열은 양쪽이 같은 것을 가리킨다.

    ⚠️ 못 찾은 것과 «계산 실패» 는 다르다.
      미등록   격자에 이 조합이 없다 → 다음 야간 목록에 넣으면 생긴다
      미조회   격자엔 있는데 아직 안 불렀다 → 기다리면 생긴다(일 50회라 며칠 걸린다)
      불가     벤더가 거절한 조건이다 → 기다려도 안 생긴다. 조건을 바꿔야 한다
      이 셋을 한 칸에 넣으면 플래너가 «안 되는 것» 과 «아직인 것» 을 구분 못 한다.
    """
    ids = [x for x in (value_ids or []) if x]
    if not _view_exists(CURVE_VIEW):
        return {"found": False, "state": "no_cache",
                "note": ("도달 곡선 캐시가 아직 만들어지지 않았습니다. "
                         "야간 선계산이 시작되면 조합별로 채워집니다.")}
    c = _client()
    where = ["country = @mk", "flight_days = @fd"]
    prm = [bigquery.ScalarQueryParameter("mk", "STRING", (market or "KR").upper()),
           bigquery.ScalarQueryParameter("fd", "INT64", int(flight_days or 30))]
    if ids:
        # 고른 값이 «전부» 들어 있고, 개수도 같은 행 (부분 일치를 같은 조합으로 보지 않는다)
        where.append("ARRAY_LENGTH(targeting_value_ids) = @n")
        prm.append(bigquery.ScalarQueryParameter("n", "INT64", len(ids)))
        for i, v in enumerate(ids):
            where.append(f"@v{i} IN UNNEST(targeting_value_ids)")
            prm.append(bigquery.ScalarQueryParameter(f"v{i}", "STRING", v))
    else:
        where.append("ARRAY_LENGTH(IFNULL(targeting_value_ids, [])) = 0")
    try:
        rows = [dict(r) for r in c.query(
            f"SELECT * FROM {CURVE_VIEW} WHERE {' AND '.join(where)} LIMIT 5",
            job_config=bigquery.QueryJobConfig(query_parameters=prm)).result()]
    except Exception as e:
        return {"found": False, "state": "error", "note": f"조회 실패: {str(e)[:120]}"}

    if not rows:
        return {"found": False, "state": "not_in_grid",
                "note": ("이 조합은 아직 «계산 대기» 입니다 — 미리 계산된 목록에 없습니다. "
                         "호출 한도가 일 50회라 자주 쓰는 조합부터 채웁니다. "
                         "이 조합이 필요하시면 다음 야간 목록에 넣어 드립니다.")}
    import datetime
    cpm, cpm_src = _cpm(media or "", market or "", datetime.date.today().isoformat())
    parsed, why = points_from_cache(rows, cpm, budget, points)
    if parsed is None:
        st = (rows[0].get("curve_status") or "not_requested")
        meta = CURVE_STATUS.get(st, {})
        return {"found": False, "state": st, "note": why,
                "can_wait": st in ("not_requested", "fetch_failed"),
                "label": meta.get("label", st)}
    return {"found": True, "state": "ok", "points": parsed["points"],
            "warnings": parsed["warnings"], "population": parsed.get("population"),
            "max_reach": parsed["max_reach"],
            "cpm": round(cpm, 2), "cpm_source": cpm_src}


def compare_curves(budget=2_000_000_000, media="", market="KR", universe=None,
                   points=20, k=DEFAULT_K, flight_days=30):
    """쓸 수 있는 제공자 전부를 «같은 예산 축» 에 올려 돌려준다.

    ★ 왜 나란히 놓는가
      넷플릭스·구글·메타가 각각 예측 API 를 가지고 있고, 우리는 메타 실측 적합을 따로
      가지고 있다. 플래너가 「이 예산이면 어디서 얼마나 닿나」를 물을 때 한 벤더만
      보여주면 그 벤더 기준으로만 판단한다.

    🔴 모양이 서로 «다르게» 나오는 것이 정상이다 — 재는 대상이 다르다.
      예측(forecast)  한 조건 안에서 노출을 늘렸을 때의 포화. 벤더가 계산한다
      실측(measured)  캠페인 사이의 관계. 예산이 큰 캠페인은 타겟도 넓게 잡는다
      가정(assumption) 우리 자료가 아니라 가정식의 모양
      그래서 제공자마다 «무엇을 재는가» 를 같이 내려보낸다. 이 줄이 없으면 보는 사람이
      차이를 «어느 쪽이 틀렸다» 로 읽는다.

    ⚠️ «못 쓰는 제공자» 도 이유와 함께 돌려준다. 빠뜨리면 화면이 지금 가진 것만
      보여주고, 플래너는 그게 전부라고 생각한다.
    """
    series, blocked = [], []
    for p in _PROVIDERS:
        try:
            ok, why = (p.available(market) if p.name in ("netflix", "google_reachplan",
                                                         "meta_rf") else p.available())
        except TypeError:
            ok, why = p.available()
        meta = {"name": p.name, "label": p.label, "kind": getattr(p, "kind", "forecast"),
                "measures": getattr(p, "measures", ""), "fitted": p.fitted}
        if not ok:
            blocked.append(dict(meta, reason=why))
            continue
        try:
            d = p.curve(budget=budget, media=media, market=market, universe=universe,
                        points=points, k=k, flight_days=flight_days)
        except Exception as e:  # noqa: BLE001
            blocked.append(dict(meta, reason=str(e)))
            continue
        series.append(dict(meta,
                           badge=d.get("estimate_badge", ""),
                           points=d.get("points", []),
                           measured=d.get("measured"),
                           fit=d.get("fit"),
                           # 화면이 모집단 출처(vendor_api/assumed)를 읽어야 한다 —
                           # 안 실으면 밴드는 그려지는데 «무엇으로 나눈 비율인지» 가 사라진다
                           cache=d.get("cache"),
                           caveats=d.get("caveats", [])))
    return {
        "budget": budget, "points": points, "market": market, "media": media,
        "flight_days": int(flight_days),
        "series": series, "blocked": blocked,
        "n_available": len(series), "n_blocked": len(blocked),
        "read_me": ("곡선 모양이 서로 다른 것은 결함이 아닙니다 — 각 곡선이 «재는 대상» 이 "
                    "다릅니다. 예측(벤더가 한 조건 안에서 계산)과 실측(우리가 캠페인 사이에서 "
                    "관찰)은 같은 질문의 답이 아닙니다. 아래 «재는 것» 을 함께 보십시오."),
        "axis_note": ("가로축은 같은 예산입니다. 다만 예산→노출 환산 CPM 은 제공자마다 "
                      "다를 수 있으니 각 곡선의 CPM 출처를 확인하십시오."),
    }


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
