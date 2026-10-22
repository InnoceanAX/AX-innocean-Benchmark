# -*- coding: utf-8 -*-
"""돌연변이 — 제 테스트가 «실제로» 불변식을 대고 있는지 재 본다.

DB 가 자기 테스트 38개를 만든 뒤 코드를 하나씩 틀어 봤고, 여섯은 잡혔는데 하나가
안 잡혔다. 그런데 «검사가 약한 게 아니라 그 줄이 중복» 이었다 — 외삽하게 바꾸니
바로 잡혔다.

🔑 그래서 두 가지를 같이 본다:
    잡힘     그 불변식을 테스트가 실제로 댄다
    안 잡힘  둘 중 하나다 — ① 테스트가 약하다 ② 돌연변이가 «동작을 안 바꿨다»(중복/죽은 줄)
             ②를 ①로 바로 읽으면 없는 결함을 고치러 간다

⚠️ 전부 사본에서 한다. 원본은 손대지 않는다.
"""
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SRC = Path(r"D:\Dropbox\정인혁\Study\innocean\업무\Adshub_2.0\ai\adshub\솔루션\BenchMark"
           r"\backend")
FILES = ("reach.py", "test_reach_cache.py", "check_invariants.py", "bq.py",
         "mart.py", "dplan.py", "status.py", "gaps.py")

# (불변식, 설명, 찾을 것, 바꿀 것) — «동작을 바꾸는» 돌연변이여야 한다
MUTATIONS = [
    ("no-point-count-gate", "점 수 게이트를 되살린다",
     "if npts is not None and npts < 4:",
     "if npts is not None and npts != 100:"),

    ("active-rows-only", "내려 둔 행을 다시 읽는다",
     'f"AND {ACTIVE_SQL} "\n            f"ORDER BY',
     'f"ORDER BY'),

    ("fetch-failed-two-answers", "캡 도달을 안 가른다",
     'if r.get("retry_exhausted") is True:\n        return "fetch_failed_capped", att',
     'if False:\n        return "fetch_failed_capped", att'),

    ("no-retry-cap-mirror", "캡 숫자를 다시 베껴 온다",
     'if r.get("retry_exhausted") is True:',
     'if (att or 0) >= 4:'),

    ("vendor-over-synth", "격자의 실측 합본을 건너뛴다",
     "    if cres is not None:",
     "    if False and cres is not None:"),

    ("no-extrapolation", "곡선 끝 밖으로 늘려 그린다",
     "    return pairs[-1][1]",
     "    x0, y0 = pairs[-2]\n    x1, y1 = pairs[-1]\n"
     "    return y1 + (y1 - y0) * ((imps - x1) / (x1 - x0)) if x1 > x0 else y1"),

    ("gap-cause-unmeasured", "가설 ③(비가산성)을 뺀다",
     '    "③ 12칸 수준에서 추정치가 합산되도록 설계되지 않았다 — 간극이 실재하지 않는다",\n',
     ""),

    ("scope-not-informative", "scope 로 분기해도 된다고 말한다",
     '"scope_informative": False,',
     '"scope_informative": True,'),

    ("population-per-row", "분모를 전역 상수로 쓴다",
     '    pop_lo = row.get("population_lower")\n    pop_hi = row.get("population_upper")',
     "    pop_lo = pop_hi = 6_482_607"),

    ("synth-exclusive-only", "겹치는 조합도 합성한다",
     '            return False, ("서로 겹치는 조건을 고르셨습니다',
     '            axes = ax\n            return True, ("서로 겹치는 조건을 고르셨습니다'),

    ("synth-no-partial", "없는 칸을 빼고 더한다",
     "    if missing:\n        return {\"found\": False, \"state\": \"incomplete\"",
     "    if False:\n        return {\"found\": False, \"state\": \"incomplete\""),

    ("interp-identity", "한쪽 앵커 기여에 계수를 곱해 항등을 깬다",
     "pts = [(x, _interp_reach(pa, x) * (1 - w) + _interp_reach(pb, x) * w)",
     "pts = [(x, _interp_reach(pa, x) * (1 - w) * 0.97 + _interp_reach(pb, x) * w)"),

    ("interp-rejects-non-monotone", "단조성 검사를 끈다",
     "    if drops:",
     "    if False:  # mutated"),

    ("no-unverified-codes", "«422» 를 다시 박는다",
     '"note": "모집단이 작아 매체가 출력을 억제한 조건입니다. "',
     '"note": "모집단이 작아 매체가 출력을 억제한 조건입니다(미공개, 422). "'),
]


