---
title: Data cleanup backlog
type: backlog
status: deferred
tags: [data-quality, fetch, parse, cleanup]
---

# Data cleanup backlog

Items to revisit **after** core pipeline stages (parse / enrichment) unless they block downstream work. Not in scope for current Stage 3B/3C unless explicitly promoted.

## How to use

- Add rows with date, category, and concrete follow-up.
- Machine-readable detail may live under `data/` or S3 `manifests/`; this file is the human/agent index.
- When an item is resolved, move it to **Resolved** with date and outcome.

---

## Open — CDN PDF unavailable (Stage 3A gaps)

**Discovered:** 2026-07-16 (parse run `20260716T140917Z-parse`, 3 worker “missing PDF” failures)  
**Retry:** `parse-missing-pdf-retry` — Markit CDN returns HTTP 200 with body `[]` (not a PDF); verified from agent VM and soak EC2.  
**Structured record:** [`data/parse_3a/unavailable_cdn_pdfs.json`](../../data/parse_3a/unavailable_cdn_pdfs.json)  
**S3 mirror:** `s3://gypsy-danger-asx-691811257790/manifests/parse/unavailable_cdn_pdfs.json`

| Ticker | Document key | Published | Expected S3 PDF key | Cleanup follow-up |
|--------|--------------|-----------|-------------------|-------------------|
| SIX | `2995-01668010-6A735896` | 2015-09-30 | `entities/SIX/annual_reports/2015_2995-01668010-6A735896.pdf` | Source PDF manually or drop from loose annual corpus; **same ticker/year** already covered by `2995-01679603-6A740324` (2015-10-30, fetched + parsed). Decide whether Sep filing is required for research or dedupe catalog. |
| CCR | `2924-02704583-3A624775` | 2023-08-29 | `entities/CCR/annual_reports/2023_2924-02704583-3A624775.pdf` | Manual PDF source → upload to S3 → run `22_liteparse_document.py`; or exclude from corpus counts with reason. |
| REZ | `2995-01557188-2A820311` | 2014-09-29 | `entities/REZ/annual_reports/2014_2995-01557188-2A820311.pdf` | Same as CCR. |

**Suggested later-stage cleanup tasks (batch):**

1. Reconcile `documents_total` (22,573) vs **fetchable** annual PDFs on CDN.
2. Tag announcements CSV rows with `pdf_status`: `available` \| `cdn_empty` \| `manual_required`.
3. Optional: second-pass fetch from alternate URLs / ASX archive if documented.
4. Re-run parse only for keys that gain PDFs (`--skip-if-exists` safe).

---

## Resolved

_(none yet)_
