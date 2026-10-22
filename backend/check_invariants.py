# -*- coding: utf-8 -*-
"""불변식 ↔ 테스트 대조 — 「주석에 약속하고 테스트를 안 붙인」 것을 막는다.

★ 왜 이 파일이 있나
  오늘(2026-10-22) 제 코드의 규범 문장 ~35개를 «손으로» 뽑아 강제되는지 댔다.
  두 건이 걸렸다(환율 폴백 무표시 · 독스트링이 코드와 어긋남). 그런데 그건
  «한 번짜리» 다 — 다음에 또 손으로 해야 하고, 안 하면 다시 쌓인다.

  처음엔 주석을 전수 긁어 「하지 않는다」 류 문장을 찾을 생각이었다. DB 가 같은
  방식을 자기 문서에 시도했는데 «거짓 양성이 쏟아졌다»(데이터셋 이름·다른 표의
  열·내부 파티션 열이 섞인다). 거짓 양성은 검사를 죽인다 — 내가 거짓 실패
  (TLS 타임아웃)에서 겪은 그 비용이다.

  🔑 DB 가 돈 길: «기계가 읽을 구간을 문서에 표시하게» 한다. 표시된 것만 대니
    거짓 양성이 0 이다. 그리고 부수 효과가 본 효과보다 크다 — 표시할 자리를
    만들면 «계약을 적게 강제» 된다. 목록이 없으면 대조할 것도 없고, 그게 곧
    「계약을 안 적었다」 는 뜻이다.

  그 길을 코드에 옮긴 것이 이 파일이다:

      reach.py 등에   # INVARIANT(slug): 사람이 읽는 한 줄
      테스트에        INV(slug)

  양방향으로 댄다. 표시 안 한 주석은 «대상이 아니다» — 그게 이 방식의 한계이고,
  「10/10 뷰 조회됨」을 「작동한다」로 읽던 자리와 같은 종류다. 그래서 적어 둔다.

⚠️ 표시가 하나도 없으면 «통과가 아니라 실패» 다. 0 을 깨끗함으로 읽지 않는다.

실행: python check_invariants.py
"""
import io
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = ("reach.py", "bq.py", "mart.py", "dplan.py", "status.py", "gaps.py")
TESTS = ("test_reach_cache.py",)

# 표시 형식. 느슨하게 받지 않는다 — 느슨하면 오타가 «없는 불변식» 으로 통과한다.
RE_INV = re.compile(r"#\s*INVARIANT\(([a-z0-9][a-z0-9-]*)\)\s*:\s*(.+?)\s*$")
RE_USE = re.compile(r"INV\(([a-z0-9][a-z0-9-]*)\)")


def scan(names, pattern, label):
    found = {}
    for n in names:
        p = HERE / n
        if not p.exists():
            continue
        for i, line in enumerate(io.open(p, encoding="utf-8").read().splitlines(), 1):
            m = pattern.search(line)
            if m:
                found.setdefault(m.group(1), []).append((n, i))
    return found


def main():
    declared = scan(SRC, RE_INV, "선언")
    covered = scan(TESTS, RE_USE, "검증")

    print("=== 불변식 ↔ 테스트 대조 ===")
    # ⚠️ 표시가 없으면 실패다. 「대조할 것이 없음」 을 「문제 없음」 으로 읽으면
    #   이 파일이 아무 일도 안 하면서 통과를 보고한다 — 가장 나쁜 실패다.
    if not declared:
        print("🔴 선언된 불변식이 하나도 없습니다 — 대조한 것이 없습니다.")
        print("   코드에 `# INVARIANT(slug): 설명` 을 적고 테스트에 INV(slug) 를 쓰십시오.")
        return 1

    missing = sorted(set(declared) - set(covered))     # 약속만 있고 테스트 없음
    orphan = sorted(set(covered) - set(declared))      # 테스트만 있고 약속 없음

    print(f"선언 {len(declared)}개 · 검증 {len(covered)}개")
    for slug in sorted(declared):
        where = declared[slug][0]
        mark = "OK  " if slug in covered else "FAIL"
        n = len(covered.get(slug, []))
        print(f"  {mark} {slug:<28} {where[0]}:{where[1]:<5} 테스트 {n}건")

    if missing:
        print()
        print(f"🔴 약속만 있고 테스트가 없는 불변식 {len(missing)}개:")
        for slug in missing:
            f, ln = declared[slug][0]
            print(f"    {slug}  ({f}:{ln})")
        print("   → 테스트에 INV(<slug>) 를 적고 그 불변식을 대십시오.")
        print("   ⚠️ 주석은 지켜지는 규칙처럼 읽힙니다. 지키게 만들거나 지우십시오.")
    if orphan:
        print()
        print(f"⚠️ 테스트가 가리키는데 선언이 없는 불변식 {len(orphan)}개:")
        for slug in orphan:
            f, ln = covered[slug][0]
            print(f"    {slug}  ({f}:{ln})")
        print("   → 슬러그 오타이거나, 코드에서 그 불변식을 지웠습니다.")

    if missing or orphan:
        return 1
    print()
    print(f"✅ 선언된 불변식 {len(declared)}개 전부 테스트가 가리킵니다.")
    print("⚠️ 확인하지 않음 — 표시하지 «않은» 주석은 대상이 아닙니다. 이 검사는 "
          "「약속한 것을 지키는가」만 보고 「약속을 다 적었는가」는 보지 않습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
