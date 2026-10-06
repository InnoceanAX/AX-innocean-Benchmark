"""
벤치마크 전용 데이터 마트 빌더 (다차원).

원칙:
- 로우데이터·공용 통합층은 읽기 전용(SELECT only). 쓰기는 `apac_kr_benchmark` 에만.
- 벤치마크 백엔드(bq.py)는 이 마트만 소비.

산출물:
- bm_campaign_monthly : 캠페인 × 월 grain. 차원(매체·국가·업종·캠페인목표·브랜드·대행사) + 지표.
  → 백엔드가 임의의 기준차원 × 필터 조합으로 4분위 벤치마크를 동적 계산.

⚠️ 2026-09-22 컬럼 의미 변경 (이 마트를 읽는 다른 솔루션 주의)
  rev      : revenue_krw(혼합) → **revenue_purchase_krw(구매 계층)** 으로 바뀌었다.
             이전 값으로 계산한 ROAS 는 google_ads 기준 1,676%로 부풀려져 있었다(정정 후 246%).
  rev_pur  : rev 와 같은 값. '구매 계층'임을 이름으로 드러내려고 추가.
  rev_all  : 예전 rev(혼합 전환가치). 진단·대조용. **ROAS 분자로 쓰지 말 것.**
  conv     : 혼합 전환(google_ads 는 구매·리드·참여를 합산 — 92.7%가 참여). CPA·ROAS 에 쓰지 말 것.
  conv_pur / conv_lead : 구매·리드 계층 전환.

데이터 현실: 스펜드 ~99% 현대·기아 자동차. 업종 다양성은 약하나, 국가/캠페인목표/브랜드는 풍부.
UPSTREAM: apac_kr_unified.v_perf_unified
실행: python mart.py [--check]
"""
import os
import sys
from google.cloud import bigquery
from industry_map import industry_case_sql, industry_expr, objective_case_sql

PROJECT = "innocean-perf-apac-kr"
MART_DS = "apac_kr_benchmark"
LOCATION = "asia-northeast3"
SOURCE = f"`{PROJECT}.apac_kr_unified.v_perf_unified`"
PLATFORM_TO_MEDIA = {"google_ads": "G", "meta": "M", "dv360": "D", "tiktok": "T", "kakao": "K", "naver": "N"}

for _k in [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
           os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))]:
    if _k and os.path.exists(_k):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
        break


def _client():
    return bigquery.Client(project=PROJECT, location=LOCATION)


def ensure_dataset(c):
    ds_id = f"{PROJECT}.{MART_DS}"
    try:
        c.get_dataset(ds_id)
    except Exception:
        ds = bigquery.Dataset(ds_id)
        ds.location = LOCATION
        ds.description = "INNOCEAN Benchmark 전용 데이터 마트. raw 미접근, 이 데이터셋만 소비."
        c.create_dataset(ds, exists_ok=True)
        print(f"created dataset {ds_id}")


def _media_case(alias="u"):
    """매체 코드 변환. 별칭을 붙인다 — 업종 사전 조인 후 platform 이 양쪽에 생겨 모호해진다."""
    whens = " ".join([f"WHEN '{p}' THEN '{m}'" for p, m in PLATFORM_TO_MEDIA.items()])
    return f"CASE {alias}.platform {whens} ELSE NULL END"


def _plats():
    return ",".join([f"'{p}'" for p in PLATFORM_TO_MEDIA])


def _gname_union(c):
    """google_ads 캠페인명 보강 — v_perf_unified엔 google campaign_name이 NULL이라
    raw ads_Campaign_<계정>(100% 커버)에서 campaign_id→name 매핑을 만든다."""
    import re
    tabs = [t.table_id for t in c.list_tables("apac_kr_raw")
            if re.match(r"ads_Campaign_\d+$", t.table_id)]
    if not tabs:
        return None
    union = " UNION ALL ".join(
        [f"SELECT CAST(campaign_id AS STRING) cid, campaign_name nm FROM `{PROJECT}.apac_kr_raw.{t}`"
         for t in tabs])
    return f"SELECT cid, MAX(nm) nm FROM ({union}) WHERE nm IS NOT NULL GROUP BY cid"


def _channel_map(c):
    """google_ads 광고상품(채널유형) 보강 — raw ads_Campaign_<계정>의
    campaign_advertising_channel_type(SEARCH/DISPLAY/VIDEO/PERFORMANCE_MAX…)에서 campaign_id→channel 매핑."""
    import re
    tabs = [t.table_id for t in c.list_tables("apac_kr_raw")
            if re.match(r"ads_Campaign_\d+$", t.table_id)]
    if not tabs:
        return None
    union = " UNION ALL ".join(
        [f"SELECT CAST(campaign_id AS STRING) cid, campaign_advertising_channel_type ct "
         f"FROM `{PROJECT}.apac_kr_raw.{t}`" for t in tabs])
    return f"SELECT cid, MAX(ct) ct FROM ({union}) WHERE ct IS NOT NULL GROUP BY cid"


def _has_col(c, view, col):
    try:
        return col in [f.name for f in c.get_table(f"{PROJECT}.apac_kr_unified.{view}").schema]
    except Exception:
        return False


def _rev_strict(c, view, alias="u"):
    """구매 계층 매출만. **없으면 0 이다 — revenue_krw 로 후퇴하지 않는다.**

    세그먼트·영상 뷰에는 revenue_purchase_krw 가 없다. 예전엔 revenue_krw 로 후퇴했는데
    그러면 두 가지가 겹쳐 틀린다:
      ① revenue_krw 는 혼합값이라 ROAS 가 부풀려진다(사전 do_not_use).
      ② DB 실측 — device 축은 분모가 캠페인 100% 인데 구매 계층 원천은 전환의 12.1% 뿐이다.
         붙이면 ROAS 가 «그럴듯하게» 8분의 1로 나온다. video 는 6.8% 다.
    0 으로 두면 커버리지 게이트가 그 차원의 ROAS 를 «측정 불가» 로 자동 차단한다.
    DB 가 age·gender 에 컬럼을 추가하면(작업 중) 여기서 자동으로 살아난다.
    """
    col = _pick_col(c, view, ["revenue_purchase_krw"], "ROAS 분자(세그먼트)")
    if col:
        return f"SUM({alias}.{col})"
    print(f"· {view}: revenue_purchase_krw 없음 → 해당 차원 ROAS 차단(0)")
    return "0"


def _conv_strict(c, view, alias="u"):
    """구매 계층 전환만. 없으면 0 → CVR 도 자동 차단(혼합 conv 로 후퇴하지 않는다)."""
    col = _pick_col(c, view, ["conversions_purchase"], "CVR 분자(세그먼트)")
    return f"SUM({alias}.{col})" if col else "0"


