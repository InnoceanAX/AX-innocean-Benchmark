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


# 🔴 기본 scope 는 «unknown» 이다 — v1 격자 22/22 가 그 값이다(DB 실측).
#   전에 "advertiser" 였는데 그건 프로덕션에 없는 값이고, 기본값이 현실과
#   다르면 모든 테스트가 «없는 모양» 위에서 돈다.
# 🔴 기본 pts 는 82 다 — 실제 응답은 74~86 이다(DB 실측). 전에 100 이었는데
#   그건 스펙 문서의 값이고 현실에 없다. 「점 수와 무관하다」 를 테스트하면서
#   «모든» 테스트가 없는 곡선 길이 위에서 돌고 있었다 — scope 와 같은 자리다.
def _row(status="ok", pts=82, scope="unknown", arr=None):
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
ck("기본 행에 «공용 계정» 류 경고가 없다",
   d and not any("공용 계정" in w for w in d["warnings"]))
ck("기본 행에 «예상 밖 범위» 경고가 없다(실제 값이므로)",
   d and not any("범위가" in w for w in d["warnings"]),
   [w[:50] for w in (d or {}).get("warnings", [])])

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
# 🔴 전에 여기서 scope="agency_shared" 를 넣고 댔다. 그 값은 «프로덕션에 없다» —
#   v1 격자 22/22 가 unknown 이다(DB 실측). 없는 축에 대한 신뢰를 만드는 테스트였다.
#   실제로 나오는 값으로 대고, «밖의 값이 오면 알리는가» 를 따로 댄다.
d3, _ = reach.points_from_cache([_row(scope="unknown")], CPM, BUD, points=5)
ck("v1 실제 값(unknown)에서 계정 무관 안내",
   d3 and any("광고 계정과 무관" in w for w in d3["warnings"]), (d3 or {}).get("warnings"))
ck("공용 계정이라고 겁주지 않는다", d3 and not any("분모가 다른" in w for w in d3["warnings"]))
ck("unknown 은 «예상 밖» 경고를 띄우지 않는다",
   d3 and not any("범위가" in w for w in d3["warnings"]))
ck("이 값으로 분기하지 말라고 표시한다", d3 and d3.get("scope_informative") is False)
# ⚠️ v2 로 넘어가거나 격자가 바뀌면 다른 값이 올 수 있다. 조용히 같은 안내를 붙이면
#   전제가 깨진 것을 모른다 — 그 사실만 알리는지 댄다.
d3b, _ = reach.points_from_cache([_row(scope="agency_shared")], CPM, BUD, points=5)
ck("예상 밖 범위 값이 오면 알린다",
   d3b and any("v1 에서는" in w and "unknown" in w for w in d3b["warnings"]),
   [w[:60] for w in (d3b or {}).get("warnings", [])])
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
        return (pairs[key], {"targeting_label": key,
                             "ad_account_scope": "advertiser",
                             "curve_id": "cid-" + key[:14],
                             "flight_days": fd,
                             "fetched_at": "2026-10-22T10:00:00"}), None
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
# 🔴 여기에 «방향» 을 걸었다가 두 번 틀렸다. 세 번째로 고친다 — 조건을 건다.
#   1,921,121(합성) 과 1,920,479(합본) 는 노출이 3.57% 다른 두 점이다. 부호를
#   붙일 수 없는 비교였는데 양쪽이 각자 부호를 붙였다(DB 는 「보수적」, 나는 「과대」).
#   같은 노출에서 재면 합성이 0.045~0.059% «낮다»(DB 전수탐색).
#
#   ⚠️ 그래서 방향 단어로 걸면 안 된다 — 나중에 노출 고정 보간을 넣는 순간
#     부호가 뒤집혀 «멀쩡한 코드» 가 깨진다. 거는 것은 «노출 수준이 함께 적히는가» 다.
#     두 계산 중 어느 것을 쓰든 이 조건은 그대로 성립한다.
# ⚠️ DB 실제 격자의 단순합은 13,657,000 이지만 칸별 끝점 분할은 모른다.
#   합만 맞는 숫자를 지어내 넣으면 재지 않은 것을 테스트에 심는 셈이라,
#   «픽스처 자신의 합» 으로 건다. 검증하려는 성질은 그 쪽이다.
EXP_IMP = 2_200_000 + 4_700_000
ck("끝 도달에 해당하는 «노출 수준» 을 돌려준다",
   sy.get("found") and sy.get("max_imp_sum") == EXP_IMP, sy.get("max_imp_sum"))
