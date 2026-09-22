# -*- coding: utf-8 -*-
"""도달(Reach 1+) 시뮬레이터 — PPT (c) '도달 시뮬레이터' 메뉴.

★ 설계 원칙: **실측하지 않은 것을 실측한 것처럼 그리지 않는다.**

현재 가진 데이터로는 «우리 데이터에 적합한 도달 곡선» 을 만들 수 없다. DB 에이전트 실측 근거:
  · DV360 : reach 컬럼 자체가 없다(수집 안 함).
  · Meta  : reach 는 있으나(853,061행·95.8%) **일자 단위**다. reach 는 유니크 값이라 날짜를 더할 수 없다.
            실증 — Hyundai_H-Promise 42일: 노출 13.7억, 일별 reach 합 12.9억, 일 최대 reach 3,414만.
            합이 최대의 **38배**. 같은 사람을 매일 다시 센 값이다.
            그 결과 빈도의 93.8%가 1.5 미만 → **포화 구간이 없어 곡선 적합이 불가능**하다.
            (`apac_kr_ops.dictionary_column_notes` 에 severity='do_not_use' 로 박혀 있다)
  · 넷플릭스: NAS 52행·7주뿐이고 원천에 frequency·reach 가 없다.

그래서 제공자(provider)를 분리했다. 지금 동작하는 것은 **가정 기반 추정** 하나뿐이고,
응답에 `fitted: False` 와 `assumptions` 를 실어 화면이 그 사실을 숨길 수 없게 한다.

제공자 교체 경로 (인터페이스 동일):
  AssumptionProvider  (지금)      노출/원 = 실측 CPM, 노출→도달 = 접촉분포 가정
  FittedReachProvider (예정)      Meta lifetime reach(캠페인×전기간 재수집) 적합 → DB 승인 대기
  NetflixReachProvider(예정)      Netflix Reach Curve API — 이노션 전용 토큰 발급 예정
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


class FittedReachProvider(Provider):
    name = "fitted"
    label = "실측 적합 (Meta lifetime reach)"
    fitted = True

    def available(self):
        # 캠페인×전기간 reach 재수집이 들어오면 활성. 일자별 reach 로는 적합 불가.
        try:
            c = _client()
            c.get_table(f"{PROJECT}.apac_kr_ops.v_meta_reach_lifetime")
            return True, "사용 가능"
        except Exception:
            return False, ("Meta lifetime reach(캠페인×전기간) 재수집 대기 중. "
                           "현재 있는 v_meta_reach_daily 는 일자 단위라 합산이 불가해 적합에 쓸 수 없습니다.")

    def curve(self, **kw):
        raise RuntimeError(self.available()[1])


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