_NOTES_CACHE = {}
_CAUTION_SEEN = set()


def _col_notes(c):
    """DB 사전의 컬럼 경고를 한 번 읽어 캐시. {(mart, col): (severity, use_instead)}

    ★ 왜 코드가 사전을 읽는가 —
      2026-09-22 에 `_rev_expr` 이 revenue_purchase_krw 가 없으면 revenue_krw 로 '후퇴'했다.
      사전에는 그 컬럼이 do_not_use 로 등재돼 있었는데(ROAS 1,782% 부풀림), 코드가 사전을
      읽지 않고 '컬럼 존재 여부'만 봤기 때문에 경고가 무력했다.
      DB 에이전트 말대로 «경고는 읽는 쪽이 볼 때만 작동한다». 그래서 읽는다.
    """
    if "v" in _NOTES_CACHE:
        return _NOTES_CACHE["v"]
    out = {}
    try:
        for r in c.query(f"""SELECT mart_name, column_name, severity,
                                    IFNULL(CAST(use_instead AS STRING),'') ui
                             FROM `{PROJECT}.apac_kr_ops.dictionary_column_notes`""").result():
            out[(r["mart_name"], r["column_name"])] = (r["severity"], r["ui"])
    except Exception as e:
        print(f"🔴 [경고] 컬럼 사전(dictionary_column_notes)을 읽지 못했습니다 — "
              f"이번 빌드는 do_not_use 차단 없이 진행합니다: {str(e)[:110]}")
    else:
        if not out:
            print("🔴 [경고] 컬럼 사전이 비어 있습니다 — 이번 빌드는 do_not_use 차단 없이 진행합니다")
        else:
            n_block = sum(1 for v in out.values() if v[0] == "do_not_use")
            print(f"· 컬럼 사전 {len(out)}건 로드(차단 대상 {n_block}건)")
    _NOTES_CACHE["v"] = out
    return out


def _col_allowed(c, view, col):
    """이 컬럼을 써도 되는가. do_not_use 면 (False, [대체후보…]) 를 돌려준다.

    use_instead 계약(DB 확정): 쉼표 구분 목록, 각 항목은 `column` 또는 `dataset.table.column`.
    첫 항목이 기본 대체값. 점이 있으면 «다른 테이블» 이라 컬럼 교체가 아니라 조인 대상 변경이다
    → 자동 전환하지 않고 로그만 남긴다(사람이 판단할 일이다).
    """
    sev, ui = _col_notes(c).get((view, col), ("", ""))
    if sev != "do_not_use":
        # caution 은 막지 않지만 조용히 지나가지도 않는다 — 빌드 기록에 남긴다.
        # (오늘 사고의 본질이 '경고가 로그에도 안 남아 아무도 모른 채 후퇴한 것'이었다)
        if sev == "caution" and (view, col) not in _CAUTION_SEEN:
            _CAUTION_SEEN.add((view, col))
            hint = f" (권장: {ui.split(',')[0].strip()})" if ui else ""
            print(f"· [사전·주의] {view}.{col} 사용{hint}")
        return True, []
    alts = [a.strip() for a in (ui or "").split(",") if a.strip()]
    return False, alts


def _pick_col(c, view, candidates, label, _seen=None):
    """후보 중 '존재하고 사전이 막지 않은' 첫 컬럼. 없으면 None.

    ★ 대체 컬럼도 다시 검증한다 — 사전이 가리키는 대체값이 그 자체로 do_not_use 일 수 있다.
      실제로 DB 사전에 `revenue_local → revenue_krw` 가 있었는데 revenue_krw 가 do_not_use 였다
      (2026-09-22, DB 가 1순위를 revenue_purchase_krw 로 정정). 경고가 경고를 무력화하는 경로다.
      DB 가 「1순위는 절대 do_not_use 를 가리키지 않는다」를 불변식으로 지키겠다고 했지만,
      소비 측이 그 불변식에 의존하면 깨졌을 때 조용히 틀린다. 그래서 여기서도 검증한다.
    """
    _seen = _seen or set()
    for col in candidates:
        if col in _seen:
            continue
        _seen.add(col)
        if not _has_col(c, view, col):
            continue
        ok, alts = _col_allowed(c, view, col)
        if ok:
            return col
        # 같은 테이블 안의 대체 후보만 자동 전환 대상
        same_table = [a for a in alts if "." not in a]
        cross_table = [a for a in alts if "." in a]
        for a in cross_table:
            print(f"· [사전] {view}.{col} 은 do_not_use. 대체 후보 {a} 는 다른 테이블이라 "
                  f"자동 전환하지 않습니다 — 조인 대상 변경은 사람이 판단할 일입니다({label})")
        if same_table:
            picked = _pick_col(c, view, same_table, label, _seen)   # 대체값도 다시 검증
            if picked:
                print(f"· [사전] {view}.{col} 은 do_not_use → {picked} 로 대체({label})")
                return picked
            print(f"· [사전] {view}.{col} 의 대체 후보가 전부 막혔거나 없습니다 → {label} 차단")
        else:
            print(f"· [사전] {view}.{col} 은 do_not_use, 같은 테이블 내 대체 없음 → {label} 차단")
    return None


def _rev_expr(c, view, alias="u"):
    """ROAS 분자 — **`revenue_purchase_krw` 를 쓴다. `revenue_krw` 가 아니다.**

    DB 사전(`apac_kr_ops.dictionary_column_notes`)에 `v_perf_unified.revenue_krw` 는
    `severity='do_not_use'` 로 등재돼 있다: "혼합값이다. google_ads 는 전 카테고리 전환가치
    합이라 ROAS 가 1,782% 로 부풀려진다. ROAS 에 쓰지 말 것."
    같은 사전이 `revenue_purchase_krw` 에 대해 "ROAS 는 이것으로 계산한다" 고 명시한다.

    실측(2026-01~, google_ads): revenue_krw 기준 ROAS 1,676% → revenue_purchase_krw 기준 246%.
    구매 전환가치가 없는 매체는 0 이 되고, 커버리지 게이트가 ROAS 를 자동으로 숨긴다
    (= '0배' 가 아니라 '미측정'으로 처리된다).
    """
    col = _pick_col(c, view, ["revenue_purchase_krw", "revenue_krw"], "ROAS 분자")
    return f"SUM({alias}.{col})" if col else "0"