ck("문구가 «각 칸을 끝까지 사는 경우» 라고 조건을 적는다",
   sy.get("found") and any("끝까지 사는" in w for w in sy["warnings"]),
   [w[:70] for w in (sy.get("warnings") or [])][:1])
ck("문구가 노출 수준을 숫자로 적는다",
   sy.get("found") and any(f"{EXP_IMP:,}" in w for w in sy["warnings"]),
   [w[:90] for w in (sy.get("warnings") or [])][:1])
# 방향을 단정하는 단어가 첫 문구에 들어오면 걸리게 한다 — 두 번 당한 자리다
ck("방향을 단정하지 않는다(«높은» 단독 주장 금지)",
   sy.get("found") and "조금 높은" not in sy["warnings"][0],
   sy.get("warnings", [""])[0][:60])
ck("정의역 경고가 붙는다",
   sy.get("found") and any("단순합" in w for w in sy["warnings"]))

print(chr(10) + "=== 🔴 격자에 벤더 실측 합본이 있으면 합성하지 않는다 ===")
# 🔴 이것이 실제 결함이었다. 격자를 읽어 보니 다중값 행이 있다:
#     age=AGE_25_34,AGE_35_49|gender=MALE   도달 1,920,479  노출끝 13,170,000
#   내 targeting_key() 는 단일값만 만들어서 이 행을 «절대» 못 찾았고, 그래서
#   두 칸을 고른 플래너에게 벤더 실측(1,920,479)이 격자에 있는데도 내 합성
#   추정치(1,921,121)를 내보내고 있었다. 추정이 실측을 가리는 쪽이다.
ck("combined_key 가 격자 모양과 같다",
   reach.combined_key(cells2) == "age=AGE_25_34,AGE_35_49|gender=MALE",
   reach.combined_key(cells2))
ck("combined_key: 값이 정렬된다",
   reach.combined_key([{"age": "35-49", "gender": "Male"},
                       {"age": "25-34", "gender": "Male"}])
   == "age=AGE_25_34,AGE_35_49|gender=MALE")
ck("combined_key: 빈 선택은 none", reach.combined_key([]) == "none")

COMBINED = _pw(13_170_000, 1_920_479, 0.86)
pairs["age=AGE_25_34,AGE_35_49|gender=MALE"] = COMBINED
vc = reach.synth_curve(cells2, media="넷플릭스", budget=10 ** 15, points=10)
ck("실측 합본이 있으면 basis=vendor_combined", vc.get("basis") == "vendor_combined",
   vc.get("basis"))
ck("실측값(1,920,479)을 쓴다 — 합성값이 아니다",
   abs((vc.get("max_reach") or 0) - 1_920_479) <= 2, vc.get("max_reach"))
ck("정의역도 실측(13,170,000) 이다", vc.get("max_imp") == 13_170_000, vc.get("max_imp"))
# ⚠️ «단순합» 이라는 뜻의 이름을 실측 정의역에 붙이지 않는다 — 이름이 뜻을 바꾼다
ck("max_imp_sum 을 쓰지 않는다", vc.get("max_imp_sum") is None)
ck("«벤더에 그대로 물었다» 고 적는다",
   any("그대로 물어서" in w for w in (vc.get("warnings") or [])))
ck("합성 경고를 붙이지 않는다",
   not any("«합성»" in w for w in (vc.get("warnings") or [])))
ck("영수증을 올린다", len(vc.get("receipts") or []) == 1, vc.get("receipts"))

