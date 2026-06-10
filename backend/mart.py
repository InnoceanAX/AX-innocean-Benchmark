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

# 단일 업스트림 = DB팀 공용 통합 계약 뷰 (읽기 전용, dedup·통화정규화·제외플래그 포함)
SOURCE = f"`{PROJECT}.apac_kr_unified.v_perf_unified`"
# v_perf_unified.platform → 프론트 매체 탭(G/M/N/K). dv360/tiktok/sa360 은 UI 탭 없음 → 제외.
PLATFORM_TO_MEDIA = {"google_ads": "G", "meta": "M"}


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


def build_fact(c):
    # 업종은 advertiser_name + campaign_name 텍스트로 추정 (v_perf_unified 엔 업종 필드 없음)
    ind = industry_case_sql("CONCAT(IFNULL(advertiser_name,''),' ',IFNULL(campaign_name,''))")
    # platform → media 매핑 CASE
    whens = " ".join([f"WHEN '{p}' THEN '{m}'" for p, m in PLATFORM_TO_MEDIA.items()])
    media_case = f"CASE platform {whens} ELSE NULL END"
    plats = ",".join([f"'{p}'" for p in PLATFORM_TO_MEDIA])
    tbl_id = f"`{PROJECT}.{MART_DS}.bm_fact_monthly`"
    sql = f"""
    CREATE OR REPLACE TABLE {tbl_id}
    CLUSTER BY media, industry AS
    SELECT
      FORMAT_DATE('%Y-%m', date) AS period,
      {media_case} AS media,
      platform,
      {ind} AS industry,
      SUM(impressions) AS impressions,
      SUM(clicks) AS clicks,
      SUM(spend_krw) AS cost,            -- 정규화 KRW (프론트 ₩ 표시와 일치)
      SUM(conversions) AS conversions,
      CURRENT_TIMESTAMP() AS _built_at
    FROM {SOURCE}
    WHERE date IS NOT NULL
      AND NOT IFNULL(is_excluded, FALSE)   -- 제외 플래그 반영(F2)
      AND platform IN ({plats})
    GROUP BY period, media, platform, industry
    HAVING impressions > 0
    """
    c.query(sql).result()
    print("bm_fact_monthly: rebuilt from v_perf_unified")


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