def _rev_all_expr(c, view, alias="u"):
    """혼합 전환가치(`revenue_krw`) — 진단·대조 전용. ROAS 분자로 쓰지 말 것."""
    return f"SUM({alias}.revenue_krw)" if _has_col(c, view, "revenue_krw") else "0"


def _conv_tiers(c, view, alias="u"):
    """전환 계층 — `conversions` 단독은 CPA 에 쓸 수 없다.

    사전: `v_perf_unified.conversions` = `do_not_use`
      "혼합값이다. google_ads 는 PURCHASE 와 PAGE_VIEW·ENGAGEMENT 를 같은 전환으로 더한다
       (전환의 92.7%가 engagement). ROAS·CPA 계산에 쓰지 말 것."
    → 구매·리드 계층을 따로 실어 CPA 는 구매 기준으로 내고, 혼합 `conv` 는 참고용으로만 둔다.
    """
    out = []
    for col, alias_out in (("conversions_purchase", "conv_pur"), ("conversions_lead", "conv_lead")):
        out.append(f"SUM({alias}.{col}) AS {alias_out}" if _has_col(c, view, col)
                   else f"0 AS {alias_out}")
    return ", ".join(out)


def build_campaign(c):
    """캠페인 × 월 grain 다차원 테이블. google 캠페인명은 raw에서 보강(P0 자동수정)."""
    # 보강된 캠페인명 텍스트 (google: raw, 그 외: v_perf_unified)
    name_expr = "COALESCE(NULLIF(u.campaign_name,''), g.nm, '')"
    # 업종 — DB 사전 `apac_kr_ops.advertiser_industry` (platform × advertiser_id, 100% 매칭) 우선.
    # 없으면 v_perf_unified.industry, 그것도 없으면 정규식 폴백.
    # 2026-09-22 DB 신설(656행). 「기타」 54% → 24.4%. 크게 기여한 두 가지:
    #   ① 현대 해외 법인 코드(HMB·HMPH·HMTH·HMCA 등)를 수송/항공으로
    #   ② 보험 규칙을 자동차보다 '먼저' 걸리게 — 현대해상이 '현대'에 걸려 자동차로 잡히던 문제
    ijoin, isrc = "", "CAST(NULL AS STRING)"
    _fallback = f"CONCAT(IFNULL(u.advertiser_name,''),' ',{name_expr})"
    if _table_exists(c, "apac_kr_ops", "advertiser_industry"):
        ijoin = (f"LEFT JOIN `{PROJECT}.apac_kr_ops.advertiser_industry` ai "
                 f"ON u.platform = ai.platform "
                 f"AND CAST(u.advertiser_id AS STRING) = CAST(ai.advertiser_id AS STRING)")
        # 폴백 순서: 사전 → «광고주/브랜드만» → 캠페인명.
        # 캠페인명을 먼저 보면 whats«app» 이 'app' 토큰에 걸려 현대차가 '앱/사이트'가 된다
        # (실측 ₩6.2억). 자세한 근거는 industry_map.industry_expr() 주석 참조.
        _brand_only = "LOWER(CONCAT(IFNULL(u.advertiser_name,''),' ',IFNULL(u.brand,'')))"
        ind = (f"COALESCE(NULLIF(ai.industry,''), "
               f"NULLIF({industry_case_sql(_brand_only)},'기타'), "
               f"{industry_case_sql(_fallback)})")
        # 'rule'(이름 기반 추정) vs 'ops_ledger'(운영팀 원장) — 화면에서 «추정 분류» 배지에 쓴다
        isrc = "ANY_VALUE(IFNULL(ai.industry_source,'regex_fallback'))"
        print("· apac_kr_ops.advertiser_industry 감지 → 사전 기반 업종 분류(정규식은 폴백)")
    else:
        _has_ind = _has_col(c, "v_perf_unified", "industry")
        ind = industry_expr(_has_ind, _fallback, brand_expr="LOWER(CONCAT(IFNULL(u.advertiser_name,''),' ',IFNULL(u.brand,'')))")
    # 캠페인 목표 — DB 사전 v_perf_unified.objective_layer 우선(2026-09-22 전 매체 확대, 집행 98.4%).
    # ★ 벤치마크에 중요한 이유: 인지(awareness) 캠페인과 리드(lead) 캠페인을 같은 표에 놓으면
    #   CPA·ROAS 가 목적이 달라서 벌어진 것을 성과 차이로 읽는다. 같은 목표끼리 견줘야 한다.
    #   objective_source: setting_l1/l2=매체 설정에서 읽음(신뢰) · name=캠페인명 추론 · unresolved=미상
    _has_obj = _has_col(c, "v_perf_unified", "objective_layer")
    osrc = "CAST(NULL AS STRING)"
    if _has_obj:
        obj = f"COALESCE(NULLIF(u.objective_layer,''), {objective_case_sql(name_expr)})"
        if _has_col(c, "v_perf_unified", "objective_source"):
            osrc = "ANY_VALUE(IFNULL(u.objective_source,'regex_fallback'))"
        print("· v_perf_unified.objective_layer 감지 → 사전 기반 캠페인목표(정규식은 폴백)")
    else:
        obj = objective_case_sql(name_expr)
    gmap = _gname_union(c)
    join = f"LEFT JOIN ({gmap}) g ON CAST(u.campaign_id AS STRING)=g.cid" if gmap else "LEFT JOIN (SELECT '' cid, '' nm) g ON FALSE"
    cmap = _channel_map(c)
    cjoin = f"LEFT JOIN ({cmap}) ch ON CAST(u.campaign_id AS STRING)=ch.cid" if cmap else "LEFT JOIN (SELECT '' cid, CAST(NULL AS STRING) ct) ch ON FALSE"
    # 영상(YouTube) 지표를 캠페인×월 grain으로 부착 — 영상 데이터는 google 전용이라
    # 영상 캠페인은 자기 플랫폼(Google)에 귀속. 영상 캠페인만의 분모(vimp/vcost)로 VTR/CPV 산출(비영상 희석 없음).
    vjoin = ""
    vcols = ("0 AS vimp, 0 AS vviews, 0.0 AS vcost, 0 AS vp25, 0 AS vp50, "
             "0 AS vp75, 0 AS vp100, 0 AS veng, 0 AS vthru")
    if _table_exists(c, "apac_kr_unified", "v_perf_unified_video"):
        vsrc = f"`{PROJECT}.apac_kr_unified.v_perf_unified_video`"
        # 영상뷰는 Google+Meta. campaign_id 충돌 방지 위해 platform까지 조인키에 포함.
        vthru_col = "SUM(thruplay) vthru, " if _has_col(c, "v_perf_unified_video", "thruplay") else "0 vthru, "
        vjoin = (f"LEFT JOIN (SELECT FORMAT_DATE('%Y-%m', date) vp, CAST(campaign_id AS STRING) vcid, platform vplat, "
                 f"SUM(impressions) vimp, SUM(spend_krw) vcost, SUM(video_views) vviews, {vthru_col}"
                 f"SUM(video_p25) vp25, SUM(video_p50) vp50, SUM(video_p75) vp75, SUM(video_p100) vp100, "
                 f"SUM(engagements) veng FROM {vsrc} WHERE NOT IFNULL(is_excluded,FALSE) GROUP BY vp, vcid, vplat) vid "
                 f"ON CAST(u.campaign_id AS STRING)=vid.vcid AND FORMAT_DATE('%Y-%m',u.date)=vid.vp AND u.platform=vid.vplat")
        vcols = ("IFNULL(ANY_VALUE(vid.vimp),0) AS vimp, IFNULL(ANY_VALUE(vid.vviews),0) AS vviews, "
                 "IFNULL(ANY_VALUE(vid.vcost),0) AS vcost, IFNULL(ANY_VALUE(vid.vp25),0) AS vp25, "
                 "IFNULL(ANY_VALUE(vid.vp50),0) AS vp50, IFNULL(ANY_VALUE(vid.vp75),0) AS vp75, "
                 "IFNULL(ANY_VALUE(vid.vp100),0) AS vp100, IFNULL(ANY_VALUE(vid.veng),0) AS veng, "
                 "IFNULL(ANY_VALUE(vid.vthru),0) AS vthru")
    # Meta 참여·영상 보강 — DB 정식 뷰 v_perf_unified_meta_ext 소비(raw 파싱 제거, DB 권장). 공유(share) 포함.
    mjoin = ""
    mcols = "0 AS mlclk, 0 AS mv3s, 0 AS meng, 0 AS mcmt, 0 AS mrct, 0 AS mlead, 0 AS mshare"
    if _table_exists(c, "apac_kr_unified", "v_perf_unified_meta_ext"):
        msrc = f"`{PROJECT}.apac_kr_unified.v_perf_unified_meta_ext`"
        mjoin = (f"LEFT JOIN (SELECT FORMAT_DATE('%Y-%m', date) mp, CAST(campaign_id AS STRING) mcid, "
                 f"SUM(IFNULL(link_clicks,0)) mlclk, SUM(IFNULL(video_3s_views,0)) mv3s, "
                 f"SUM(IFNULL(post_engagement,0)) meng, SUM(IFNULL(comment,0)) mcmt, "
                 f"SUM(IFNULL(reaction,0)) mrct, SUM(IFNULL(lead,0)) mlead, SUM(IFNULL(share,0)) mshare "
                 f"FROM {msrc} WHERE NOT IFNULL(is_excluded,FALSE) GROUP BY mp, mcid) mt "
                 f"ON CAST(u.campaign_id AS STRING)=mt.mcid AND FORMAT_DATE('%Y-%m',u.date)=mt.mp "
                 f"AND u.platform='meta'")
        mcols = ("IFNULL(ANY_VALUE(mt.mlclk),0) AS mlclk, IFNULL(ANY_VALUE(mt.mv3s),0) AS mv3s, "
                 "IFNULL(ANY_VALUE(mt.meng),0) AS meng, IFNULL(ANY_VALUE(mt.mcmt),0) AS mcmt, "
                 "IFNULL(ANY_VALUE(mt.mrct),0) AS mrct, IFNULL(ANY_VALUE(mt.mlead),0) AS mlead, "
                 "IFNULL(ANY_VALUE(mt.mshare),0) AS mshare")
    tbl = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    sql = f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, market AS
    SELECT
      FORMAT_DATE('%Y-%m', u.date) AS period,
      {_media_case()} AS media,
      -- market 은 advertiser_dim 조인 결과라 미매핑 광고주는 NULL 이다.
      -- WHERE 로 잘라내면 전체 CPM·CPC 분모에서 조용히 빠진다
      -- (2026-06~09 google_ads 124광고주 ₩66.5억 = 25%가 그렇게 사라지고 있었다).
      -- → '(미상)' 버킷으로 남겨 총계는 맞추고, 국가 축에서 눈에 보이게 한다.
      IFNULL(NULLIF(u.market,''),'(미상)') AS market,
      {ind} AS industry,
      {isrc} AS industry_source,
      {obj} AS objective,
      {osrc} AS objective_source,
      u.brand AS brand,
      IFNULL(NULLIF(u.agency,''),'(미상)') AS agency,
      IFNULL(ch.ct,'(기타)') AS channel,
      u.campaign_id AS campaign_id,
      SUM(u.impressions) AS imp,
      SUM(u.clicks) AS clk,
      SUM(u.spend_krw) AS cost,
      SUM(u.conversions) AS conv,
      {_rev_expr(c, 'v_perf_unified')} AS rev,
      -- rev 와 같은 값이지만 '구매 계층'임을 이름으로 드러낸다(타 솔루션이 이름만 보고 쓰도록).
      {_rev_expr(c, 'v_perf_unified')} AS rev_pur,
      -- 혼합 전환가치(전 카테고리). ROAS 에 쓰면 안 되고 진단·대조용으로만 둔다.
      {_rev_all_expr(c, 'v_perf_unified')} AS rev_all,
      {_conv_tiers(c, 'v_perf_unified')},
      {vcols},
      {mcols},
      CURRENT_TIMESTAMP() AS _built_at
    FROM {SOURCE} u
    {join}
    {ijoin}
    {cjoin}
    {vjoin}
    {mjoin}
    WHERE u.date IS NOT NULL AND NOT IFNULL(u.is_excluded, FALSE)
      AND u.platform IN ({_plats()})
    GROUP BY period, media, market, industry, objective, brand, agency, channel, campaign_id
    HAVING imp > 0
    """
    c.query(sql).result()
    print("bm_campaign_monthly: rebuilt (google 캠페인명 raw 보강 + 영상지표 + Meta actions 부착)")


def _table_exists(c, dataset, table):
    try:
        c.get_table(f"{PROJECT}.{dataset}.{table}")
        return True
    except Exception:
        return False


# 세그먼트 차원: dim → (통합뷰, 뷰의 세그먼트 컬럼). DB가 뷰를 추가하면 자동 빌드(전부 Google).
SEGMENTS = {
    "device": ("v_perf_unified_device", "device"),
    "age":    ("v_perf_unified_age", "age_range"),
    "gender": ("v_perf_unified_gender", "gender"),
}


def build_segment(c, dim, view, col):
    """세그먼트 차원(device/age/gender) 마트. 뷰 없으면 skip.
    뷰엔 advertiser_name/campaign_name 없음 → raw 캠페인명(gmap)으로 목표/업종 보강.
    세그먼트값은 컬럼명 {dim} 으로 표준화 저장."""
    if not _table_exists(c, "apac_kr_unified", view):
        print(f"· {view} 없음 → {dim} 차원 skip (DB 추가 대기)")
        return False
    dsrc = f"`{PROJECT}.apac_kr_unified.{view}`"
    name_expr = "COALESCE(g.nm,'')"
    ind = industry_expr(_has_col(c, view, 'industry'), name_expr,
                        brand_expr=_brand_expr(c, view))
    obj = objective_case_sql(name_expr)
    gmap = _gname_union(c)
    join = (f"LEFT JOIN ({gmap}) g ON CAST(u.campaign_id AS STRING)=g.cid"
            if gmap else "LEFT JOIN (SELECT '' cid,'' nm) g ON FALSE")
    tbl = f"`{PROJECT}.{MART_DS}.bm_{dim}_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, {dim} AS
    SELECT FORMAT_DATE('%Y-%m', u.date) AS period, {_media_case()} AS media,
      IFNULL(NULLIF(u.market,''),'(미상)') AS market, {ind} AS industry, {obj} AS objective, u.brand AS brand,
      UPPER(CAST(u.{col} AS STRING)) AS {dim}, u.campaign_id AS campaign_id,
      SUM(u.impressions) imp, SUM(u.clicks) clk, SUM(u.spend_krw) cost, {_conv_strict(c, view)} AS conv,
      {_rev_strict(c, view)} AS rev,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {dsrc} u
    {join}
    WHERE u.date IS NOT NULL AND NOT IFNULL(u.is_excluded,FALSE)
      AND u.platform IN ({_plats()}) AND u.{col} IS NOT NULL
    GROUP BY period, media, market, industry, objective, brand, {dim}, campaign_id
    HAVING imp > 0
    """).result()
    print(f"· bm_{dim}_monthly: built ({dim} 차원 활성)")
    return True