del pairs["age=AGE_25_34,AGE_35_49|gender=MALE"]
fb = reach.synth_curve(cells2, media="넷플릭스", budget=10 ** 15, points=10)
ck("합본이 없으면 합성으로 돌아간다", fb.get("basis") == "synthesis", fb.get("basis"))
ck("합성 때는 영수증이 칸마다", len(fb.get("receipts") or []) == 2, fb.get("receipts"))
ck("시도한 합본 키를 알려 준다",
   fb.get("combined_key_tried") == "age=AGE_25_34,AGE_35_49|gender=MALE",
   fb.get("combined_key_tried"))

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
# 🔴 「원인」 을 단정하지 않는가. 전에는 「어떤 칸에도 안 드는 프로필이
#   그만큼 있다」 고 적었는데 그건 관측된 차이에 원인을 붙인 추론이었다.
#   특히 가설 ③(12칸 비가산)이면 «간극 자체가 없다» — 닿지 못하는 프로필이 없다.
#   가설 둘만 제시하면 받는 쪽에 «없는 선택» 을 강요한다.
ck("원인을 측정하지 않았다고 적는다",
   sf.get("found") and any("측정하지 않았습니다" in w for w in sf["warnings"]))
ck("가설을 셋 다 적는다",
   sf.get("found") and all(h in " ".join(sf["warnings"])
                           for h in ("정보가 없는", "전체를 덮지", "합산되도록")),
   [w[:60] for w in (sf.get("warnings") or [])][1:2])
ck("«닿을 수 없다» 고 단정하지 않는다",
   sf.get("found") and not any("닿을 수 없" in w for w in sf["warnings"]))
ck("가산성을 확인한 칸 수를 적는다",
   sf.get("found") and any("2칸" in w for w in sf["warnings"]))
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
# 🔴 앵커를 고정한다. 안 하면 _grid_anchors 가 «살아 있는 격자» 를 읽어서,
#   DB 가 45일을 넣는 순간 이 테스트가 깨진다 — 내 코드는 그대로인데.
#   배포 게이트가 남의 데이터에 의존하면 남이 격자를 채우는 것만으로
#   내 배포가 막히고, 그때 깨진 이유가 내 코드처럼 보인다.
reach._grid_anchors = lambda mk, k: [30, 60, 90]
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


print(chr(10) + "=== 🔴 fetch_failed 는 두 가지 답을 뜻한다 (재시도 남음 / 멈춤) ===")
# 🔴 DB 가 재시도 캡(4회)과 attempts 열을 넣었다. 같은 상태값인데 답이 다르다 —
#   재시도가 남았으면 「기다리면 생길 수 있다」, 캡에 걸렸으면 「기다려도 안 생긴다」.
#   가르지 않으면 플래너를 영원히 기다리게 한다.
# ⚠️ NULL 은 «아직 안 멈췄다» 다. 옛 행 전부가 NULL 이라(실측 41행) NULL 을 캡으로
#   보면 멀쩡한 대기 칸이 「안 생긴다」 로 나간다.
for att, want, why in ((None, "fetch_failed", "NULL(옛 행)은 기다릴 수 있다"),
                       (1, "fetch_failed", "1회면 재시도가 남았다"),
                       (3, "fetch_failed", "3회는 캡(4) 미만이다"),
                       (4, "fetch_failed_capped", "4회는 캡 도달이다"),
                       (9, "fetch_failed_capped", "캡을 넘은 것도 멈춤이다")):
    got, n = reach.fetch_failed_state(att)
    ck(f"attempts={att} → {want} ({why})", got == want, got)
ck("캡 도달은 «기다려도 안 생긴다» 고 적는다",
   "기다려도 생기지 않습니다"
   in reach.CURVE_STATUS["fetch_failed_capped"]["note"])
ck("재시도 남은 쪽은 «생길 수 있다» 고 적는다",
   "생길 수 있습니다" in reach.CURVE_STATUS["fetch_failed"]["note"])
ck("두 문구가 다르다",
   reach.CURVE_STATUS["fetch_failed"]["note"]
   != reach.CURVE_STATUS["fetch_failed_capped"]["note"])
# 🔴 스펙 에러표에 422 가 없다(200·400·401·404·429·500 뿐). 422 는 v2 검토에서 나온
#   미회신 질문이고 v1 이 억제를 어떻게 알리는지 모른다 — 화면에 코드를 박으면
#   보는 사람은 확인된 사실로 읽는다.
ck("suppressed 문구에 «422» 를 적지 않는다",
   "422" not in reach.CURVE_STATUS["suppressed"]["note"],
   reach.CURVE_STATUS["suppressed"]["note"][:60])
