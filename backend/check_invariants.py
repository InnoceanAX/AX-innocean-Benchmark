# -*- coding: utf-8 -*-
"""불변식 ↔ 테스트 대조 — 「주석에 약속하고 테스트를 안 붙인」 것을 막는다.

★ 왜 이 파일이 있나
  2026-10-22 에 제 코드의 규범 문장 ~35개를 «손으로» 뽑아 강제되는지 댔다. 두 건이
  걸렸다(환율 폴백 무표시 · 독스트링이 코드와 어긋남). 그런데 그건 «한 번짜리» 다 —
  다음에 또 손으로 해야 하고, 안 하면 다시 쌓인다.

  처음엔 주석을 전수 긁어 「하지 않는다」 류 문장을 찾을 생각이었다. DB 가 같은 방식을
  자기 문서에 시도했고 «거짓 양성이 쏟아졌다»(데이터셋 이름·다른 표의 열·내부 파티션
  열이 섞인다). 거짓 양성은 검사를 죽인다 — 거짓 실패(TLS 타임아웃)에서 겪은 그 비용이다.

  🔑 DB 가 돈 길: «기계가 읽을 자리에 적게» 만든다. 표시된 것만 대니 거짓 양성이 0 이다.
    부수 효과가 본 효과보다 크다 — 표시할 자리를 만들면 «불변식을 한 줄로 적게» 강제된다.

      코드    # INVARIANT(slug): 사람이 읽는 한 줄
              # INVARIANT(slug, untested): 아직 테스트가 없다고 «밝힌» 것
      테스트   INV(slug)

⚠️ 이 검사가 확인하지 «않는» 것: 표시하지 않은 주석. 「약속한 것을 지키는가」만 보고
  「약속을 다 적었는가」는 보지 않는다 — 「10/10 뷰 조회됨」을 「작동한다」로 읽던
  자리와 같은 종류다.

🔴 세 가지를 «실패» 로 본다. 전부 「조용히 통과」 하던 길이다:
  ① 표시가 하나도 없음        → 대조한 것이 없다. 0 을 깨끗함으로 읽지 않는다
  ② 표시가 «틀리게» 적힘      → 건너뛰어진다. 실측으로 확인했다(대문자·밑줄·콜론 누락·
                              이름 오타 셋이 전부 조용히 통과했고 ✅ 가 찍혔다).
                              DB 가 자기 문서 검사에서 찾은 「닫는 표시 누락」과 같다
  ③ 슬러그 오타              → 테스트가 «아무것도 안 대면서» 통과한다

실행: python check_invariants.py
"""
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 화면(index.html)도 본다 — 거기 약속이 더 숨기 쉽다. 테스트가 브라우저 probe 라
#   게이트에 못 붙여서, 그 불변식들은 `untested` 로 «밝혀» 둔다(빚으로 보인다).
SRC = ("reach.py", "bq.py", "mart.py", "dplan.py", "status.py", "gaps.py",
       "../index.html")
TESTS = ("test_reach_cache.py",)

# 엄격 — 이 모양만 «대조된다»
RE_INV = re.compile(
    r"(?:#|//)\s*INVARIANT\(([a-z0-9][a-z0-9-]*)(\s*,\s*untested)?\)\s*:\s*(.+?)\s*$")
RE_USE = re.compile(r"\bINV\(([a-z0-9][a-z0-9-]*)\)")
# 느슨 — 「적으려 한 흔적」. 엄격과 개수가 다르면 그 줄은 조용히 건너뛰어진 것이다
RE_INV_LOOSE = re.compile(r"INVARIANT")
RE_USE_LOOSE = re.compile(r"\bINV\b")


def _lines(names):
    for n in names:
        p = HERE / n
        if not p.exists():
            continue
        for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
            yield n, i, line


def scan_decl():
    """엄격 매치 · untested 표시 · 느슨하지만 안 잡힌 줄."""
    ok, untested, malformed = {}, set(), []
    for n, i, line in _lines(SRC):
        m = RE_INV.search(line)
        if m:
            ok.setdefault(m.group(1), []).append((n, i))
            if m.group(2):
                untested.add(m.group(1))
        elif RE_INV_LOOSE.search(line):
            malformed.append((n, i, line.strip()[:90]))
    return ok, untested, malformed


