# -*- coding: utf-8 -*-
"""캐시 곡선 해석기 검증 — 실제 데이터가 오기 전에 가짜 행으로 댄다.

왜 이 파일이 있나
  벤더 곡선(넷플릭스·구글·메타)은 자격증명이 풀릴 때까지 한 행도 들어오지 않는다.
  그 상태에서 「조회 구현 완료」라고 적으면 검증되지 않은 주장이 된다. 그래서 질의와
  해석을 나누고, 해석기를 여기서 댄다 — 데이터가 오면 질의만 붙는다.

★ 성공 경로만 보면 «아무것도 안 하는 코드» 도 통과한다(DB 지침). 그래서 네 상태
  (ok · suppressed · not_permitted · not_requested)와 깨진 입력을 다 댄다.

실행: python test_reach_cache.py
"""
import reach


def _ir(n=100, scale=1.0):
    """impression_reach 배열 — 노출이 늘수록 도달이 체감하는 모양."""
    out = []
    for i in range(1, n + 1):
        imp = i * 1_000_000
        out.append({"impression": imp, "reach": int(imp ** 0.9 * scale)})
    return out


def _row(status="ok", pts=100, scope="advertiser", arr=None):
    return {"curve_status": status, "points": pts, "ad_account_scope": scope,
            "impression_reach": _ir(pts) if arr is None else arr,
            "country": "KR", "flight_days": 30}


CPM = 33_620.0
BUD = 2_000_000_000
fails = []


def ck(name, cond, got=""):
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}  {got}")
        fails.append(name)


print("=== 정상 경로 ===")
d, why = reach.points_from_cache([_row()], CPM, BUD, points=10)
ck("ok 행이면 점이 나온다", d is not None and len(d["points"]) == 10, why or "")
ck("도달이 단조증가", d and all(d["points"][i]["reach"] <= d["points"][i + 1]["reach"]
                            for i in range(len(d["points"]) - 1)))
ck("예산 축 상한을 넘지 않는다", d and d["points"][-1]["cost"] <= BUD)
ck("빈도가 계산된다", d and d["points"][-1]["frequency"] is not None)
ck("advertiser 범위면 경고 없음", d and not any("공용 계정" in w for w in d["warnings"]))

print("\n=== 쓸 수 없는 상태 네 가지 — 점을 만들지 않고 이유를 준다 ===")
for st, kw in (("suppressed", "억제"), ("not_permitted", "쓸 수 없는"),
               ("fetch_failed", "실패"), ("not_requested", "호출하지 않았")):
    d2, why2 = reach.points_from_cache([_row(status=st)], CPM, BUD)
    ck(f"{st} → 점 없음", d2 is None)
    ck(f"{st} → 이유에 '{kw}' 포함", bool(why2) and kw in why2, why2 or "")

print("\n=== 경고가 붙는 경우 ===")
d3, _ = reach.points_from_cache([_row(scope="agency_shared")], CPM, BUD, points=5)
ck("공용 계정이면 경고", d3 and any("공용 계정" in w for w in d3["warnings"]))
d4, _ = reach.points_from_cache([_row(pts=40)], CPM, BUD, points=5)
ck("점이 100개가 아니면 경고", d4 and any("40개" in w for w in d4["warnings"]))
d5, _ = reach.points_from_cache([_row(scope="unknown")], CPM, BUD, points=5)
ck("범위 미기록이면 경고", d5 and any("기록되지 않" in w for w in d5["warnings"]))

print("\n=== 외삽 — 캐시 끝을 넘는 예산은 늘려 그리지 않는다 ===")
small = [{"impression": 1_000_000, "reach": 500_000},
         {"impression": 2_000_000, "reach": 900_000}]
d6, _ = reach.points_from_cache([_row(pts=2, arr=small)], CPM, BUD, points=5)
cap = 2_000_000 * CPM / 1000.0
ck("캐시 끝에서 멈춘다", d6 and d6["points"][-1]["cost"] <= round(cap) + 1,
   f"last={d6['points'][-1]['cost'] if d6 else '-'} cap={round(cap)}")
ck("잘렸다고 경고", d6 and any("넘어" in w for w in d6["warnings"]))

print("\n=== 깨진 입력 — 조용히 0 을 만들지 않는다 ===")
for nm, rows in (("행 없음", []),
                 ("배열 없음", [_row(arr=[])]),
                 ("구조 다름", [_row(arr=[{"imp": 1, "rch": 2}])]),
                 ("스칼라", [_row(arr="not-a-list")])):
    d7, why7 = reach.points_from_cache(rows, CPM, BUD)
    ck(f"{nm} → 점 없음 + 이유", d7 is None and bool(why7), why7 or "")

d8, why8 = reach.points_from_cache([_row()], 0, BUD)
ck("CPM 0 이면 거부", d8 is None and bool(why8), why8 or "")

print()
print("=== 모집단 밴드 — 분모가 범위라는 것을 숨기지 않는다 ===")
# 벤더가 준 모집단(메타 delivery_estimate 실측 모양: ES 25~54 → 20.9M~24.6M)
rv = _row(); rv.update({"population_lower": 20_900_000, "population_upper": 24_600_000,
                        "population_source": "vendor_api"})
d9, _ = reach.points_from_cache([rv], CPM, BUD, points=5)
ck("벤더 모집단이면 밴드가 나온다",
   d9 and all("reach_pct_lower" in p and "reach_pct_upper" in p for p in d9["points"]))
ck("상한 모집단이 하한 도달률을 만든다",
   d9 and d9["points"][-1]["reach_pct_lower"] < d9["points"][-1]["reach_pct_upper"])
ck("population.is_vendor 가 참", d9 and d9["population"]["is_vendor"] is True)
ck("범위라고 경고", d9 and any("범위입니다" in w for w in d9["warnings"]))

# 가정 모집단 — 같은 칸에 섞이면 신뢰도가 같아 보인다
ra = _row(); ra.update({"population_lower": 30_000_000, "population_upper": 30_000_000,
                        "population_source": "assumed"})
d10, _ = reach.points_from_cache([ra], CPM, BUD, points=5)
ck("가정 모집단은 is_vendor 가 거짓", d10 and d10["population"]["is_vendor"] is False)
ck("가정이라고 경고", d10 and any("«가정»" in w for w in d10["warnings"]))

# 모집단이 없으면 % 를 만들지 않는다
d11, _ = reach.points_from_cache([_row()], CPM, BUD, points=5)
ck("모집단 없으면 밴드 없음",
   d11 and d11["population"] is None and "reach_pct_lower" not in d11["points"][0])

# 상·하한이 뒤집혀 들어와도 버티는가
rb = _row(); rb.update({"population_lower": 24_600_000, "population_upper": 20_900_000,
                        "population_source": "vendor_api"})
d12, _ = reach.points_from_cache([rb], CPM, BUD, points=3)
ck("상하한 뒤집힘을 바로잡는다",
   d12 and d12["population"]["lower"] < d12["population"]["upper"])

print()
if fails:
    print(f"FAILED {len(fails)}건: {', '.join(fails)}")
    raise SystemExit(1)
print("전부 통과")