def build_video(c):
    """영상 벤치마크 마트 bm_video_monthly (media='V'). 소스 v_perf_unified_video(Google 영상 캠페인).
    VTR(조회율)=video_views/imp, CPV=cost/video_views, 완전조회율=video_p100/video_views.
    뷰엔 advertiser/campaign_name 없음 → raw 캠페인명(gmap)으로 목표/업종 보강. 뷰 없으면 skip."""
    view = "v_perf_unified_video"
    if not _table_exists(c, "apac_kr_unified", view):
        print(f"· {view} 없음 → 영상(V) 차원 skip (DB 추가 대기)")
        return False
    dsrc = f"`{PROJECT}.apac_kr_unified.{view}`"
    name_expr = "COALESCE(g.nm,'')"
    ind = industry_expr(_has_col(c, view, 'industry'), name_expr,
                        brand_expr=_brand_expr(c, view))
    obj = objective_case_sql(name_expr)
    gmap = _gname_union(c)
    join = (f"LEFT JOIN ({gmap}) g ON CAST(u.campaign_id AS STRING)=g.cid"
            if gmap else "LEFT JOIN (SELECT '' cid,'' nm) g ON FALSE")
    tbl = f"`{PROJECT}.{MART_DS}.bm_video_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, market AS
    SELECT FORMAT_DATE('%Y-%m', u.date) AS period, 'V' AS media,
      IFNULL(NULLIF(u.market,''),'(미상)') AS market, {ind} AS industry, {obj} AS objective, u.brand AS brand,
      u.campaign_id AS campaign_id,
      SUM(u.impressions) imp, SUM(u.clicks) clk, SUM(u.spend_krw) cost, {_conv_strict(c, view)} AS conv,
      {_rev_strict(c, view)} AS rev,
      SUM(u.video_views) vviews, SUM(u.video_p100) vp100, SUM(u.engagements) eng,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {dsrc} u
    {join}
    WHERE u.date IS NOT NULL AND NOT IFNULL(u.is_excluded,FALSE)
      AND IFNULL(u.video_views,0) > 0
    GROUP BY period, media, market, industry, objective, brand, campaign_id
    HAVING imp > 0
    """).result()
    print("· bm_video_monthly: built (영상 VTR/CPV/완전조회율 활성)")
    return True


# ── 디플랜 NAS(소재 grain) ────────────────────────────────────────────────
# 출처: apac_kr_raw.dplan_archive_rows (NAS 엑셀 적재). 통합뷰 v_dplan_benchmark 는 캠페인 grain 이라
# PPT가 요구하는 소재 단위 표를 못 만든다 → 마트에서 raw 를 소재 grain 으로 집계한다.
# Meta ext 전례(8966458)와 같은 패턴: raw 파싱으로 먼저 살리고, DB가 v_dplan_creative 를 내면 소스만 교체.
#
# ★ 금액 신뢰도 — DB 에이전트 실측(QA/30_):
#   기준이 갈리는 축은 '매체'가 아니라 '파일(=광고주)'이다. NAS 파일은 광고주별로 만들어져 파일 안에서 기준이 일관된다.
#   NAS÷API 금액비율 중앙: 현대백화점 1.0004 · 현대자동차 1.0004 · naver 1.0000 → 순매체비
#                          정관장 1.3566(노출비율 1.0019 인 유효관측 기준) → 총액 의심
#   새로 노출할 매체의 소속 파일: 넷플릭스·티빙·토스·COVI·SMR·Teads = 더현대HI(순매체비),
#                                티빙·TW360 = 현대자동차GN7(순매체비), 블라인드·토스 일부 = 정관장(총액 의심)
#   → 'NAS 전량 분리'는 과보수적. 같은 표 비교를 허용하되 총액 의심 '파일'의 행만 단가지표에서 뺀다.
#   DB 가 spend_basis 를 파일 단위 값(net_estimated / gross_suspected / unknown)으로 채워주면
#   아래 하드코딩 대신 그 컬럼을 쓴다(자동 승계 — _basis_filled 참조).
DPLAN_COST_DENY = ("정관장",)

# ⚠️ 디플랜 NAS 와 API 통합뷰(v_perf_unified)는 같은 캠페인을 양쪽에 갖는다(유튜브·인스타 등).
#    절대 합산하지 말 것 — 화면에서도 별도 데이터소스로 분리해 노출한다. (DB QA/30_ Q1)
DPLAN_NEVER_SUM_WITH_API = True


def build_dplan_creative(c):
    """디플랜 NAS 소재 grain 마트 bm_dplan_creative_monthly.

    소스: apac_kr_unified.v_dplan_creative (DB 정식 뷰, dictionary_marts 등재 = 소비 허가).
    NC 파싱 규칙(creative_format·creative_seconds·creative_ratio)은 DB 가 정본으로 소유한다.
    벤치마크가 raw 를 다시 파싱하면 규칙이 두 곳에 생겨 NC 개정 때 어긋난다(DB QA/30_ Q3).
    → 2026-09-22 raw 정규식 브리지에서 이 뷰 소비로 교체. Meta ext(8966458)와 같은 경로.

    ⚠️ 이 마트는 v_perf_unified 계열(bm_campaign_monthly 등)과 절대 합산하지 않는다.
       같은 캠페인이 NAS 와 API 양쪽에 있다(DB 가 dictionary_marts.purpose 에도 명시).
    """
    view = "v_dplan_creative"
    if not _table_exists(c, "apac_kr_unified", view):
        print(f"· {view} 없음 → 디플랜 소재 마트 skip (DB 신설 대기)")
        return False
    src = f"`{PROJECT}.apac_kr_unified.{view}`"
    tbl = f"`{PROJECT}.{MART_DS}.bm_dplan_creative_monthly`"
    # 매체명 표준화 사전(유튜브/YT/youtube → 유튜브). 없으면 원본 media 를 그대로 쓴다.
    if _table_exists(c, "apac_kr_ops", "dplan_media_dict"):
        djoin = (f"LEFT JOIN `{PROJECT}.apac_kr_ops.dplan_media_dict` d "
                 f"ON LOWER(TRIM(r.media))=LOWER(TRIM(d.alias))")
        mname = "COALESCE(NULLIF(d.media_name,''), r.media)"
        mtype, moper, mapi = "ANY_VALUE(d.media_type)", "ANY_VALUE(d.operator)", "ANY_VALUE(d.api_platform)"
    else:
        djoin, mname = "", "r.media"
        mtype = moper = mapi = "CAST(NULL AS STRING)"
    # 금액 신뢰도 — 파일(file_sha256) 단위 enum. 기준표 apac_kr_ops.dplan_spend_basis.
    # gross_suspected(정관장 파일)만 단가지표에서 뺀다. 노출·클릭·조회는 imp_ratio 1.0 이라 유효.
    # ⚠️ 부분문자열 매칭 금지 — 'net_media / gross=소진금액(마크업 포함)' 같은 서술이 net_declared 다.
    cost_ok_expr = "(LOWER(TRIM(IFNULL(r.spend_basis,''))) != 'gross_suspected')"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(rf"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media_name, advertiser AS
    SELECT
      FORMAT_DATE('%Y-%m', r.date)                                    AS period,
      {mname}                                                         AS media_name,
      {mtype}                                                         AS media_type,
      {moper}                                                         AS media_operator,
      {mapi}                                                          AS api_platform,
      IFNULL(NULLIF(r.industry,''),'(미상)')                          AS industry,
      IFNULL(NULLIF(r.advertiser,''),'(미상)')                        AS advertiser,
      IFNULL(NULLIF(r.brand,''),'(미상)')                             AS brand,
      IFNULL(NULLIF(COALESCE(r.nc_product, r.product),''),'(미상)')   AS product,
      r.campaign_name                                                 AS campaign_name,
      r.adgroup_name                                                  AS adgroup_name,
      COALESCE(r.creative_label, r.creative_name_raw)                 AS creative_name,
      -- '동영상'(4,472행)은 '비디오'의 표기 변형이라 한 형식으로 묶는다. 초수·비율은 그 행들만 비어 있다.
      CASE WHEN r.creative_format IN ('동영상','영상') THEN '비디오'
           ELSE r.creative_format END                                 AS creative_format,
      r.creative_ratio                                                AS creative_ratio,
      r.creative_seconds                                              AS creative_sec,
      UPPER(NULLIF(r.nc_device,''))                                   AS device,
      -- 전환목표(PPT '전환목표 드롭다운'). NC 규칙상 'non' = 전환목표 없음.
      CASE WHEN r.nc_conv IS NULL OR r.nc_conv='' THEN NULL
           WHEN LOWER(r.nc_conv)='non' THEN '없음' ELSE r.nc_conv END AS conv_goal,
      NULLIF(r.nc_goal,'')                                            AS objective,
      NULLIF(r.placement,'')                                          AS placement,
      NULLIF(r.targeting,'')                                          AS targeting,
      SUM(IFNULL(r.impressions,0))                                    AS imp,
      SUM(IFNULL(r.clicks,0))                                         AS clk,
      SUM(IFNULL(r.views,0))                                          AS views,
      SUM(IFNULL(r.spend_krw,0))                                      AS cost,
      SUM(IFNULL(r.conversions,0))                                    AS conv,
      -- 이 행의 광고비로 단가지표(CPM/CPC/CPV)를 계산해도 되는가
      {cost_ok_expr}                                                  AS cost_ok,
      ANY_VALUE(r.spend_basis)                                        AS spend_basis,
      MAX(r.source_loaded_at)                                         AS _src_loaded_at,
      CURRENT_TIMESTAMP()                                             AS _built_at
    FROM {src} r
    {djoin}
    WHERE r.date IS NOT NULL AND IFNULL(r.media,'') != ''
    GROUP BY period, media_name, industry, advertiser, brand, product,
             campaign_name, adgroup_name, creative_name,
             creative_format, creative_ratio, creative_sec, device, conv_goal,
             objective, placement, targeting, cost_ok
    HAVING imp > 0 OR cost > 0
    """).result()
    r = list(c.query(
        f"SELECT COUNT(*) n, COUNT(DISTINCT media_name) m, MAX(period) mx, SUM(cost) sp, "
        f"ROUND(100*SAFE_DIVIDE(SUM(IF(creative_format IS NOT NULL,imp,0)),SUM(imp)),1) f, "
        f"ROUND(100*SAFE_DIVIDE(SUM(IF(creative_ratio IS NOT NULL,imp,0)),SUM(imp)),1) rt, "
        f"ROUND(100*SAFE_DIVIDE(SUM(IF(creative_sec IS NOT NULL,imp,0)),SUM(imp)),1) s "
        f"FROM {tbl}").result())[0]
    print(f"· bm_dplan_creative_monthly: built (행 {r['n']:,} · 매체 {r['m']} · 최신 {r['mx']} · "
          f"₩{int(r['sp']):,} · 노출가중 유형 {r['f']}% 가로세로 {r['rt']}% 초수 {r['s']}%)")
    return True


