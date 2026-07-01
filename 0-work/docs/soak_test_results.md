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

## Rung 2 — (pending)

- **Workers:** 4
- **Keys / worker:** 500
- **Rate limit:** 1.0 req/s
- **Aggregate docs/hr:**
- **vs B0 linear (×4 = ~5,084):**
- **Verdict:**

---

## Rung 3 — (pending)

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

*(fill after ladder complete)*

- **Highest passing rung:**
- **Workers for Phase C fetch:**
- **Notes:**
