---
title: Gypsy Danger — ASX Temporal Research
type: project
status: active
domain: finance / corporate research
signal: ASX annual reports → temporal graph → leakage & leadership analysis
tags: [asx, annual-reports, temporal-graph, leakage, erp, leadership]
---

# Gypsy Danger — ASX Temporal Research

**Rip ASX annual reports to build a temporal entity graph and test whether ERP changes and executive turnover correlate with financial leakage and performance shifts.**

## Why

Understand organisational value leakage through primary-source filings at scale across the ASX. The end state is a temporal graph linking entities, annual reports, events, and financial signals — grounded in what companies actually disclose, not secondary summaries.

Pipeline patterns are adapted from [Papal Papers RESEARCH_APPROACH.md](https://github.com/nirubanxp413/papalpapers/blob/27a5b694a2c0bf988adbc15d426fb7c6b6622ddc/RESEARCH_APPROACH.md) (index-first, idempotent fetch, staged analysis). Execution detail lives in [`0-work/plans/plan.md`](../plans/plan.md).

## Research questions

These three questions define the project:

| # | Question |
|---|----------|
| 1 | Does ERP change result in leakage? |
| 2 | What are the key causes for leakage in an org? |
| 3 | When key execs leave, how does that impact financial performance? |

## Leakage

**Working definition:** measurable value erosion visible in filings — margin decline, impairment charges, integration or restructuring costs, revenue shortfalls vs prior guidance, or explicit language about under-recovery or revenue leakage. Refine during Stage 4 analysis.

## Project stages

- **Stage 1 (Test):** Pilot tickers (~10–20); validate normalise → index → fetch. See [`0-work/plans/plan.md`](../plans/plan.md).
- **Stage 2 (Fetch):** Full ASX corpus using the validated pipeline.
- **Stage 3 (Parse):** Organise report content — design TBD after fetch.
- **Stage 4 (Analysis):** Thematic extraction, temporal graph, reporting against the three questions.

## Data & storage

Fetch structure (foundational — parse and analysis layers added in later stages):

```
data/
  entities.csv                        # central source of truth: ticker, metadata, entity_xid
  fetch_log.json                      # success / skip / fail audit
  cache/                              # HTTP cache (gitignored)
  entities/{TICKER}/
    announcements.csv                 # all Markit API items (ticker + entity_xid + every item field)
    raw/{documentKey}.pdf             # one file per announcement
```

**Stage 1 scope:** index all announcements per entity; fetch can be scoped to annual reports via `--annual-reports-only` on `03_fetch_documents.py`. See [`0-work/docs/announcements-schema.md`](announcements-schema.md).

**Fetch model:** Markit Digital JSON API — paginate by `entity_xid`, store full index per ticker, download PDFs by `documentKey`. See [`0-work/docs/links.md`](links.md) and [`0-work/plans/plan.md`](../plans/plan.md).

**Scripts (Steps 1 → 2 → 3):** `01_normalise_entities.py` → `02_index_announcements.py` → `03_fetch_documents.py` (library: `00_asx_api.py`)

## Tasks

- [ ] Stage 1: test fetch pipeline on pilot tickers
- [ ] Stage 2: full ASX fetch
- [ ] Stage 3: parse (TBD)
- [ ] Stage 4: analysis (TBD)

## Agent brief

**Current state:** Index complete (~1,838 tickers, ~1.26M announcement rows). Annual report filter validated — **22,128 strict annual report PDFs** vs full corpus. Ladder complete through rung 4 (20 workers, ~17,870 docs/hr aggregate). Phase C fetch blocked pending legal review.

**Next action:** Legal clearance → Phase C fetch with `--annual-reports-only` (strict) to S3 via 20-worker fleet.

**Constraints**

- Read this file before planning or executing work
- Write plans to `0-work/plans/`
- Run side effects from `0-work/scripts/`; log every run in `0-work/scripts/log.md`
- Do not create git branches unless explicitly asked

**Open decisions for agent to flag, not resolve silently**

- Pilot ticker list (`data/pilot_tickers.txt`)
- First-time `entity_xid` bootstrap strategy (see plan)
- Parse approach (Stage 3)
- Local model choice for thematic runs (Stage 4)