def build_reach(c):
    """Meta 캠페인 누적 도달을 마트로 복사.

    ⚠️ 서비스 SA(benchmark-app)는 `apac_kr_benchmark` 만 읽는다. 원본 뷰
    `apac_kr_unified.v_meta_campaign_reach` 는 권한이 없어 런타임에 조회가 실패한다
    (로컬은 perf-data-analyst 라 되고 라이브는 안 되는, 가려지기 쉬운 차이다).
    bm_fx 와 같은 경로로 빌더가 읽어 마트에 복사한다.
    """
    view = "v_meta_campaign_reach"
    if not _table_exists(c, "apac_kr_unified", view):
        print(f"· {view} 없음 → 도달 마트 skip")
        return False
    tbl = f"`{PROJECT}.{MART_DS}.bm_meta_campaign_reach`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY market AS
    SELECT campaign_id, campaign_name, market, brand, advertiser_name,
           period_start, period_end, period_days,
           -- 🔑 2026-10-06 DB 추가 — period_days 는 «수집 창» 이고 집행일수가 아니다.
           --   delivery_days_actual 이 insights 에서 «노출이 있던 날» 을 센 진짜 집행일수다.
           --   reach_window_exceeds_delivery=FALSE 는 집행이 창을 벗어나 도달이 «깎인» 쪽이다.
           delivery_days_actual, delivery_first_date, delivery_last_date,
           reach_window_exceeds_delivery, period_source,
           impressions, unique_reach, frequency, spend_krw,
           cost_per_1k_reach_krw, source_snapshot_date,
           CURRENT_TIMESTAMP() AS _built_at
    FROM `{PROJECT}.apac_kr_unified.{view}`
    WHERE unique_reach > 0 AND impressions > 0
    """).result()
    r = list(c.query(f"SELECT COUNT(*) n, COUNT(DISTINCT market) m FROM {tbl}").result())[0]
    print(f"· bm_meta_campaign_reach: built (캠페인 {r['n']:,} · 시장 {r['m']})")
    return True


# ── 이름 규약 (DB f5 합의, 2026-10-06) ─────────────────────────────
#   bm_*        이 빌더가 만든다. CREATE OR REPLACE 로 매번 덮는다.
#   접두 없음    DB 에이전트가 넣는다 (netflix_targeting · netflix_reach_curve ·
#               netflix_reach_grid · v_netflix_reach). 여기서 만들지도, 지우지도 않는다.
#
# ★ 넷플릭스 타게팅 사전을 내가 «복사하지 않는» 이유
#   한때 bm_netflix_targeting 으로 복사했다. 서비스 SA 가 apac_kr_unified 를 못 읽어서였는데,
#   DB 가 apac_kr_benchmark(우리가 OWNER·benchmark-app 이 READER)에 직접 넣어줬다.
#   내가 또 복사하면 «DB 훅이 먼저 도는지» 에 의존하게 된다 — 그 훅은 일일 실행의
#   alerting 뒤(약 03:05 KST)에 돈다. 먼저 안 돌면 조용히 하루 묵은 값을 쓴다.
#   같은 훅 안에서 복사하면 그 틈이 없으므로 DB 쪽에 맡긴다.
#
# ⚠️ 이 빌더는 테이블을 «열거해서» 지우지 않는다(이름을 지정한 DROP 만 한다).
#   열거 삭제를 넣으면 DB 가 넣은 netflix_* 가 모르는 테이블로 보여 사라진다.


def build_metric_caveats(c):
    """사전 경고를 마트로 복사 — 서비스 SA 는 apac_kr_ops 를 못 읽는다.

    ★ 왜 필요한가 — device·video 는 사전이 do_not_use 로 막아 rev=0 이 되고 커버리지 게이트가
      자동으로 잡는다. 그런데 age·gender 는 caution 이라 막지 않고, 커버리지가 8.4%/7.7% 로
      임계(10%)와 1.6%p 차이다. 데이터가 조금만 움직이면 게이트를 통과하고
      «23% 낮은 ROAS» 가 조용히 화면에 나간다(DB 실측: age/gender ROAS 189.7% vs 기준 245.6%).
      게이트가 «우연히» 잡는 것에 기대지 않고, 경고 자체를 화면까지 가져간다.
    """
    if not _table_exists(c, "apac_kr_ops", "dictionary_column_notes"):
        print("· dictionary_column_notes 없음 → 지표 경고 마트 skip")
        return False
    tbl = f"`{PROJECT}.{MART_DS}.bm_metric_caveats`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} AS
    SELECT mart_name, column_name, severity, note,
           CAST(use_instead AS STRING) AS use_instead,
           CURRENT_TIMESTAMP() AS _built_at
    FROM `{PROJECT}.apac_kr_ops.dictionary_column_notes`
    WHERE severity IN ('do_not_use','caution')
    """).result()
    n = list(c.query(f"SELECT COUNT(*) n FROM {tbl}").result())[0]["n"]
    print(f"· bm_metric_caveats: built ({n}건 — 서비스가 화면 경고에 사용)")
    return True