ck("어느 상태 문구에도 안 쟀거나 없는 코드를 박지 않는다",
   not any("422" in (v.get("note") or "") for v in reach.CURVE_STATUS.values()))

print(chr(10) + "=== 🔴 내려 둔 행(is_active=FALSE)을 쓰지 않는다 ===")
# 🔴 실측(2026-10-22): 뷰는 is_active=FALSE 행을 걸러 주지 «않는다».
#     is_active=False 행 2(ok 2) · True 41(ok 20)
#   그리고 KR·30일·none 을 걸러 보면 3행이 나오는데 하필 «비활성 실험 칸 2개» 가
#   먼저 왔다 — ok 안에 정렬 기준이 없어 순서가 임의였다. ok[0] 을 쓰므로 나는
#   DB 가 내려 둔 칸의 곡선과 영수증을 쓰고 있었다.
#   이 키는 곡선이 같아서(6,482,607) 숫자가 안 바뀌어 «안 보였다». 하지만 DB 가
#   «틀려서» 내린 행이면 그대로 쓴다 — 그러면 is_active 가 아무 일도 하지 않는다.
_sql = reach._rows_sql(5)
ck("조회 SQL 이 is_active 를 거른다", "is_active" in _sql, _sql[:140])

# ── 조회를 «문자열 키» 하나에 묶어 두지 않는다 ──────────────────────
# 🔴 내 조회는 DB 의 targeting_key 형식에 묶여 있었다. 그 계약이 하루에 두 번
#   바뀌었다. 형식이 바뀌면 내 조회는 「없다」 를 돌려주고 화면은 「계산 대기」
#   라고 적는다 — 곡선이 있는데 아직이라고 말하고, DB 쪽 문제로 보인다.
ck("내 키 → 값 집합 변환(단일)",
   reach._key_values("age=AGE_25_34|gender=MALE") == "AGE_25_34,MALE")
ck("내 키 → 값 집합 변환(다중)",
   reach._key_values("age=AGE_25_34,AGE_35_49|gender=MALE")
   == "AGE_25_34,AGE_35_49,MALE")
ck("값 집합은 정렬된다(고른 순서 무관)",
   reach._key_values("gender=MALE|age=AGE_35_49,AGE_25_34")
   == "AGE_25_34,AGE_35_49,MALE")
ck("none 은 빈 값", reach._key_values("none") == "")

# 🔴 빈 배열의 STRING_AGG 는 '' 이 아니라 NULL 이다(실측). IFNULL 이 없으면
#   조건이 영원히 거짓이 되고, 하필 「타게팅 없음」 행이 89.6% 의 분모다.
ck("값 SQL 에 IFNULL 이 있다(빈 배열은 NULL 이다)",
   "IFNULL" in reach.VALUES_SQL, reach.VALUES_SQL[:60])
# ⚠️ 값 조건은 «값이 있을 때만» 켠다. 빈 문자열로 켜면 value_ids 가 안 채워진
#   타게팅 행까지 「타게팅 없음」 으로 잡혀, 타게팅된 곡선을 전체 도달로 쓴다.
ck("값이 없으면 값 조건을 켜지 않는다", "@tv" not in reach._rows_sql(5, False))
ck("값이 있으면 키 «또는» 값으로 맞춘다",
   "@tv" in reach._rows_sql(5, True) and "OR" in reach._rows_sql(5, True))
ck("값 조건이 targeting_value_ids 를 쓴다",
   "targeting_value_ids" in reach._rows_sql(5, True))
# ⚠️ IFNULL(is_active, TRUE) 여야 한다. NULL 을 비활성으로 보면 컬럼이 안 채워진
#   행이 전부 사라져 화면이 조용히 빈다 — 「없다」와 「내렸다」는 다르다.
ck("NULL 은 활성으로 본다(IFNULL ... TRUE)",
   "IFNULL(is_active, TRUE)" in _sql, _sql[:140])