def setup():
    tmp = Path(tempfile.mkdtemp(prefix="mut_"))
    for f in FILES:
        if (SRC / f).exists():
            shutil.copy2(SRC / f, tmp / f)
    return tmp


def run_tests(tmp):
    r = subprocess.run([sys.executable, "-X", "utf8", "test_reach_cache.py"],
                       cwd=tmp, capture_output=True, text=True, encoding="utf-8")
    out = (r.stdout or "") + (r.stderr or "")
    fails = [l.strip() for l in out.splitlines() if l.strip().startswith("FAIL")]
    return r.returncode, fails, out


# 🔴 「잡혔다」 가 「그 불변식을 쟀다」 를 뜻하지 않는다. 돌연변이가 «엉뚱한» 테스트를
#   깨뜨려도 종료코드는 1 이다 — DB 가 찾은 「안 잡힘의 세 번째 뜻」 의 거울이다.
#   실제로 interp-identity 가 그랬다: 가중치를 틀었더니 «다른» 보간 테스트가 걸렸고
#   항등 검사에 이가 있는지는 모르는 상태였다.
#   그래서 `# INV(slug)` 아래 딸린 ck() 이름들을 모아 «그 묶음이» 깨졌는지 본다.
RE_INV_MARK = re.compile(r"^\s*#\s*INV\(([a-z0-9][a-z0-9-]*)\)\s*$")
RE_CK = re.compile(r'ck\(\s*(?:f?")([^"]+)"')


def owned_checks(tmp):
    """slug → 그 불변식에 딸린 ck() 이름 집합.

    `# INV(slug)` 부터 다음 `# INV(` 또는 섹션 print 까지를 그 불변식의 묶음으로 본다.
    ⚠️ f-문자열 ck 이름은 중괄호가 들어가 실행 결과와 글자가 다르다 — 그래서
      «중괄호 앞부분» 으로 맞춘다. 완벽하지 않지만 귀속이 «틀리는» 쪽은 없다.
    """
    src = io.open(Path(tmp) / "test_reach_cache.py", encoding="utf-8").read()
    owned, cur = {}, None
    for line in src.splitlines():
        m = RE_INV_MARK.match(line)
        if m:
            cur = m.group(1)
            owned.setdefault(cur, set())
            continue
        if line.lstrip().startswith("print("):
            cur = None
            continue
        if cur:
            c = RE_CK.search(line)
            if c:
                owned[cur].add(c.group(1).split("{")[0].strip())
    return owned


def attributed(fails, names):
    """실패한 테스트 중 «그 불변식 묶음» 의 것을 돌려준다(없으면 None).

    ⚠️ 참/거짓만 돌려주면 출력에 «첫» 실패를 찍게 되고, 그게 귀속된 테스트가 아닐 때
      읽는 사람이 「엉뚱한 게 잡았다」 로 오해한다 — 실제로 한 번 그랬다.
      귀속된 줄을 돌려줘서 출력이 «무엇이 잡았는지» 를 말하게 한다.
    """
    for f in fails:
        body = f[4:].strip()
        for n in names:
            if n and body.startswith(n[:24]):
                return f
    return None


print("--- ① 손대지 않은 사본 (돌연변이 없이 통과해야 한다) ---")
d = setup()
code, fails, out = run_tests(d)
print(f"  exit={code} · 실패 {len(fails)}건 "
      + ("✅" if code == 0 and not fails else "🔴 기준선이 이미 깨졌다"))
assert code == 0, out[-500:]

