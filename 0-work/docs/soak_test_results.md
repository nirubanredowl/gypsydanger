# CDN soak & scaling ladder results

Record each run here. Automated runs also write JSON to `s3://$BUCKET/logs/ladder/rung{N}/`.

---

## B0 — 1 VM baseline (2026-07-01)

- **Instance:** `i-0812f82dd21298e96` (`gypsy-danger-soak-01`)
- **Workers:** 1
- **Ticker:** CBA (7,713 keys available)
- **Rate limit:** 1.0 req/s
- **Requests:** 500
- **Success:** 500 (100%)
- **429:** 0 | **503:** 0 | **other:** 0
- **Bytes:** ~502 MB
- **Elapsed:** 1,416 s (~23.6 min)
- **docs/hr (per worker):** ~1,271
- **Verdict:** PASS — stable at 1 req/s from AWS Sydney

---

## Rung 1 — (pending)

- **Workers:** 1
- **Keys:** 2,000 (shared pool)
- **Rate limit:** 1.0 req/s per worker
- **Aggregate docs/hr:**
- **vs B0 linear (×1):**
- **Verdict:**

---

## Rung 2 — (2026-07-02)

- **Workers:** 4 (separate EC2, unique IPs)
- **Keys / worker:** 500 (2,000 total)
- **Rate limit:** 1.0 req/s
- **Success:** 2,000/2,000 (0% errors)
- **Aggregate docs/hr:** 3,703
- **Per-worker docs/hr:** 1,245 | 1,094 | 926 | 1,098
- **vs B0 linear (×4 = 5,084):** 72.8% (target for PASS was ≥4,067)
- **Verdict:** **FAIL / plateau** (borderline — missed 80% threshold by ~9%)
- **S3:** `s3://gypsy-danger-asx-691811257790/logs/ladder/rung2/summary.json`
- **Note:** 0×429/503 — slowdown is throughput variance / soft contention, not hard blocks

---

## Rung 3 — (skip or optional)

- **Workers:** 10
- **Keys / worker:** 200
- **Verdict:**

---

## Rung 4 — (2026-07-02)

- **Run ID:** `20260702T014116Z-98609`
- **Workers:** 20 (separate EC2)
- **Keys / worker:** 100 (2,000 total)
- **Rate limit:** 1.0 req/s
- **Success:** 1,999/2,000 (0.05% errors — 1× other on worker_07)
- **Aggregate docs/hr:** **17,870**
- **Per-worker docs/hr:** 894–1,711 (median ~1,325)
- **Wall clock:** ~403 s (~6.7 min)
- **vs B0 linear (×20 = 25,420):** 70.3% full linear; **88% of 80% pass target (20,336)**
- **vs rung 2 (×5 workers):** 3,703 → 17,870 = **4.8×** throughput (near-linear scale-up)
- **Verdict:** **FAIL / plateau** by strict rule, but **clear winner for fleet sizing**
- **S3:** `s3://gypsy-danger-asx-691811257790/logs/ladder/rung4/20260702T014116Z-98609/summary.json`

---

## Chosen fleet size

- **Highest useful rung:** **Rung 4 (20 workers)** — scales well from rung 2; not capped at ~3,700 aggregate
- **Workers for Phase C fetch:** **20**
- **Expected throughput:** ~17,870 docs/hr → full corpus (~1.26M) in **~3 days**
- **Notes:** Strict 80% linear threshold is conservative; 0.05% error rate is acceptable. Rung 5/6 optional but likely diminishing returns.
