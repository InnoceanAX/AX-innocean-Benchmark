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
from functools import lru_cache
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
LOCATION = "asia-northeast3"
CAMP_TBL = f"`{PROJECT}.apac_kr_benchmark.bm_campaign_monthly`"
DPLAN_TBL = f"`{PROJECT}.apac_kr_benchmark.bm_dplan_creative_monthly`"

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
        q = (f"SELECT SAFE_DIVIDE(SUM(IF(cost_ok,cost,0)),SUM(IF(cost_ok,imp,0)))*1000 cpm, "
             f"SUM(IF(cost_ok,imp,0)) imp FROM {DPLAN_TBL} WHERE imp > 0 {mf}")
        src = "bm_dplan_creative_monthly (디플랜 NAS 실측, 금액기준 확인분만)"
    r = list(c.query(q).result())[0]
    cpm = r["cpm"]
    if not cpm or cpm <= 0 or not r["imp"]:
        # 해당 매체 실측이 없으면 전체 평균으로 후퇴(그 사실을 소스 문자열에 남긴다)
        r2 = list(c.query(f"SELECT SAFE_DIVIDE(SUM(cost),SUM(imp))*1000 cpm FROM {CAMP_TBL} "
                          f"WHERE imp > 0").result())[0]
        return (r2["cpm"] or 3000.0), "전체 평균 CPM (해당 매체 실측 없음)"
    return float(cpm), src


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

    def curve(self, budget, media, market, universe, points, k=DEFAULT_K):
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


REACH_VIEW = f"`{PROJECT}.apac_kr_unified.v_meta_campaign_reach`"
FIT_MIN_CAMPAIGNS = 12   # 이보다 적으면 그 시장은 적합하지 않는다(과적합 방지)


def _view_exists(fq):
    try:
        _client().get_table(fq.strip("`"))
        return True
    except Exception:
        return False


@lru_cache(maxsize=64)
def _fit(market, day):
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
    rows = list(c.query(
        f"SELECT impressions imp, unique_reach rch FROM {REACH_VIEW} "
        f"WHERE unique_reach > 0 AND impressions > 0 {mf}",
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

    def curve(self, budget, media, market, universe, points, k=None):
        import datetime
        day = datetime.date.today().isoformat()
        fit = _fit(market or "", day)
        scope = f"{market} 시장" if market else "전체 시장"
        if fit is None and market:                 # 그 시장 표본이 적으면 전체로 후퇴
            fit, scope = _fit("", day), "전체 시장(해당 시장 표본 부족)"
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
                    "a": round(a, 4), "b": round(b, 4),
                    "interpretation": (f"노출을 2배로 늘리면 도달은 약 {2**b:.2f}배가 됩니다"
                                       f" (b={b:.3f} < 1 이므로 수확체감)."),
                    "source": "apac_kr_unified.v_meta_campaign_reach (캠페인 기간 전체 누적 유니크 도달)"},
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


class NetflixReachProvider(Provider):
    name = "netflix"
    label = "Netflix Reach Curve API"
    fitted = True

    def available(self):
        if os.environ.get("NETFLIX_ADS_TOKEN"):
            return True, "사용 가능"
        return False, ("넷플릭스 광고 API 토큰 미발급. 이노션 전용 토큰 발급 예정 — "
                       "발급 후 NETFLIX_ADS_TOKEN 시크릿을 주입하면 자동 활성화됩니다.")

    def curve(self, **kw):
        raise RuntimeError(self.available()[1])


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


def curve(budget=2_000_000_000, media="", market="KR", universe=None, points=20,
          provider=None, k=DEFAULT_K):
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
                       universe=universe, points=points, k=k)
    res["providers"] = providers()["providers"]
    return res


if __name__ == "__main__":
    import json
    print(json.dumps(providers(), ensure_ascii=False, indent=2))
    r = curve(budget=2_000_000_000, media="넷플릭스", points=8)
    print(f"\n{r['estimate_badge']} · CPM ₩{r['measured']['cpm']:,.0f} ({r['measured']['cpm_source']})")
    for p in r["points"]:
        print(f"  ₩{p['cost']:>13,}  노출 {p['imps']:>12,}  도달 {p['reach_pct']:>6}%  빈도 {p['frequency']}")
