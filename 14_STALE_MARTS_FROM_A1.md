# `bm_benchmark` · `bm_fact_monthly` 가 3개월째 안 만들어진다 — A1 → Benchmark

> 작성: 2026-09-08 · 보낸 곳: `솔루션/A1` (AI Workspace)
> 경위: A1 이 Benchmark 산출물을 만들려고 마트를 쓰려다 발견 → DB 에이전트가 원인 규명 → **Benchmark 소관**으로 이관
> **아무것도 고치지 않았습니다.** 확인만 하고 넘깁니다.

---

## 0. 한 줄

`backend/mart.py` 에 **`bm_benchmark`·`bm_fact_monthly` 를 만드는 코드가 없습니다.**
«조용한 실패» 가 아니라 **«빌드 대상에서 빠진»** 상태입니다.

---

## 1. 증상

| 테이블 | 마지막 빌드 | 최신 기간 | 행수 |
|---|---|---|---|
| `bm_campaign_monthly` | **2026-09-06** ✅ | 2026-09 | 27,806 |
| `bm_age_monthly` · `bm_device_monthly` · `bm_gender_monthly` · `bm_video_monthly` | **2026-09-06** ✅ | 2026-09 | 정상 |
| `bm_fx` | 2026-09-06 ✅ | — | 11 |
| **`bm_benchmark`** | 🔴 **2026-06-11** | — | 60 |
| **`bm_fact_monthly`** | 🔴 **2026-06-11** | **2026-06** | 1,122 |

`benchmark-mart-builder` Job 은 **매일 20:00 UTC 정상 성공**합니다
(2026-09-06 succeeded, rows=27,772). Job 이 죽은 게 아닙니다.

## 2. 원인 — 소스에 없습니다

    solutions/BenchMark/backend/mart.py   (15,161 bytes · 2026-06-19)

    grep -c "bm_benchmark|bm_fact_monthly" backend/mart.py   →  **0**

`mart.py` 가 실제로 만드는 것:

    build_campaign(c)   → bm_campaign_monthly     ✅
    build_segment(c, …) → bm_age/device/gender/video_monthly  ✅
    (fx)                → bm_fx                   ✅

**`bm_benchmark`·`bm_fact_monthly` 를 만드는 함수가 없습니다.**
Job 이미지는 `innocean-benchmark:backend-v50` / `command=python mart.py` 입니다.

즉 **2026-06-11 이후 어느 시점에 두 테이블 생성 코드가 빠졌고, 그 뒤로 옛 데이터가 그대로 남아 있는** 것입니다.

---

## 3. 왜 지금 알리나 — **«낡은 채로 살아 있는» 것이 없는 것보다 위험합니다**

A1 이 벤치마크를 만들려고 `bm_benchmark` 를 열었습니다.
**60행이 멀쩡히 조회되고 CPM·CPC·CTR 분위수가 다 들어 있었습니다.**
`_built_at` 을 안 봤으면 **3개월 낡은 수치로 «지금 우리가 어디쯤인가» 를 말할 뻔했습니다.**

CPM·CPC 는 계절성이 커서 6월 값으로 9월을 판단하면 틀립니다.

> DB 에이전트가 이 위험을 알고 **`bm_benchmark` 를 사전(`dictionary_marts`)에 일부러 등재하지 않았습니다** —
> *"등재 = 사용허가 신호이므로"*. 대신 `bm_campaign_monthly` 를 등재했습니다.
> **좋은 판단이라고 봅니다.**

---

## 4. A1 은 이렇게 우회했습니다 (참고)

`bm_benchmark` 를 기다리지 않고 **`bm_campaign_monthly` 로 분위수를 직접 계산**합니다.
그게 원본이고 최신이라 오히려 나았습니다.

    APPROX_QUANTILES(cpm, 100)[OFFSET(50)]  → 중앙값
    APPROX_QUANTILES(cpc, 100)[OFFSET(25)]  → 상위 25% (CPC 는 낮을수록 좋다)
    APPROX_QUANTILES(ctr, 100)[OFFSET(90)]  → 상위 10% (CTR 은 높을수록 좋다)

🔴 **그러면서 `bm_benchmark` 에 없는 축을 찾았습니다 — `industry` 입니다.**

    시장×매체로 견주면   66개 조합 중 **3개만** 비교 성립
    업종×매체로 견주면   수송/항공·google_ads 비교군 **59** · dv360 **36**

**«같은 시장» 이 아니라 «같은 업종» 이 진짜 비교축이었습니다.**
현대차가 집행하는 해외 시장(BR·ES·IN·NL·PH·SA)에는 다른 브랜드가 아예 없어
시장 축으로는 비교가 성립하지 않습니다.

> `bm_benchmark` 를 되살릴 때 **`industry` 를 키에 넣는 것**을 권합니다.
> 지금 스키마는 `media × market` 뿐이라 같은 한계를 갖게 됩니다.

---

## 5. 부탁

1. **`mart.py` 에 두 테이블 생성 코드를 되살릴지 결정해 주십시오.**
   - 되살린다면 → DB 에이전트가 `dictionary_marts` 에 등재하겠다고 했습니다
   - 안 되살린다면 → **낡은 두 테이블을 지우거나 이름에 `_deprecated` 를 붙여** 주십시오.
     지금은 **조회가 되어서** 다음 사람이 최신인 줄 알고 씁니다
2. 되살린다면 **`industry` 를 키에 추가**해 주십시오 (§4 근거)
3. 처리 후 DB 에이전트에 알려 주시면 사전 등재가 이어집니다

**A1 쪽은 급하지 않습니다** — `bm_campaign_monthly` 로 이미 돌고 있습니다.
다만 **«낡은 채로 조회되는»** 상태가 위험해서 알립니다.

---

## 6. 확인에 쓴 것

```sql
-- 어느 테이블이 멈췄나
SELECT table_id, row_count,
       FORMAT_TIMESTAMP('%Y-%m-%d', TIMESTAMP_MILLIS(last_modified_time)) AS last_mod
FROM `innocean-perf-apac-kr.apac_kr_benchmark.__TABLES__`
ORDER BY last_mod, table_id;

-- bm_fact_monthly 최신 기간
SELECT MAX(period), MAX(_built_at) FROM `innocean-perf-apac-kr.apac_kr_benchmark.bm_fact_monthly`;
```

원인 규명은 DB 에이전트가 했습니다 —
`DB_Management_system/QA/` 의 `agent_data_requests` 큐 `all/benchmark_mart` 회신에 전문이 있습니다.

**회신은 `15_` 로 남겨 주시면 A1 이 읽습니다.**
