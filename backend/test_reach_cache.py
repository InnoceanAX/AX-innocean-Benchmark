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
# 🔴 전에 여기서 «대행사 공용 계정이면 분모가 다른 집단» 경고를 검증했다. 그 경고를
#   지웠으므로 이 테스트도 바뀐다 — DB 실측(E1)에서 서로 다른 세 광고주로 호출한
#   86개 점이 «전부» 동일했다. 계정은 v1 모델의 입력이 아니다.
#   틀린 경고를 지킨 테스트는 틀린 코드를 지킨다.
d3, _ = reach.points_from_cache([_row(scope="agency_shared")], CPM, BUD, points=5)
ck("공용 계정이라고 겁주지 않는다", d3 and not any("분모가 다른" in w for w in d3["warnings"]))
ck("계정과 무관하다고 적는다", d3 and any("광고 계정과 무관" in w for w in d3["warnings"]),
   (d3 or {}).get("warnings"))
# 🔴 «100점» 규칙을 버렸다 — DB 첫 실호출이 86점이었고 간격도 로그였다(2026-10-22).
#   개수로 판정하면 멀쩡한 곡선마다 경고가 떠서 그 경고가 곧 무시된다.
d4, _ = reach.points_from_cache([_row(pts=86)], CPM, BUD, points=5)
ck("86점(실제 응답)도 경고 없이 통과", d4 and not any("점" in w and "뿐" in w
                                                  for w in d4["warnings"]))
d4b, why4b = reach.points_from_cache([_row(pts=3)], CPM, BUD, points=5)
ck("점이 3개면 경고하거나 거부", (d4b is None) or any("뿐" in w for w in d4b["warnings"]),
   why4b or "")
d5, _ = reach.points_from_cache([_row(scope="unknown")], CPM, BUD, points=5)
# 🔴 전에는 «어느 계정 기준인지 기록되지 않았다» 고 경고했다. v1 에서는 계정이
#   모델 입력이 아니므로(DB 실측 E1) 기록 여부가 곡선을 바꾸지 않는다 — 무의미한
#   경고였다. 대신 계정과 무관하다는 «측정된» 사실을 적는다.
ck("범위 미기록을 겁주지 않는다", d5 and not any("기록되지 않" in w for w in d5["warnings"]))
ck("미기록도 계정 무관 안내", d5 and any("광고 계정과 무관" in w for w in d5["warnings"]))

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
print("=== 쓸 수 있는가 — 단조증가 ===")
bad = [{"impression": 1_000_000, "reach": 500_000},
       {"impression": 2_000_000, "reach": 400_000},   # 노출↑ 인데 도달↓
       {"impression": 3_000_000, "reach": 900_000}]
d13, why13 = reach.points_from_cache([_row(pts=3, arr=bad)], CPM, BUD, points=5)
ck("도달이 줄면 거부", d13 is None and bool(why13) and "줄어드는" in (why13 or ""), why13 or "")

# 오목성 위반은 «걸지 않는다» — 양자화 한 단계짜리가 섞여 있다(DB 실측 7건)
import math as _m
qz = []
for i in range(1, 40):
    imp = i * 1000
    qz.append({"impression": imp, "reach": int(imp ** 0.89)})   # 정수 양자화
d14, _ = reach.points_from_cache([_row(pts=len(qz), arr=qz)], CPM, BUD, points=6)
ck("양자화된 실제 모양(b=0.89)은 통과", d14 is not None and len(d14["points"]) == 6)


# ═══════════════ 셀 합성 · 기간 보간 (DB 실측 2026-10-22) ═══════════════
# ★ 판정 기준은 하나다 — DB 가 실제로 부른 숫자를 제 더하기가 재현하는가.
#   재현하지 못하면 제 산수가 틀린 것이고, 그건 화면에 틀린 도달로 나간다.

def _pw(end_imp, end_reach, b, n=86):
    """끝점이 (end_imp, end_reach) 인 멱함수 곡선. 로그 간격 — 실제 응답 모양."""
    import math
    a = end_reach / (end_imp ** b)
    out = []
    for i in range(n):
        t = i / (n - 1.0)
        imp = 1000.0 * math.exp(t * math.log(end_imp / 1000.0))
        out.append((imp, a * imp ** b))
    return out


# DB 실측 E2 — 이 두 칸을 더한 값이 합본 호출과 0.033% 차였다
A_25_34_M = _pw(2_200_000, 612_282, 0.828)
B_35_49_M = _pw(4_700_000, 1_308_839, 0.847)

print("\n=== 합성 산수 — DB 실측 재현 ===")
pairs = {"age=AGE_25_34|gender=MALE": A_25_34_M,
         "age=AGE_35_49|gender=MALE": B_35_49_M}
