"""
벤치마크 전용 데이터 마트 빌더 (다차원).

원칙:
- 로우데이터·공용 통합층은 읽기 전용(SELECT only). 쓰기는 `apac_kr_benchmark` 에만.
- 벤치마크 백엔드(bq.py)는 이 마트만 소비.

산출물:
- bm_campaign_monthly : 캠페인 × 월 grain. 차원(매체·국가·업종·캠페인목표·브랜드·대행사) + 지표.
  → 백엔드가 임의의 기준차원 × 필터 조합으로 4분위 벤치마크를 동적 계산.

데이터 현실: 스펜드 ~99% 현대·기아 자동차. 업종 다양성은 약하나, 국가/캠페인목표/브랜드는 풍부.
UPSTREAM: apac_kr_unified.v_perf_unified
실행: python mart.py [--check]
"""
import os
import sys
from google.cloud import bigquery
from industry_map import industry_case_sql, objective_case_sql

PROJECT = "innocean-perf-apac-kr"
MART_DS = "apac_kr_benchmark"
LOCATION = "asia-northeast3"
SOURCE = f"`{PROJECT}.apac_kr_unified.v_perf_unified`"
PLATFORM_TO_MEDIA = {"google_ads": "G", "meta": "M", "dv360": "D", "tiktok": "T"}

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


def _media_case():
    whens = " ".join([f"WHEN '{p}' THEN '{m}'" for p, m in PLATFORM_TO_MEDIA.items()])
    return f"CASE platform {whens} ELSE NULL END"


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


def build_campaign(c):
    """캠페인 × 월 grain 다차원 테이블. google 캠페인명은 raw에서 보강(P0 자동수정)."""
    # 보강된 캠페인명 텍스트 (google: raw, 그 외: v_perf_unified)
    name_expr = "COALESCE(NULLIF(u.campaign_name,''), g.nm, '')"
    ind = industry_case_sql(f"CONCAT(IFNULL(u.advertiser_name,''),' ',{name_expr})")
    obj = objective_case_sql(name_expr)
    gmap = _gname_union(c)
    join = f"LEFT JOIN ({gmap}) g ON CAST(u.campaign_id AS STRING)=g.cid" if gmap else "LEFT JOIN (SELECT '' cid, '' nm) g ON FALSE"
    tbl = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    sql = f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, market AS
    SELECT
      FORMAT_DATE('%Y-%m', u.date) AS period,
      {_media_case()} AS media,
      u.market AS market,
      {ind} AS industry,
      {obj} AS objective,
      u.brand AS brand,
      IFNULL(NULLIF(u.agency,''),'(미상)') AS agency,
      u.campaign_id AS campaign_id,
      SUM(u.impressions) AS imp,
      SUM(u.clicks) AS clk,
      SUM(u.spend_krw) AS cost,
      SUM(u.conversions) AS conv,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {SOURCE} u
    {join}
    WHERE u.date IS NOT NULL AND NOT IFNULL(u.is_excluded, FALSE)
      AND u.platform IN ({_plats()}) AND u.market IS NOT NULL AND u.market != ''
    GROUP BY period, media, market, industry, objective, brand, agency, campaign_id
    HAVING imp > 0
    """
    c.query(sql).result()
    print("bm_campaign_monthly: rebuilt (google 캠페인명 raw 보강 포함)")


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
    ind = industry_case_sql(name_expr)
    obj = objective_case_sql(name_expr)
    gmap = _gname_union(c)
    join = (f"LEFT JOIN ({gmap}) g ON CAST(u.campaign_id AS STRING)=g.cid"
            if gmap else "LEFT JOIN (SELECT '' cid,'' nm) g ON FALSE")
    tbl = f"`{PROJECT}.{MART_DS}.bm_{dim}_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    c.query(f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, {dim} AS
    SELECT FORMAT_DATE('%Y-%m', u.date) AS period, {_media_case()} AS media,
      u.market AS market, {ind} AS industry, {obj} AS objective, u.brand AS brand,
      UPPER(CAST(u.{col} AS STRING)) AS {dim}, u.campaign_id AS campaign_id,
      SUM(u.impressions) imp, SUM(u.clicks) clk, SUM(u.spend_krw) cost, SUM(u.conversions) conv,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {dsrc} u
    {join}
    WHERE u.date IS NOT NULL AND NOT IFNULL(u.is_excluded,FALSE)
      AND u.platform IN ({_plats()}) AND u.market IS NOT NULL AND u.market!='' AND u.{col} IS NOT NULL
    GROUP BY period, media, market, industry, objective, brand, {dim}, campaign_id
    HAVING imp > 0
    """).result()
    print(f"· bm_{dim}_monthly: built ({dim} 차원 활성)")
    return True


def build():
    c = _client()
    ensure_dataset(c)
    build_campaign(c)
    for _dim, (_view, _col) in SEGMENTS.items():   # device/age/gender — 뷰 있으면 자동 빌드
        build_segment(c, _dim, _view, _col)
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
