---
title: Gypsy Danger — Stage 3 Parse (LlamaIndex)
type: spec
status: draft
stage: 3
depends_on: Phase C fetch (S3 annual reports)
tags: [parse, llamaindex, tables, pdf, annual-reports]
---

# Stage 3 — Parse annual reports (LlamaIndex)

Design spec for parsing **22,560** loose annual-report PDFs already in S3. Goal: structured artefacts downstream of raw PDFs, ready for temporal graph + leakage analysis (Stage 4).

**Parent:** [`spec.md`](spec.md) · **Fetch layout:** [`announcements-schema.md`](announcements-schema.md)

---

## Corpus

| Item | Value |
|------|------:|
| S3 bucket | `gypsy-danger-asx-691811257790` |
| PDF prefix | `entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf` |
| Loose annual reports in S3 | ~22,560 |
| Missing on CDN (known) | 3 (SIX, CCR, REZ) |
| Tickers with ≥1 report | ~1,793 |

Index metadata (headline, announcement date, `fileSize`, types) lives in  
`entities/{TICKER}/{TICKER}_Announcements.csv` on the same bucket.

---

## Sample reports (for layout review)

Eight random PDFs across size / era / sector. **Regenerate browser links** (presigned, 7-day expiry):

```bash
set -a && source 0-work/scripts/.env && set +a
python3 0-work/scripts/14_parse_sample_links.py
```

| Ticker | FY | Size | Headline | S3 key |
|--------|-----|------|----------|--------|
| GMG | 2022 | 24 MB | Goodman Group Sustainability Report and 2022 Annual Report | `entities/GMG/annual_reports/2022_2924-02574374-2A1401779.pdf` |
| GRR | 2021 | 19 MB | Annual Report to shareholders | `entities/GRR/annual_reports/2021_2924-02366720-3A565819.pdf` |
| HFR | 2020 | 13 MB | Annual Report to shareholders | `entities/HFR/annual_reports/2020_2924-02219266-6A973367.pdf` |
| ALK | 2016 | 3.8 MB | Annual Report to shareholders - 2016 | `entities/ALK/annual_reports/2016_2995-01789611-6A794312.pdf` |
| MCP | 2018 | 3.4 MB | FY18 McPhersons Annual Report | `entities/MCP/annual_reports/2018_2995-02030256-2A1108135.pdf` |
| UNT | 2021 | 3.2 MB | Annual Report and Appendix 4E | `entities/UNT/annual_reports/2021_2924-02413413-3A574136.pdf` |
| A1N | 2019 | 2.7 MB | 2018 HT&E Annual Report | `entities/A1N/annual_reports/2019_2995-02074914-2A1132251.pdf` |
| CDM | 2013 | 1.5 MB | Annual Report June 2013 | `entities/CDM/annual_reports/2013_2995-01447152-2A757665.pdf` |

**What to look for when reviewing samples**

- Cover / TOC structure; single vs multi-document PDFs (e.g. sustainability + annual)
- Financial statement tables (income, balance sheet, cash flow) — scanned vs born-digital
- Column headers, units ($m, $000), comparative columns (current / prior year)
- Footnotes and segment breakdowns
- Director / executive sections relevant to leadership-change research
- Anything that must **not** be flattened (table structure matters for Stage 4)

---

## Parse modes (proposed)

Two pipelines, same input PDF, different outputs:

### Parse A — **Raw extraction** (text + tables as-is)

**Purpose:** Faithful dump for search, chunking, and manual QA.

| Output | Description |
|--------|-------------|
| `text/` | Page-ordered plain text (or markdown) per PDF |
| `tables/` | One artefact per detected table — format TBD after sample review (HTML, CSV, or LlamaIndex `Document` JSON) |
| `manifest.json` | Per-PDF: page count, parser version, char/table counts, errors |

**LlamaIndex direction:** `SimpleDirectoryReader` + PDF reader (e.g. `PDFReader`, `LlamaParse`, or `PyMuPDF`/`pdfplumber` node parser) → write nodes to disk or S3 without aggressive table normalisation.

**Open:** chunk size, overlap, whether to keep page boundaries in metadata.

### Parse B — **Structured financial tables**

**Purpose:** Normalised tables for time-series and cross-company comparison (Stage 4 leakage / performance signals).

| Output | Description |
|--------|-------------|
| `structured_tables/` | Tables matching a **defined schema** (you to specify after sample review) |
| `table_index.json` | Row-level index: ticker, FY, statement type, line label, value, unit, column year |

**Candidate schema dimensions** (fill in after review):

- [ ] Statement type: `income` | `balance_sheet` | `cash_flow` | `notes` | `other`
- [ ] Line item label (as printed vs normalised COA)
- [ ] Period columns: `2024`, `2023`, …
- [ ] Unit: `$`, `$m`, `$000`, `%`
- [ ] Consolidated vs segment
- [ ] Source page / table id

**LlamaIndex direction:** LlamaParse or custom `PDFTableReader` + Pydantic extraction / `StructuredLLMProgram` for header detection and row alignment — only after Parse A baseline exists.

---

## Storage layout (draft)

```
s3://gypsy-danger-asx-691811257790/
  entities/{TICKER}/annual_reports/{YYYY}_{documentKey}.pdf   # existing
  parsed/
    {TICKER}/{YYYY}_{documentKey}/
      raw/           # Parse A
        text.md
        tables/
        manifest.json
      structured/    # Parse B
        tables.json
        manifest.json
  manifests/
    parse_progress.json
```

Local mirror (optional): `data/parsed/…` for pilot tickers only.

---

## Execution plan (draft)

1. **Pilot** — 8 sample tickers above + CBA/QGL (preflight); validate Parse A quality.
2. **Lock table schema** — you annotate samples; update § Parse B schema in this doc.
3. **Implement** — `15_parse_annual_reports.py` (Parse A), then `16_structure_tables.py` (Parse B).
4. **Scale** — batch on EC2 or local with S3 read/write; progress manifest like Phase C.

---

## Agent brief (parse)

**Current state:** Fetch complete (~22,560 PDFs). Parse **not started**. This doc is the working spec.

**Next action:** Review sample PDFs → fill **Open decisions** below → approve Parse A pilot.

**Open decisions** (owner: you)

- [ ] LlamaIndex reader: LlamaParse vs open-source PDF stack
- [ ] Parse A table format: HTML / CSV / JSON
- [ ] Parse B target schema (columns, normalisation rules)
- [ ] Pilot ticker set (default: 8 samples + CBA + QGL)
- [ ] LLM provider for structured extraction (if any)
- [ ] Cost / volume limits for full corpus parse

---

## References

- [LlamaIndex — PDF parsing](https://docs.llamaindex.ai/en/stable/module_guides/loading/documents_and_nodes/usage_documents/)
- [LlamaParse](https://docs.llamaindex.ai/en/stable/llama_cloud/llama_parse/)
- [open-parse experiment](../experiments/open-parse/) — sample parse run on 8 PDFs
- Project fetch summary: `s3://gypsy-danger-asx-691811257790/manifests/fetch/20260706T033644Z-fetch/summary.json`