_real_ok_pairs = reach._ok_pairs


def _fake_ok_pairs(market, fd, key):
    if key in pairs:
        return (pairs[key], {"targeting_label": key, "ad_account_scope": "advertiser"}), None
    return None, "격자에 없습니다"


reach._ok_pairs = _fake_ok_pairs
reach._cpm = lambda *a, **k: (33_620.0, "test")

cells2 = [{"age": "25-34", "gender": "Male"}, {"age": "35-49", "gender": "Male"}]
sy = reach.synth_curve(cells2, media="넷플릭스", budget=10 ** 15, points=12)
ck("배타적 2칸은 합성된다", sy.get("found"), sy.get("note"))
EXP = 612_282 + 1_308_839          # = 1,921,121 (DB 가 계산한 합성값)
ck(f"끝점이 두 칸의 합({EXP:,})과 일치",
   sy.get("found") and abs(sy["max_reach"] - EXP) <= 2, sy.get("max_reach"))
# DB 가 부른 «합본» 실측은 1,920,479 — 합성은 그보다 위(=보수적이 아니라 과대)였다.
#   방향을 코드가 아니라 테스트에 적어 둔다. 뒤집히면 여기서 걸린다.
ck("실측 합본(1,920,479) 대비 0.2% 이내",
   sy.get("found") and abs(sy["max_reach"] - 1_920_479) / 1_920_479 * 100 <= 0.2,
   sy.get("max_reach"))
ck("합성이라고 밝힌다", sy.get("basis") == "synthesis")
ck("합성 경고가 붙는다", sy.get("found") and any("«합성»" in w for w in sy["warnings"]))
# 🔴 방향을 테스트로 못 박는다. DB 보고서는 이 차이를 「보수적」 이라고 적었는데
#   측정값은 합본 1,920,479 < 합성 1,921,121 이다 — 합성이 «높다». 보수적의 반대다.
#   그리고 높은 쪽이 위험하다: 도달을 넉넉히 보여 주면 예산을 적게 잡는다.
#   문구만 고치면 다음에 또 뒤집히므로 여기서 걸리게 한다.
ck("합성이 실측 합본보다 «높다» (방향)",
   sy.get("found") and sy["max_reach"] > 1_920_479, sy.get("max_reach"))
ck("화면 문구가 «높다» 고 적는다",
   sy.get("found") and any("높은" in w for w in sy["warnings"]),
   [w[:70] for w in (sy.get("warnings") or [])][:1])
ck("«낮게 나온다» 고 적지 않는다",
   sy.get("found") and not any("낮게" in w for w in sy["warnings"]))
ck("정의역 경고가 붙는다",
   sy.get("found") and any("단순합" in w for w in sy["warnings"]))

print("\n=== 합성을 «거절» 해야 하는 경우 ===")
bad_sets = [
    ("«전체» 가 섞임", [{"age": "", "gender": ""}, {"age": "25-34", "gender": "Male"}], "품"),
    ("축이 다름(겹친다)", [{"age": "25-34"}, {"gender": "Male"}], "겹치"),
    ("같은 칸 두 번", [{"age": "25-34", "gender": "Male"},
                    {"age": "25-34", "gender": "Male"}], "두 번"),
    ("한 칸뿐", [{"age": "25-34", "gender": "Male"}], "두 칸 이상"),
]
for nm, cs, kw in bad_sets:
    r = reach.synth_curve(cs, media="넷플릭스")
    ck(f"{nm} → 거절", not r.get("found") and r.get("state") == "not_synthesizable",
       r.get("state"))
    ck(f"{nm} → 이유에 '{kw}'", kw in (r.get("note") or ""), r.get("note"))

print("\n=== 빠진 칸이 있으면 «빼고 더하지» 않는다 ===")
r_inc = reach.synth_curve([{"age": "25-34", "gender": "Male"},
                           {"age": "65+", "gender": "Female"}], media="넷플릭스")
ck("한 칸이 없으면 합성 안 함", not r_inc.get("found") and r_inc.get("state") == "incomplete",
   r_inc.get("state"))
ck("없는 칸을 알려 준다", len(r_inc.get("missing") or []) == 1, r_inc.get("missing"))

print("\n=== 🔴 데모 전부 고르기 ≠ «타게팅 없음» (실측 89.6%) ===")
full_pairs = {}
for an, ae in reach.V1_AGE_ENUM.items():
    for gn, ge in reach.V1_GENDER_ENUM.items():
        full_pairs[f"age={ae}|gender={ge}"] = _pw(3_400_000, 484_043, 0.84)