def scan_use():
    ok, malformed = {}, []
    for n, i, line in _lines(TESTS):
        m = RE_USE.search(line)
        if m:
            ok.setdefault(m.group(1), []).append((n, i))
        elif RE_USE_LOOSE.search(line):
            malformed.append((n, i, line.strip()[:90]))
    return ok, malformed


def main():
    declared, untested, bad_decl = scan_decl()
    covered, bad_use = scan_use()

    print("=== 불변식 ↔ 테스트 대조 ===")
    fail = 0

    # 🔴 ② 틀리게 적힌 표시 — 가장 먼저 본다. 이게 있으면 아래 숫자가 거짓이다.
    if bad_decl or bad_use:
        print(f"🔴 표시가 «틀리게» 적힌 줄 {len(bad_decl) + len(bad_use)}개 — "
              "그 줄은 대조되지 «않습니다»:")
        for n, i, txt in (bad_decl + bad_use):
            print(f"    {n}:{i}  {txt}")
        print("   → 형식은 `# INVARIANT(slug): 설명` 과 `INV(slug)` 입니다"
              " (슬러그는 소문자·숫자·붙임표).")
        print("   ⚠️ 틀린 표시는 «없는 불변식» 이 되어 조용히 통과합니다 — 실측 확인됨.")
        fail = 1

    # 🔴 ① 표시가 하나도 없으면 실패. 「대조할 것이 없음」 을 「문제 없음」 으로 읽으면
    #   이 파일이 아무 일도 안 하면서 통과를 보고한다 — 가장 나쁜 실패다.
    if not declared:
        print("🔴 선언된 불변식이 하나도 없습니다 — 대조한 것이 없습니다.")
        return 1

    missing = sorted(set(declared) - set(covered) - untested)
    orphan = sorted(set(covered) - set(declared))      # 🔴 ③ 슬러그 오타
    declared_untested = sorted(untested - set(covered))

    print(f"선언 {len(declared)}개 (테스트 대상 {len(declared) - len(untested)} · "
          f"테스트 없음으로 «밝힌» 것 {len(untested)}) · 검증 {len(covered)}개")
    for slug in sorted(declared):
        where = declared[slug][0]
        n = len(covered.get(slug, []))
        if slug in untested and n == 0:
            mark = "밝힘"
        elif n:
            mark = "OK  "
        else:
            mark = "FAIL"
        print(f"  {mark} {slug:<28} {where[0]}:{where[1]:<5} 테스트 {n}건")

    if missing:
        print()
        print(f"🔴 약속만 있고 테스트가 없는 불변식 {len(missing)}개:")
        for slug in missing:
            f, ln = declared[slug][0]
            print(f"    {slug}  ({f}:{ln})")
        print("   → 테스트에 INV(<slug>) 를 적고 그 불변식을 대십시오.")
        print("   → 아직 못 대는 것이면 `# INVARIANT(slug, untested):` 로 «밝히십시오».")
        print("   ⚠️ 주석은 지켜지는 규칙처럼 읽힙니다. 지키게 만들거나 밝히거나 지우십시오.")
        fail = 1
    if orphan:
        print()
        print(f"🔴 테스트가 가리키는데 선언이 없는 불변식 {len(orphan)}개:")
        for slug in orphan:
            f, ln = covered[slug][0]
            print(f"    {slug}  ({f}:{ln})")
        print("   → 슬러그 오타이거나, 코드에서 그 불변식을 지웠습니다. "
              "오타면 그 테스트는 «아무것도 대지 않습니다».")
        fail = 1

    # ⚠️ 밝힌 것을 «매번» 찍는다. DB 방식(제외한 것을 따로 남긴다)이다 —
    #   「제외했다고 잊으면 그게 구멍」 이라서, 게이트는 안 막고 목록에는 남긴다.
    if declared_untested:
        print()
        print(f"⚠️ 테스트가 «아직» 없다고 밝힌 불변식 {len(declared_untested)}개 "
              "— 게이트를 막지 않지만 빚입니다:")
        for slug in declared_untested:
            f, ln = declared[slug][0]
            print(f"    {slug}  ({f}:{ln})")

    if fail:
        return 1
    print()
    print(f"✅ 테스트 대상 불변식 {len(declared) - len(untested)}개 전부 "
          "테스트가 가리킵니다.")
    print("⚠️ 확인하지 않음 — 표시하지 «않은» 주석은 대상이 아닙니다. 이 검사는 "
          "「약속한 것을 지키는가」만 보고 「약속을 다 적었는가」는 보지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
