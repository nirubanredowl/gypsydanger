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

## Rung 4 — (pending)

- **Workers:** 20
- **Keys / worker:** 100
- **Verdict:**

---

## Chosen fleet size

*(provisional — rung 2 borderline fail)*

- **Highest passing rung:** B0 only (strict); rung 2 at 73% linear with zero errors
- **Workers for Phase C fetch:** **4** conservative, or probe rung 4 (20) before deciding
- **Notes:** At 4 workers × ~926–1,245 docs/hr each, full corpus (~1.26M) ≈ **14–17 days**. More workers may not help much if CDN caps near ~3,700 aggregate docs/hr.