# ok 끼리 순서가 임의면 영수증이 매번 달라질 수 있다 — 그러면 되물을 수 없다
ck("ok 끼리 순서를 고정한다(fetched_at)", "fetched_at DESC" in _sql, _sql[:200])
ck("앵커 조회도 활성만 본다", "IFNULL(is_active, TRUE)" in reach.ACTIVE_SQL
   or reach.ACTIVE_SQL == "IFNULL(is_active, TRUE)", reach.ACTIVE_SQL)

print(chr(10) + "=== 🔴 앵커를 격자에서 읽는다 (조합마다 다르다) ===")
# 🔴 DB 45일 실측이 두 전제를 깼다.
#   ① 45일이 들어오면서 앵커가 넷이 됐다 — 하드코딩한 (30,60,90) 은 이미 틀렸다.
#   ② 더 중요한 것: 45일은 «전체 조건에만» 있고 데모 칸에는 없다. 상수로 묶으면
#     데모 칸의 45일 요청이 (30,45) 로 묶여 「45일이 없다」 고 포기하는데,
#     (30,60) 이면 답할 수 있다. 그게 퇴행이다 — 여기서 걸리게 한다.
dur[45] = _pw(46_783_000, 7_080_839, 0.8975)    # DB 실측 45일

reach._grid_anchors = lambda mk, k: [30, 45, 60, 90]
i50 = reach._interp_duration("KR", 50, "none", "넷플릭스", 10 ** 15, 8)
ck("앵커가 넷이면 50일은 (45,60) 으로 묶인다",
   i50 and i50.get("anchor_days") == [45, 60], (i50 or {}).get("anchor_days"))

# 데모 칸처럼 45일이 «없는» 조합
reach._grid_anchors = lambda mk, k: [30, 60]
i45b = reach._interp_duration("KR", 45, "none", "넷플릭스", 10 ** 15, 8)
ck("45일이 없는 조합은 (30,60) 으로 묶인다 — 포기하지 않는다",
   i45b and i45b.get("anchor_days") == [30, 60], (i45b or {}).get("anchor_days"))
ck("쓸 수 있는 앵커 목록을 올린다", i45b and i45b.get("grid_anchors") == [30, 60])

print(chr(10) + "=== 🔴 노출 상한은 «유도값» 이다 — 정의역이 일수에 단조가 아니다 ===")
# 실측: 30일 47,047,000 · 45일 46,783,000 · 60일 49,153,000
#   45일이 30일보다 «좁다». cap = min(30,60) = 47,047,000 으로 그리면 45일의
#   실제 정의역(46,783,000)보다 264,000 노출만큼 더 멀리 답한다.
#   얼마나 깎을지는 또 추측이므로 깎지 않고 «유도값» 이라고 적는다.
ck("유도값이라고 데이터로 표시한다", i45b and i45b.get("max_imp_is_measured") is False)
ck("유도한 상한을 숫자로 올린다", i45b and i45b.get("max_imp_derived") == 47_047_000,
   (i45b or {}).get("max_imp_derived"))
ck("문구에 «유도» 가 들어간다",
   i45b and any("유도" in w for w in i45b["warnings"]),
   [w[:60] for w in (i45b or {}).get("warnings", [])])
ck("«단조롭지 않다» 를 적는다",
   i45b and any("단조롭지" in w for w in i45b["warnings"]))

print(chr(10) + "=== 보간 오차는 상수가 아니라 기울기다 ===")
# 🔴 실측 -0.067%(작은 노출) ~ -0.950%(정의역 끝). 「1% 이내」로 뭉개면
#   그 기울기를 놓치고, 플래너는 큰 예산에서 판단한다.
ck("오차가 «노출과 함께 커진다» 고 적는다",
   i45b and any("노출과 함께 커집니다" in w for w in i45b["warnings"]))
ck("두 끝의 오차를 숫자로 적는다",
   i45b and any("0.07" in w and "0.95" in w for w in i45b["warnings"]))
ck("«조금 낮게» 같은 뭉갠 말을 쓰지 않는다",
   i45b and not any("조금 낮게" in w for w in i45b["warnings"]))

reach._ok_pairs = _real_ok_pairs

print()
if fails:
    print(f"FAILED {len(fails)}건: {', '.join(fails)}")
    raise SystemExit(1)
print("전부 통과")