pairs = full_pairs
full_cells = [{"age": a, "gender": g} for a in reach.V1_AGE_ENUM
              for g in reach.V1_GENDER_ENUM]
ck("_is_full_cover: 12칸이면 True", reach._is_full_cover(full_cells))
ck("_is_full_cover: 11칸이면 False", not reach._is_full_cover(full_cells[:-1]))
ck("_is_full_cover: 연령 6개만도 True",
   reach._is_full_cover([{"age": a} for a in reach.V1_AGE_ENUM]))
ck("_is_full_cover: 연령 5개는 False",
   not reach._is_full_cover([{"age": a} for a in list(reach.V1_AGE_ENUM)[:5]]))
sf = reach.synth_curve(full_cells, media="넷플릭스", budget=10 ** 15, points=6)
ck("12칸 합성은 된다", sf.get("found"), sf.get("note"))
ck("full_cover 플래그가 선다", sf.get("full_cover") is True)
ck("«타게팅 없음이 아니다» 를 «맨 앞에» 적는다",
   sf.get("found") and "타게팅 없음" in (sf["warnings"][0] or ""),
   (sf.get("warnings") or [""])[0][:60])
ck("89.6% 를 숫자로 적는다",
   sf.get("found") and any("89.6" in w for w in sf["warnings"]))
ck("«왜 그런지는 모른다» 를 적는다",
   sf.get("found") and any("모릅니다" in w for w in sf["warnings"]))
# 🔴 0.2% 경고만 달고 끝내면 10.4% 어긋남을 0.2% 로 읽는다 — 50배다.
ck("0.2% 경고가 전부가 아니다",
   sf.get("found") and sf["warnings"][0].find("0.2%") < 0,
   (sf.get("warnings") or [""])[0][:40])

print("\n=== 기간 보간 — 실측 앵커 안에서만 ===")
dur = {30: _pw(47_047_000, 6_482_607, 0.8903),
       60: _pw(47_047_000, 7_559_130, 0.8991),
       90: _pw(47_047_000, 8_253_925, 0.9010)}


def _dur_ok_pairs(market, fd, key):
    if fd in dur:
        return (dur[fd], {"targeting_label": "전체", "ad_account_scope": "advertiser"}), None
    return None, "격자에 없습니다"


reach._ok_pairs = _dur_ok_pairs
reach._curve_rows = lambda *a, **k: []          # 정확히 일치하는 행은 없다 → 보간 경로
i45 = reach._interp_duration("KR", 45, "none", "넷플릭스", 10 ** 15, 8)
ck("45일은 30·60 사이로 보간된다", i45 and i45.get("found"), (i45 or {}).get("note"))
ck("앵커를 밝힌다", i45 and i45.get("anchor_days") == [30, 60])
ck("보간이라고 밝힌다", i45 and i45.get("basis") == "duration_interp")
MID = (6_482_607 + 7_559_130) / 2
ck(f"45일 끝점이 두 앵커의 중간({MID:,.0f})",
   i45 and abs(i45["max_reach"] - MID) <= 2, (i45 or {}).get("max_reach"))
ck("보간 경고가 붙는다", i45 and any("보간한 값" in w for w in i45["warnings"]))
# ⚠️ 앵커 밖으로 늘리지 않는다 — 90일 밖은 재지 않았고 체감하므로 과대해진다
ck("120일은 보간하지 않는다(None)", reach._interp_duration(
    "KR", 120, "none", "넷플릭스", 10 ** 15, 8) is None)
ck("20일은 보간하지 않는다(None)", reach._interp_duration(
    "KR", 20, "none", "넷플릭스", 10 ** 15, 8) is None)
ck("30일(앵커 자체)은 보간 경로가 아니다", reach._interp_duration(
    "KR", 30, "none", "넷플릭스", 10 ** 15, 8) is None)

print("\n=== 보간 헬퍼 ===")
pp = [(1000.0, 100.0), (2000.0, 180.0), (4000.0, 300.0)]
ck("구간 안은 선형", abs(reach._interp_reach(pp, 1500.0) - 140.0) < 1e-6)
ck("왼쪽 밖은 첫 값", reach._interp_reach(pp, 10.0) == 100.0)
ck("오른쪽 밖은 끝 값(외삽 안 함)", reach._interp_reach(pp, 10 ** 9) == 300.0)
ck("빈 곡선은 0", reach._interp_reach([], 100.0) == 0.0)

reach._ok_pairs = _real_ok_pairs

print()
if fails:
    print(f"FAILED {len(fails)}건: {', '.join(fails)}")
    raise SystemExit(1)
print("전부 통과")
