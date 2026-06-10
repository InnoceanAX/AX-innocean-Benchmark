"""
벤치마크 전용 데이터 마트 빌더.

원칙:
- 로우데이터(apac_kr_raw)·공용 통합층(apac_kr_unified)은 **읽기 전용**(SELECT only).
- 쓰기는 오직 벤치마크 전용 데이터셋 `apac_kr_benchmark` 에만.
- 벤치마크 백엔드(bq.py)는 이 마트만 소비한다.

데이터 현실: 스펜드 ~99%가 현대·기아(자동차). '업종' 다양성은 없음 →
  벤치마크 1차 축 = **권역(market)**, 2차 = **브랜드(brand)**. (둘 다 v_perf_unified에 존재, 0% null)
  4분위 벤치마크 = 권역×매체별 캠페인 KPI 분포(평균/중앙/상위25%/상위10%) = FEATURES.md 의도.

산출물 (dataset: apac_kr_benchmark):
- bm_fact_monthly : 월 × 매체 × 권역 × 브랜드 집계 (표/차트/추세)
- bm_benchmark    : 권역 × 매체 4분위(CPM/CPC/CTR 평균·중앙·상위25%·상위10% + 캠페인수)

UPSTREAM: apac_kr_unified.v_perf_unified (dedup·spend_krw·brand·market·is_excluded)
실행: python mart.py [--check]
"""
import os
import sys
from google.cloud import bigquery

PROJECT = "innocean-perf-apac-kr"
MART_DS = "apac_kr_benchmark"
LOCATION = "asia-northeast3"
SOURCE = f"`{PROJECT}.apac_kr_unified.v_perf_unified`"
# v_perf_unified.platform → 프론트 매체 탭. 네이버(N)·카카오(K)는 수집되면 추가.
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


# 매체/플랫폼 매핑 SQL 조각
def _media_case():
    whens = " ".join([f"WHEN '{p}' THEN '{m}'" for p, m in PLATFORM_TO_MEDIA.items()])
    return f"CASE platform {whens} ELSE NULL END"


def _plats():
    return ",".join([f"'{p}'" for p in PLATFORM_TO_MEDIA])


def build_fact(c):
    """월 × 매체 × 권역 × 브랜드 집계 (표/추세/차트용)."""
    tbl = f"`{PROJECT}.{MART_DS}.bm_fact_monthly`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()   # 클러스터링 변경 허용
    sql = f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, market AS
    SELECT
      FORMAT_DATE('%Y-%m', date) AS period,
      {_media_case()} AS media,
      market,
      brand,
      SUM(impressions) AS impressions,
      SUM(clicks) AS clicks,
      SUM(spend_krw) AS cost,
      SUM(conversions) AS conversions,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {SOURCE}
    WHERE date IS NOT NULL AND NOT IFNULL(is_excluded, FALSE)
      AND platform IN ({_plats()}) AND market IS NOT NULL AND market != ''
    GROUP BY period, media, market, brand
    HAVING impressions > 0
    """
    c.query(sql).result()
    print("bm_fact_monthly: rebuilt")


def build_benchmark(c):
    """권역 × 매체 4분위 벤치마크 — 캠페인 단위 KPI 분포.
    상위(top) = '더 좋은' 방향: CPM/CPC 낮을수록 좋음 → 낮은 분위, CTR 높을수록 좋음 → 높은 분위."""
    tbl = f"`{PROJECT}.{MART_DS}.bm_benchmark`"
    c.query(f"DROP TABLE IF EXISTS {tbl}").result()
    sql = f"""
    CREATE OR REPLACE TABLE {tbl} CLUSTER BY media, market AS
    WITH camp AS (
      SELECT {_media_case()} AS media, market, brand, campaign_id,
        SUM(impressions) imp, SUM(clicks) clk, SUM(spend_krw) cost, SUM(conversions) conv
      FROM {SOURCE}
      WHERE NOT IFNULL(is_excluded, FALSE) AND platform IN ({_plats()})
        AND market IS NOT NULL AND market != '' AND date IS NOT NULL
      GROUP BY media, market, brand, campaign_id
      HAVING imp >= 1000           -- 노이즈 캠페인 제외(최소 노출)
    ),
    kpi AS (
      SELECT media, market,
        SAFE_DIVIDE(cost, imp) * 1000 AS cpm,
        SAFE_DIVIDE(cost, clk)        AS cpc,
        SAFE_DIVIDE(clk, imp) * 100   AS ctr
      FROM camp WHERE imp > 0 AND clk > 0
    )
    SELECT media, market, COUNT(*) AS n_campaigns,
      -- CPM (낮을수록 좋음)
      ROUND(AVG(cpm),1) cpm_avg,
      ROUND(APPROX_QUANTILES(cpm,100)[OFFSET(50)],1) cpm_median,
      ROUND(APPROX_QUANTILES(cpm,100)[OFFSET(25)],1) cpm_top25,
      ROUND(APPROX_QUANTILES(cpm,100)[OFFSET(10)],1) cpm_top10,
      -- CPC (낮을수록 좋음)
      ROUND(AVG(cpc),1) cpc_avg,
      ROUND(APPROX_QUANTILES(cpc,100)[OFFSET(50)],1) cpc_median,
      ROUND(APPROX_QUANTILES(cpc,100)[OFFSET(25)],1) cpc_top25,
      ROUND(APPROX_QUANTILES(cpc,100)[OFFSET(10)],1) cpc_top10,
      -- CTR (높을수록 좋음 → 상위는 높은 분위)
      ROUND(AVG(ctr),2) ctr_avg,
      ROUND(APPROX_QUANTILES(ctr,100)[OFFSET(50)],2) ctr_median,
      ROUND(APPROX_QUANTILES(ctr,100)[OFFSET(75)],2) ctr_top25,
      ROUND(APPROX_QUANTILES(ctr,100)[OFFSET(90)],2) ctr_top10,
      CURRENT_TIMESTAMP() AS _built_at
    FROM kpi
    GROUP BY media, market
    HAVING n_campaigns >= 3        -- 표본 3개 미만 권역 제외
    """
    c.query(sql).result()
    print("bm_benchmark: rebuilt")


def build():
    c = _client()
    ensure_dataset(c)
    build_fact(c)
    build_benchmark(c)
    n = list(c.query(
        f"SELECT COUNT(*) n, COUNT(DISTINCT media) m, COUNT(DISTINCT market) mk "
        f"FROM `{PROJECT}.{MART_DS}.bm_fact_monthly`").result())[0]
    b = list(c.query(
        f"SELECT COUNT(*) n FROM `{PROJECT}.{MART_DS}.bm_benchmark`").result())[0]
    print(f"DONE. fact rows={n['n']} media={n['m']} markets={n['mk']} | benchmark rows={b['n']}")


def check():
    c = _client()
    for r in c.query(
        f"SELECT media, COUNT(*) n, COUNT(DISTINCT market) markets "
        f"FROM `{PROJECT}.{MART_DS}.bm_benchmark` GROUP BY media ORDER BY media").result():
        print(dict(r))


if __name__ == "__main__":
    check() if "--check" in sys.argv else build()
