# -*- coding: utf-8 -*-
"""자동 데이터-갭 요청기 — 벤치마크가 '값이 비어있는 지표'를 감지해 DB 에이전트 요청 큐로 발행.

요청자(requested_by) 명시로 **에이전트 간 요청 충돌/중복 방지**:
  dedupe_key = requested_by|platform|metric  (예: benchmark|meta|revenue)
  → 같은 에이전트의 같은 요청은 한 행으로 유지(중복 누적 X), 타 에이전트(report 등)의 요청은 별개 행.
  → DB 에이전트는 (platform,metric)로 실제 작업을 묶고, requested_by로 '누가 필요로 하는지' 추적.

큐: `apac_kr_ops.agent_data_requests` (DB 에이전트가 모니터링하는 ops 레이어).
흐름: 벤치마크 감지→open 발행 → DB가 연동플랫폼·수집데이터 검토해 채움→dictionary_version↑ → 벤치마크 자동반영,
      해소된 갭은 다음 스캔에서 status='fulfilled' 자동 처리. DB가 'unavailable'로 닫은 건 재요청 안 함(결정 존중).
실행: mart.build() 끝에서 매일 자동 호출 + `python gaps.py` 수동.
"""
from mart import _client, PROJECT, MART_DS

AGENT_ID = "benchmark"
REQ_TBL = f"`{PROJECT}.apac_kr_ops.agent_data_requests`"
MART = f"`{PROJECT}.{MART_DS}.bm_campaign_monthly`"
MEDIA = {"google_ads": "G", "meta": "M", "dv360": "D", "tiktok": "T", "kakao": "K"}
COVER_MIN = 0.02   # 커버리지 2% 미만이면 '값 없음(갭)'으로 간주

# 벤치마크가 원하는 지표 레지스트리 — (platform, metric, label, mart_col). col=None=마트에 컬럼 자체가 없음(미수집).
WANTED = [
    ("meta",    "conversions", "전환수·CVR (전환 추적)",        "conv"),
    ("meta",    "revenue",     "전환가치·ROAS",                 "rev"),
    ("meta",    "video_6s",    "6초 조회 (영상)",               None),
    ("meta",    "photo_view",  "사진 조회",                     None),
    ("kakao",   "conversions", "전환수·CVR",                    "conv"),
    ("kakao",   "revenue",     "전환가치·ROAS",                 "rev"),
    ("kakao",   "video",       "영상지표(조회/VTR/CPV)",        "vimp"),
    ("tiktok",  "revenue",     "전환가치·ROAS",                 "rev"),
    ("dv360",   "video",       "영상지표(조회/VTR/CPV)",        "vimp"),
    ("naver",   "all",         "네이버 전체(노출·클릭·비용) 수집", None),
]

DDL = f"""CREATE TABLE IF NOT EXISTS {REQ_TBL} (
  dedupe_key STRING, requested_by STRING, platform STRING, metric STRING, label STRING,
  status STRING, coverage FLOAT64, detail STRING, db_response STRING,
  created_at TIMESTAMP, updated_at TIMESTAMP, last_seen_at TIMESTAMP
)"""


def _coverage(c):
    cols = sorted({w[3] for w in WANTED if w[3]})
    sel = ", ".join(f"SAFE_DIVIDE(COUNTIF({col}>0),COUNT(*)) cov_{col}" for col in cols)
    out = {}
    for r in c.query(f"SELECT media, {sel} FROM {MART} GROUP BY media").result():
        out[r["media"]] = {col: (r[f"cov_{col}"] or 0) for col in cols}
    return out


def request_gaps(c=None):
    """갭 스캔 후 요청 큐를 멱등 갱신(MERGE). open 갭 수 반환."""
    c = c or _client()
    try:
        c.query(DDL).result()
        cov = _coverage(c)
        present = set(cov.keys())
        recs = []
        for platform, metric, label, col in WANTED:
            media = MEDIA.get(platform)
            if platform == "naver" or media not in present or col is None:
                gap, coverage = True, 0.0          # 플랫폼/컬럼 미수집
            else:
                coverage = float(cov.get(media, {}).get(col, 0.0))
                gap = coverage < COVER_MIN
            recs.append((f"{AGENT_ID}|{platform}|{metric}", platform, metric, label, gap, round(coverage, 4)))
        using = " UNION ALL ".join(
            f"SELECT '{dk}' dedupe_key,'{pf}' platform,'{mt}' metric,'{lb}' label,"
            f"{str(gp).upper()} gap,{cv} coverage"
            for (dk, pf, mt, lb, gp, cv) in recs)
        c.query(f"""
        MERGE {REQ_TBL} T USING ({using}) S ON T.dedupe_key=S.dedupe_key
        WHEN MATCHED AND S.gap AND T.status IN ('open','ack') THEN UPDATE SET
          updated_at=CURRENT_TIMESTAMP(), last_seen_at=CURRENT_TIMESTAMP(), coverage=S.coverage
        WHEN MATCHED AND NOT S.gap AND T.status IN ('open','ack') THEN UPDATE SET
          status='fulfilled', db_response='데이터 확인됨(자동 해소)', updated_at=CURRENT_TIMESTAMP(), coverage=S.coverage
        WHEN NOT MATCHED AND S.gap THEN INSERT
          (dedupe_key, requested_by, platform, metric, label, status, coverage, detail, created_at, updated_at, last_seen_at)
          VALUES (S.dedupe_key, '{AGENT_ID}', S.platform, S.metric, S.label, 'open', S.coverage,
            '벤치마크 자동감지: 해당 플랫폼에서 이 지표 값이 비어있음. 연동 플랫폼·수집데이터 검토 후 채워주세요.',
            CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """).result()
        n_open = sum(1 for r in recs if r[4])
        print(f"· agent_data_requests MERGE 완료 — 현재 gap {n_open}건 (requested_by={AGENT_ID})")
        return n_open
    except Exception as e:   # 큐 갱신 실패가 마트 빌드를 깨지 않도록
        print(f"· [경고] 데이터-갭 요청 스킵: {str(e)[:160]}")
        return -1


if __name__ == "__main__":
    request_gaps()
