# -*- coding: utf-8 -*-
"""벤치마크 준비 상태 점검 — 어디서 막혀 있고, 누가 무엇을 해야 하는가.

왜 이 파일이 있나
  도달 곡선 세 벤더는 «자격증명 → 수집 → 캐시 → 화면» 네 칸을 거친다. 한 칸이라도
  비면 화면이 안 켜지는데, 어느 칸인지는 각자 다른 곳을 봐야 안다(시크릿·BQ·API).
  매번 사람이 세 군데를 뒤지면 «아직 안 됐나?» 를 묻게 되고, 그러면 아무도 안 본다.
  이 스크립트가 네 칸을 한 번에 찍는다.

  ⚠️ 토큰 «추출» 은 자동화할 수 없다. 1Password 공유 링크 안에 있고, 일회성 열람일 수
    있어 사람이 아닌 쪽이 열면 정작 사람이 못 꺼낸다. 그 한 단계만 사람이 하고,
    나머지는 데이터가 들어오면 스스로 켜진다.

실행: python status.py
"""
import os
import sys

PROJECT = "innocean-perf-apac-kr"
OK, NO, WAIT = "[O]", "[X]", "[.]"


def _sm_names():
    try:
        from google.cloud import secretmanager as sm
        c = sm.SecretManagerServiceClient()
        return [s.name.split("/")[-1] for s in
                c.list_secrets(request={"parent": f"projects/{PROJECT}"})]
    except Exception as e:
        print(f"  (시크릿 조회 실패: {str(e)[:80]})")
        return None


def _bq():
    from google.cloud import bigquery
    return bigquery.Client(project=PROJECT)


def _view_rows(c, view, status_col="curve_status"):
    """(ok 행, 전체 행) — 뷰가 없으면 None."""
    try:
        c.get_table(view.strip("`"))
    except Exception:
        return None
    try:
        r = list(c.query(
            f"SELECT COUNTIF(IFNULL({status_col},'')='ok') ok, COUNT(*) n FROM {view}"
        ).result())[0]
        return r["ok"], r["n"]
    except Exception:
        return None


def main():
    print("=" * 72)
    print("벤치마크 준비 상태")
    print("=" * 72)

    names = _sm_names()
    c = _bq()

    # ── 벤더별 체인 ────────────────────────────────────────────────
    vendors = [
        {"key": "netflix", "label": "넷플릭스 Reach Curve",
         "secrets": ["netflix-netkey-staging", "netflix-netkey-production"],
         "view": f"`{PROJECT}.apac_kr_benchmark.v_netflix_reach`",
         "human": "1Password 에서 NetKey 를 꺼내 "
                  "python deploy/set_netflix_key.py --env staging 실행 "
                  "(발급일도 함께 기록 — 유효기간 1년)"},
        {"key": "google", "label": "Google ReachPlanService",
         "secrets": ["google-ads-developer-token"],
         "view": f"`{PROJECT}.apac_kr_benchmark.v_google_reach_plan`",
         "human": "MCC API 센터에서 기본(Basic) 등급 토큰 확인 — "
                  "8286530634(lc2015) · 7297527650(dplan360) · 9958618792(hmca). "
                  "없으면 신청(심사 필요). 테스트 등급은 실제 예측이 안 나온다"},
        {"key": "meta_rf", "label": "Meta Reach & Frequency",
         "secrets": [],   # 기존 토큰 재사용 — 권한만 추가하면 된다
         "view": f"`{PROJECT}.apac_kr_benchmark.v_meta_rf_curve`",
         "human": "비즈니스 관리자에서 ads_management 부여 여부 «결정». "
                  "R&F 전용 시스템 사용자 권고(1.9% 를 위해 13계정 ₩94.5억에 쓰기 권한)"},
    ]

    for v in vendors:
        print(f"\n── {v['label']} " + "─" * max(0, 50 - len(v["label"])))
        # 1) 자격증명
        if v["secrets"]:
            if names is None:
                print(f"  {WAIT} 자격증명  확인 불가")
            else:
                have = [s for s in v["secrets"] if s in names]
                if have:
                    print(f"  {OK} 자격증명  {', '.join(have)}")
                else:
                    print(f"  {NO} 자격증명  없음 ({' / '.join(v['secrets'])})")
        else:
            print(f"  {WAIT} 자격증명  기존 메타 토큰 재사용 — 권한(ads_management)만 필요")
        # 2) 수집 → 캐시
        rows = _view_rows(c, v["view"])
        if rows is None:
            print(f"  {NO} 캐시      뷰 없음 ({v['view'].split('.')[-1].strip('`')})")
        else:
            ok_n, all_n = rows
            mark = OK if ok_n else (WAIT if all_n else NO)
            print(f"  {mark} 캐시      쓸 수 있는 곡선 {ok_n:,} / 격자 {all_n:,}")
        # 3) 화면
        try:
            import reach
            prov = next((p for p in reach._PROVIDERS if p.name in
                         (v["key"], "google_reachplan" if v["key"] == "google" else v["key"])), None)
            if prov:
                try:
                    avail, why = prov.available("")
                except TypeError:
                    avail, why = prov.available()
                print(f"  {OK if avail else NO} 화면      {'켜짐' if avail else '꺼짐'}")
        except Exception as e:
            print(f"  (제공자 확인 실패: {str(e)[:60]})")
        print(f"  -> 사람이 할 일: {v['human']}")

    # ── 우리 실측(메타 누적 도달) ──────────────────────────────────
    print("\n── 우리 실측 적합 (메타 누적 도달) " + "─" * 20)
    try:
        r = list(c.query(f"""
            SELECT COUNT(*) n,
                   COUNTIF(delivery_days_actual IS NOT NULL
                           AND delivery_within_reach_window) usable
            FROM `{PROJECT}.apac_kr_benchmark.bm_meta_campaign_reach`""").result())[0]
        print(f"  {OK} 마트      전체 {r['n']:,} · 적합 가능 {r['usable']:,}")
    except Exception as e:
        print(f"  {NO} 마트      {str(e)[:70]}")

    print("\n" + "=" * 72)
    print("토큰 «추출» 만 사람이 합니다. 들어오면 수집·캐시·화면은 스스로 켜집니다.")
    print("=" * 72)


if __name__ == "__main__":
    for _k in (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
               os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                               "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))):
        if _k and os.path.exists(_k):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
            break
    sys.exit(main() or 0)