print()
print("--- ② 불변식마다 코드를 틀어 본다 ---")
caught, escaped, skipped = [], [], []
misattributed, unowned = [], []
OWNED = owned_checks(setup())
for slug, desc, find, repl in MUTATIONS:
    d = setup()
    p = d / "reach.py"
    s = io.open(p, encoding="utf-8").read()
    if find not in s:
        skipped.append((slug, desc))
        print(f"  [?] {slug:<30} 앵커 없음 — 돌연변이를 못 넣었다")
        continue
    io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(find, repl, 1))
    code, fails, out = run_tests(d)
    names = OWNED.get(slug, set())
    if code == 0:
        escaped.append((slug, desc))
        print(f"  [X] {slug:<30} 🔴 «안 잡힘» — {desc}")
    elif not names:
        unowned.append((slug, desc))
        print(f"  [?] {slug:<30} ⚠️ 잡혔지만 이 불변식에 딸린 테스트를 못 찾음 "
              f"(# INV({slug}) 표시 확인)")
    elif attributed(fails, names):
        caught.append(slug)
        hit = attributed(fails, names)
        extra = f" (+{len(fails) - 1}건 더)" if len(fails) > 1 else ""
        print(f"  [O] {slug:<30} 잡힘 · {hit[:62]}{extra}")
    else:
        misattributed.append((slug, desc, fails[:2]))
        print(f"  [~] {slug:<30} 🔴 «엉뚱한 테스트가» 잡음 — 이 불변식은 재지 못했다")
        for f in fails[:2]:
            print(f"        {f[:88]}")

print()
print(f"잡힘 {len(caught)} · 안 잡힘 {len(escaped)} · 귀속 불명 "
      f"{len(misattributed)} · 묶음 없음 {len(unowned)} · 못 넣음 {len(skipped)}")
if misattributed:
    print()
    print("🔴 «엉뚱한 테스트가» 잡은 것 — 종료코드는 1 이지만 그 불변식은 재지 "
          "못했다. 돌연변이가 다른 가드를 먼저 건드리는 것이다(DB 가 찾은 모양):")
    for slug, desc, fs in misattributed:
        print(f"     {slug}: {desc}")
        print(f"        → 돌연변이를 «그 불변식만» 건드리게 좁히거나, 그 불변식의 "
              f"테스트를 더 날카롭게 하십시오.")
if unowned:
    print()
    print("⚠️ 잡혔지만 어느 테스트가 잡았는지 귀속할 수 없는 것 "
          "(# INV(slug) 표시가 ck 위에 없음):")
    for slug, desc in unowned:
        print(f"     {slug}: {desc}")
if escaped:
    print()
    print("🔴 안 잡힌 것 — 둘 중 하나다. 먼저 ②를 확인한다:")
    print("   ① 테스트가 약하다")
    print("   ② 돌연변이가 «동작을 안 바꿨다»(중복/죽은 줄) ← DB 가 이쪽이었다")
    for slug, desc in escaped:
        print(f"     {slug}: {desc}")
if skipped:
    print()
    print("⚠️ 앵커를 못 찾은 것 — 돌연변이 자체가 안 들어갔으니 «모른다» 다:")
    for slug, desc in skipped:
        print(f"     {slug}: {desc}")

# 🔴 종료 코드를 낸다. 찍기만 하면 게이트가 이걸 보고도 통과한다 —
#   「상태코드를 찍기만 하던」 라이브 검증과 같은 자리다.
# ⚠️ 앵커를 못 찾은 것도 실패로 본다. 돌연변이가 «안 들어갔으면» 그 불변식을
#   재지 못한 것이고, 「모른다」 를 「통과」 로 세면 이 파일이 아무 일도 안 한다.
if escaped or skipped or misattributed or unowned or not caught:
    sys.exit(1)
print()
print(f"✅ 돌연변이 {len(caught)}개 전부 테스트가 잡습니다 — 검사에 이가 있습니다.")
print("⚠️ 확인하지 않음 — 여기 «적지 않은» 돌연변이는 대상이 아닙니다. "
      "불변식마다 하나씩이고, 한 불변식의 모든 위반을 다 대지는 않습니다.")