def build_fx(c):
    """최신 환율을 마트로 복사 — 서비스 SA(benchmark-app)는 raw 미접근이므로 마트 경유.
    소스 apac_kr_raw.fx_rates_daily(ECB). bm_fx = 최신일 통화별 to_krw."""
    tbl = f"`{PROJECT}.{MART_DS}.bm_fx`"
    src = f"`{PROJECT}.apac_kr_raw.fx_rates_daily`"
    if not _table_exists(c, "apac_kr_raw", "fx_rates_daily"):
        print("· fx_rates_daily 없음 → 환율 마트 skip")
        return False
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} AS
    SELECT currency, to_krw, to_usd, date AS asof
    FROM {src} WHERE date=(SELECT MAX(date) FROM {src}) AND to_krw IS NOT NULL
    """).result()
    print("· bm_fx: built (최신 환율 복사)")
    return True


def check_upstream_freshness(c):
    """업스트림(google_ads DTS)이 이 빌드보다 늦게 도착했는지 확인해 로그로 남긴다.

    DTS 는 최근 2~3일을 재진술(restate)하므로, 빌드가 DTS 보다 먼저 돌면 그 차이만큼
    google_ads 가 낮게 잡힌다. 예약 빌드(20:00 UTC)는 DTS(≈04:46 UTC)보다 뒤라 안전하지만,
    수동 재빌드를 02~05시 UTC 에 돌리면 경합이 난다 — 그때 조용히 넘어가지 않게 한다.
    막지는 않는다(빌드 실패가 더 나쁘다). 알리기만 한다.
    """
    try:
        rows = list(c.query(f"""
            SELECT COUNTIF(TIMESTAMP_MILLIS(last_modified_time) > mart_ts) AS stale,
                   COUNT(*) AS n,
                   FORMAT_TIMESTAMP('%Y-%m-%d %H:%M UTC', MAX(TIMESTAMP_MILLIS(last_modified_time))) AS newest
            FROM `{PROJECT}.apac_kr_raw.__TABLES__`,
                 (SELECT TIMESTAMP_MILLIS(last_modified_time) mart_ts
                  FROM `{PROJECT}.{MART_DS}.__TABLES__` WHERE table_id='bm_campaign_monthly')
            WHERE table_id LIKE 'p_ads_CampaignBasicStats%'
        """).result())
        if not rows or not rows[0]["n"]:
            return
        r = rows[0]
        if r["stale"]:
            print(f"· [주의] google_ads DTS {r['stale']}/{r['n']}개가 마트 빌드보다 늦게 도착했습니다"
                  f"(최신 {r['newest']}). 이번 빌드는 그만큼 낮게 잡혔을 수 있습니다 — 재빌드를 권합니다.")
        else:
            print(f"· 업스트림 신선도 OK (google_ads DTS {r['n']}개 전부 빌드 이전, 최신 {r['newest']})")
    except Exception as e:
        print(f"· [경고] 업스트림 신선도 확인 스킵: {str(e)[:120]}")


def _brand_expr(c, view, alias="u"):
    """업종 폴백이 먼저 볼 «광고주/브랜드» 텍스트. 없으면 빈 문자열 → 폴백은 캠페인명만 본다."""
    parts = [f"IFNULL({alias}.{col},'')" for col in ("advertiser_name", "brand")
             if _has_col(c, view, col)]
    if not parts:
        return ""
    return "LOWER(CONCAT(" + ", ' ', ".join(parts) + "))"


def report_fallback_usage(c):
    """정규식 폴백이 실제로 도는 행이 있는지 알린다.

    ★ 왜 폴백을 «고치지» 않고 «알리기» 만 하는가 —
      DB 지적: "정규식을 사전으로 옮기자는 게 요지인데 옮기면서 또 정규식을 만들면
      3개월 뒤 같은 대화를 한다." 폴백에 손대면 유지보수 대상이 생기고, 그러면
      사전이 늦어도 아프지 않아서 결국 사전이 안 온다.
      대신 폴백이 «도는 순간» 을 드러낸다 — 그게 사전에 빠진 광고주가 생겼다는 신호다.

    ⚠️ 내 폴백 토큰에도 알려진 함정이 있다(2026-09-22 DB 가 양쪽에서 겪은 것):
      ' air' · ' game' · ' 앱'  앞뒤 공백을 요구해 붙여쓴 이름을 놓친다(YouTube·Awareness 류)
      'kia' · 'app' · 'hmb'     짧은 약어라 부분매칭 오탐이 난다(Nokia·happy 류)
      사전이 덮는 동안은 드러나지 않지만, 폴백이 도는 행이 생기면 이 함정이 같이 산다.
    """
    tbl = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
    try:
        r = list(c.query(f"""
            SELECT COUNTIF(industry_source='regex_fallback') ind_fb,
                   COUNTIF(objective_source='regex_fallback') obj_fb,
                   ROUND(SUM(IF(industry_source='regex_fallback', cost, 0))) ind_cost,
                   ROUND(SUM(IF(objective_source='regex_fallback', cost, 0))) obj_cost,
                   COUNT(*) n
            FROM {tbl}""").result())[0]
    except Exception as e:
        print(f"· [경고] 폴백 사용량 확인 스킵: {str(e)[:100]}")
        return
    if r["ind_fb"] or r["obj_fb"]:
        print(f"· [주의] 정규식 폴백이 돌고 있습니다 — 사전에 빠진 대상이 있습니다:")
        if r["ind_fb"]:
            print(f"    업종 {r['ind_fb']:,}행 (₩{int(r['ind_cost'] or 0):,}) "
                  f"→ apac_kr_ops.advertiser_industry 에 추가 요청 대상")
        if r["obj_fb"]:
            print(f"    캠페인목표 {r['obj_fb']:,}행 (₩{int(r['obj_cost'] or 0):,}) "
                  f"→ v_perf_unified.objective_layer 미분류")
        print("    ⚠️ 폴백 토큰에는 구분자·붙여쓰기 함정이 있습니다(코드 주석 참조).")
    else:
        print("· 정규식 폴백 미사용 — 업종·목표 전부 사전에서 왔습니다")


def build():
    c = _client()
    ensure_dataset(c)
    build_campaign(c)
    build_fx(c)
    for _dim, (_view, _col) in SEGMENTS.items():   # device/age/gender — 뷰 있으면 자동 빌드
        build_segment(c, _dim, _view, _col)
    build_video(c)                                 # 영상(V) — 뷰 있으면 자동 빌드
    build_dplan_creative(c)                        # 디플랜 NAS 소재 grain — raw 있으면 자동 빌드
    build_reach(c)                                 # Meta 캠페인 누적 도달 — 서비스 SA 접근용 복사
    build_metric_caveats(c)                        # 사전 경고 — 서비스가 화면에 띄울 수 있게 복사
    try:                                            # 값 없는 지표 자동 감지 → DB 에이전트 요청 큐 발행
        import gaps
        gaps.request_gaps(c)
        gaps.ask_db(c)      # 판단이 필요한 질의 발행 + 도착한 회신 로그 출력
    except Exception as _e:
        print(f"· [경고] 데이터-갭 요청 스킵: {str(_e)[:120]}")
    check_upstream_freshness(c)
    report_fallback_usage(c)
    n = list(c.query(
        f"SELECT COUNT(*) n, COUNT(DISTINCT campaign_id) camps, COUNT(DISTINCT media) media, "
        f"COUNT(DISTINCT market) markets, COUNT(DISTINCT objective) objs "
        f"FROM `{PROJECT}.{MART_DS}.bm_campaign_monthly`").result())[0]
    print(f"DONE. rows={n['n']} campaigns={n['camps']} media={n['media']} "
          f"markets={n['markets']} objectives={n['objs']}")


def check():
    c = _client()
    for r in c.query(
        f"SELECT media, objective, COUNT(DISTINCT campaign_id) camps "
        f"FROM `{PROJECT}.{MART_DS}.bm_campaign_monthly` GROUP BY 1,2 ORDER BY 1,3 DESC").result():
        print(dict(r))


if __name__ == "__main__":
    check() if "--check" in sys.argv else build()
