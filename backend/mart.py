"""
벤치마크 전용 데이터 마트 빌더.

원칙:
- 로우데이터(apac_kr_raw)·공용 통합층(apac_kr_unified)은 **읽기 전용**(SELECT only).
- 쓰기는 오직 벤치마크 전용 데이터셋 `apac_kr_benchmark` 에만.
- 벤치마크 백엔드(bq.py)는 이 마트만 소비한다(로우 직접 접근 금지).

산출물 (dataset: apac_kr_benchmark):
- bm_advertiser_industry : 업종 매핑 시드 테이블 (F1 결정이 채울 자리)
- bm_fact_monthly        : 월(period) × 매체(media) × 업종(industry) 사전집계 fact

UPSTREAM: 현재는 인터im 소스. DB의 `v_perf_unified` 제공 시 여기만 교체.
실행: python mart.py            (마트 생성/갱신)
      python mart.py --check    (행수 확인)
"""
import os
import sys
from google.cloud import bigquery
from industry_map import industry_case_sql, _KEYWORD_RULES, _FRONT_SET

PROJECT = "innocean-perf-apac-kr"
MART_DS = "apac_kr_benchmark"
LOCATION = "asia-northeast3"

# 키 로딩 (로컬). Cloud Run에선 ADC.
for _k in [os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
           os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "setup", "innocean-perf-apac-kr-40e02bc0d0d8.json"))]:
    if _k and os.path.exists(_k):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _k
        break

# 매체(UI 탭 G/M/N/K) → 업스트림 소스 정의 (읽기 전용)
UPSTREAM = {
    "G": {  # Google Ads
        "platform": "google_ads",
        "table": f"`{PROJECT}.apac_kr_unified.v_unified_google_ads`",
        "date": "date", "imp": "impressions", "clk": "clicks", "cost": "cost",
        "conv": "conversions", "text": "campaign_name",
    },
    "M": {  # Meta
        "platform": "meta",
        "table": f"`{PROJECT}.apac_kr_raw.meta_insights_daily`",
        "date": "_date", "imp": "impressions", "clk": "clicks", "cost": "spend",
        "conv": "conversions",
        "text": "CONCAT(IFNULL(account_name,''),' ',IFNULL(campaign_name,''),' ',IFNULL(_brand,''))",
    },
    # N(네이버)·K(카카오): 데이터 수집 전 → 마트 미생성
}


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


def build_mapping_table(c):
    """업종 매핑 시드 테이블 (감사/확장용). 현재는 규칙을 행으로 노출."""
    rows = []
    for industry, kws in _KEYWORD_RULES:
        target = industry if industry in _FRONT_SET else "기타"
        for kw in kws:
            rows.append({"keyword": kw.lower(), "industry": target})
    tbl_id = f"{PROJECT}.{MART_DS}.bm_advertiser_industry"
    schema = [
        bigquery.SchemaField("keyword", "STRING"),
        bigquery.SchemaField("industry", "STRING"),
    ]
    t = bigquery.Table(tbl_id, schema=schema)
    c.create_table(t, exists_ok=True)
    c.query(f"TRUNCATE TABLE `{tbl_id}`").result()
    c.insert_rows_json(tbl_id, rows)
    print(f"bm_advertiser_industry: {len(rows)} keyword rules")


def _media_select(media, src):
    ind = industry_case_sql(src["text"])
    conv = src.get("conv")
    # 일부 소스(Meta)는 지표가 STRING 으로 적재됨 → SAFE_CAST 로 안전 변환
    def num(col):
        return f"SAFE_CAST({col} AS FLOAT64)"
    conv_expr = f"SUM({num(conv)})" if conv else "0"
    return f"""
    SELECT
      FORMAT_DATE('%Y-%m', {src['date']}) AS period,
      '{media}' AS media,
      '{src['platform']}' AS platform,
      {ind} AS industry,
      SUM({num(src['imp'])}) AS impressions,
      SUM({num(src['clk'])}) AS clicks,
      SUM({num(src['cost'])}) AS cost,
      {conv_expr} AS conversions
    FROM {src['table']}
    WHERE {src['date']} IS NOT NULL
    GROUP BY period, media, platform, industry
    """


def build_fact(c):
    selects = [_media_select(m, s) for m, s in UPSTREAM.items()]
    union = "\nUNION ALL\n".join(selects)
    tbl_id = f"`{PROJECT}.{MART_DS}.bm_fact_monthly`"
    sql = f"""
    CREATE OR REPLACE TABLE {tbl_id}
    CLUSTER BY media, industry AS
    WITH unioned AS (
    {union}
    )
    SELECT *, CURRENT_TIMESTAMP() AS _built_at
    FROM unioned
    WHERE impressions > 0
    """
    c.query(sql).result()
    print("bm_fact_monthly: rebuilt")


def build():
    c = _client()
    ensure_dataset(c)
    build_mapping_table(c)
    build_fact(c)
    n = list(c.query(
        f"SELECT COUNT(*) n, COUNT(DISTINCT media) m, COUNT(DISTINCT industry) i, "
        f"MIN(period) mn, MAX(period) mx FROM `{PROJECT}.{MART_DS}.bm_fact_monthly`"
    ).result())[0]
    print(f"DONE. fact rows={n['n']} media={n['m']} industries={n['i']} {n['mn']}~{n['mx']}")


def check():
    c = _client()
    for r in c.query(
        f"SELECT media, COUNT(*) n, COUNT(DISTINCT industry) inds, "
        f"SUM(impressions) imp FROM `{PROJECT}.{MART_DS}.bm_fact_monthly` "
        f"GROUP BY media ORDER BY media"
    ).result():
        print(dict(r))


if __name__ == "__main__":
    check() if "--check" in sys.argv else build()
