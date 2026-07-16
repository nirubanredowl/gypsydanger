---
title: Stage 3 — LiteParse + LLM enrichment pipeline
type: plan
status: draft
stage: 3
depends_on:
  - Phase C annual reports (~22,560 PDFs in S3)
  - CFO change fetch (2,133 PDFs in S3)
  - parse-sample-corpus pilot (100 PDFs, LiteParse 100/100)
tags: [parse, liteparse, gemini, mastra, tables, state-management]
---

# Stage 3 — LiteParse + LLM enrichment pipeline

**Goal:** Turn raw PDFs into **clean, structured report artefacts** (especially financial tables) suitable for Stage 4 temporal graph and leakage analysis — without LlamaParse credit costs.

**Parent:** [`spec.md`](../docs/spec.md) · **Prior parse draft:** [`parse-spec.md`](../docs/parse-spec.md) (superseded on reader choice — see § Reader decision)

**Pilot corpus:** [`data/parse-sample-corpus/`](../data/parse-sample-corpus/) — 100 PDFs with LiteParse + open-parse outputs.

---

## Summary

| Phase | What | Output |
|-------|------|--------|
| **3A** | LiteParse basic parse | Per-PDF markdown + spatial JSON |
| **3B** | Page split | Per-page PDF + PNG (+ page-level LiteParse slice) |
| **3C** | LLM enrichment (Gemini via Mastra) | Cleaned markdown, structured tables, document manifest |
| **3D** | Scale-out | Batch runner over full S3 corpus with progress manifests |
| **2E** *(parallel)* | Expand fetch | Additional announcement types beyond annual + CFO |

LlamaParse is **out of scope** for cost reasons (~11M credits for full annual corpus). LiteParse is local/fast; LLM cost is bounded and controllable per page.

---

## Architecture

```mermaid
flowchart TB
  subgraph inputs [Inputs — S3]
    PDF["entities/{TICKER}/{corpus}/{name}.pdf"]
    IDX["entities/{TICKER}/{TICKER}_Announcements.csv"]
  end

  subgraph phase3a [3A — LiteParse basic parse]
    LP["LiteParse\noutput_format=markdown"]
    RAW_MD["parsed/{corpus}/{doc}/liteparse/document.md"]
    RAW_JSON["parsed/{corpus}/{doc}/liteparse/document.json"]
  end

  subgraph phase3b [3B — Page split]
    SPLIT["Split PDF → pages"]
    PAGE_PDF["pages/{n}.pdf"]
    PAGE_PNG["pages/{n}.png"]
    PAGE_MD["pages/{n}.liteparse.md"]
  end

  subgraph phase3c [3C — LLM enrichment — Mastra]
    STATE["state.json per document"]
    P1["Step 1: Page classify\n(text-heavy vs table-heavy)"]
    P2["Step 2: Page clean\n(Gemini multimodal)"]
    P3["Step 3: Table extract\n(structured JSON schema)"]
    P4["Step 4: Document merge\n(clean report + table index)"]
    CLEAN["enriched/document.md"]
    TABLES["enriched/tables.json"]
  end

  subgraph phase3d [3D — Scale]
    MANIFEST["manifests/parse_enrich_progress.json"]
    WORKERS["EC2 / VM workers\nidempotent shards"]
  end

  PDF --> LP
  LP --> RAW_MD
  LP --> RAW_JSON
  RAW_MD --> SPLIT
  PDF --> SPLIT
  SPLIT --> PAGE_PDF
  SPLIT --> PAGE_PNG
  SPLIT --> PAGE_MD

  PAGE_PNG --> P2
  PAGE_MD --> P2
  RAW_MD --> P4
  P1 --> P2 --> P3 --> P4
  P4 --> CLEAN
  P4 --> TABLES
  STATE --> P1
  P2 --> STATE
  P3 --> STATE
  P4 --> STATE

  phase3c --> MANIFEST
  phase3d --> WORKERS
```

---

## Corpus priority (parse order)

| Priority | Corpus | S3 prefix | Count | Avg pages/PDF | Notes |
|----------|--------|-----------|------:|--------------:|-------|
| 1 | Annual reports | `entities/{TICKER}/annual_reports/` | ~22,560 | ~50 | Primary leakage signal |
| 2 | CFO changes | `entities/{TICKER}/cfo_changes/` | 2,133 | ~1.4 | Leadership-change events |
| 3+ | TBD (fetch expansion) | per § Parallel fetch track | — | — | Appendix 4G, 4E, etc. |

---

## Phase 3A — LiteParse basic parse

**Purpose:** Fast, local, layout-aware first pass. No cloud parse credits.

### Inputs
- PDF from S3 (or local mirror)
- Announcement metadata from index CSV (ticker, date, headline, `announcementTypes`)

### Processing
```python
from liteparse import LiteParse

parser = LiteParse(
    output_format="markdown",
    image_mode="placeholder",
    extract_links=True,
    quiet=True,
    # ocr_enabled=True  # enable per-doc or for scanned pages (Phase 3B classifier)
)
result = parser.parse(pdf_bytes)
```

### Outputs (per document)
```
parsed/{TICKER}/01_annual_reports/{document_key}/   # corpus folder from registry
  meta.json
  liteparse/
    pages/
      0001/page.json              # text_items[] + page dimensions
      0001/page.md                # page.markdown (tables as pipe syntax)
    document.md                   # optional full-doc cache
    manifest.json
```

### Script
| Script | Role |
|--------|------|
| `22_liteparse_document.py` | Single-PDF parse (S3 → output prefix) |
| `23_liteparse_shard.py` | Shard worker (multi-doc, progress upload) |
| `aws/run_liteparse_parse.sh` | Orchestrator (shards, EC2, SNS) — mirror Phase C pattern |

### Pilot gate
- Run on `data/parse-sample-corpus/` (100 PDFs) — **done**
- Run on 10 pilot tickers (~200–500 PDFs) before full corpus
- Validate: markdown table count, OCR need rate, failure rate

### Learnings from sample (100 PDFs)
- LiteParse: **100/100** success, ~0.1–0.3s/page for born-digital annual reports
- Annual reports: **~50 markdown tables/PDF** (heuristic); CFO notices: 0–2 tables
- open-parse: 99/100 (one pdfminer image failure); fragmented nodes, no markdown tables
- **Decision:** LiteParse is primary reader; open-parse optional for bbox QA only

---

## Phase 3B — Page split

**Purpose:** Give the LLM stage page-sized units — PDF for structure, PNG for vision, LiteParse slice for text anchor.

### Processing
1. Split source PDF into single-page PDFs (`pypdf` or `pymupdf`)
2. Render each page to PNG (300 DPI default; LiteParse `lit screenshot` or `pymupdf`)
3. Slice LiteParse output by `page_num` → per-page markdown + text_items subset

### Outputs
```
parsed/{corpus}/{TICKER}/{document_key}/
  pages/
    manifest.json           # page_count, dimensions, render_dpi
    001/
      page.pdf
      page.png
      liteparse.md          # page-scoped markdown from 3A
      liteparse.json        # text_items for this page
    002/
      ...
```

### Script
| Script | Role |
|--------|------|
| `24_split_pdf_pages.py` | Split + render + slice LiteParse JSON |
| `25_build_parse_shards.py` | Shard documents for workers |

### Design notes
- Page numbers: 1-based, zero-padded 3 digits (`001` … `120`)
- Store page count in all downstream state keys
- Skip empty pages (no text_items and blank render) — log in manifest
- Large PDFs (GMG 2022, 100+ pages): process pages lazily; do not load all PNGs into memory

---

---

## Data model — catalog CSVs, paths, and traceability

**Principle:** Folders hold blobs; **CSV catalogs** are the query layer. Every artefact joins back to `entities.csv` and per-ticker `{TICKER}_Announcements.csv` via stable keys.

### Per-ticker layout + ordered corpus folders

**Parse storage is per ticker.** Corpus types use a **fixed, numbered registry** — no ad-hoc folder names.

#### `catalog/corpus_registry.csv` (master list — defines order)

| `corpus_id` | `corpus_key` | `parse_folder` | `fetch_s3_folder` | `status` | Description |
|------------:|--------------|----------------|-------------------|----------|-------------|
| `01` | `annual_reports` | `01_annual_reports` | `annual_reports` | **active** | Loose annual reports (~22,560) |
| `02` | `quarterly_reports` | `02_quarterly_reports` | `quarterly_reports` | planned | Quarterly activity / Appendix 4C |
| `03` | `half_year_reports` | `03_half_year_reports` | `half_year_reports` | planned | Half-year / interim reports |
| `04` | `full_year_accounts` | `04_full_year_accounts` | `full_year_accounts` | planned | Appendix 4E financial bundles |
| `05` | `cfo_changes` | `05_cfo_changes` | `cfo_changes` | **active** | CFO appointment / resignation (~2,133) |
| `06` | `ceo_changes` | `06_ceo_changes` | `ceo_changes` | planned | CEO / MD changes |
| `07` | `appendix_4g` | `07_appendix_4g` | `appendix_4g` | planned | Corporate governance (~14k) |
| `08` | `director_changes` | `08_director_changes` | `director_changes` | planned | Director appt / resign |

Rules:
- **`corpus_id`** is the sort key everywhere (catalogs, manifests, UI).
- **`corpus_key`** is the stable snake_case identifier in CSV columns (never `annual` or `cfo` shorthand).
- **`parse_folder`** = `{corpus_id zero-padded}_{corpus_key}` under each ticker.
- **`fetch_s3_folder`** may differ from parse folder name during migration; registry maps fetch → parse.
- New corpus types **append** with the next integer — never renumber existing IDs.

### Canonical identifiers

| ID | Scope | Source | Example |
|----|-------|--------|---------|
| `ticker` | Entity | `entities.csv` | `CBA` |
| `entity_xid` | Entity (API) | `entities.csv` | `362963398` |
| `corpus_id` | Report type | `corpus_registry.csv` | `01` |
| `corpus_key` | Report type | `corpus_registry.csv` | `annual_reports` |
| `document_key` | **Document (global PK)** | announcements `documentKey` | `2924-02860163-2A1552191` |
| `page_num` | Page within doc | 1-based | `42` |
| `page_id` | **Page (global PK)** | `{document_key}#p{page_num:04d}` | `2924-…#p0042` |
| `statement_id` | Standalone statement | `{document_key}#s{type}` | `2924-…#sincome` |

Document folder name = **`document_key`** (globally unique; joins announcements CSV directly).

### Existing layer (unchanged)

```
data/
  entities.csv
  entities/{TICKER}/{TICKER}_Announcements.csv
```

### S3 layout — per ticker, ordered corpus

```
s3://bucket/

  entities/{TICKER}/                          # FETCH (existing)
    {TICKER}_Announcements.csv
    annual_reports/{YYYY}_{documentKey}.pdf
    cfo_changes/{YYYY-MM-DD}_{documentKey}.pdf

  parsed/{TICKER}/                            # PARSE root — one folder per company
    01_annual_reports/
      {documentKey}/
        meta.json                             # document metadata (see below)
        state.json                            # pipeline step status
        liteparse/                            # 3A — page-native LiteParse output
          pages/
            0001/page.json
            0001/page.md
            0002/...
          document.md                       # full-doc markdown (optional cache)
          manifest.json
        pages/                                # 3B — render assets
          0001/page.pdf
          0001/page.png
        flash/                                # 3C — Gemini Flash (current scope)
          pages/
            0001/content.md                 # cleaned page (text + inline tables)
          statements/                         # meaningful standalone tables
            income_statement.json
            balance_sheet.json
            cash_flow.json
            manifest.json                     # which statements found + page refs
        usage.json                            # token cost for this document

    05_cfo_changes/
      {documentKey}/...

  catalog/                                    # QUERY LAYER
    corpus_registry.csv
    documents.csv
    pages.csv
    statements.csv                            # P&L, balance sheet, etc.
    usage_runs.csv
    usage_documents.csv
```

**Parsed prefix:** `parsed/{ticker}/{parse_folder}/{document_key}/`  
Example: `parsed/CBA/01_annual_reports/2924-02860163-2A1552191/`

---

## LiteParse output structure (native)

LiteParse is **page-first**, but it does **not** emit typed blobs (`text` / `table` / `image`) as separate native objects. Understanding this shapes how we store and clean.

```
ParseResult
├── text: str                         # full-document markdown (output_format=markdown)
├── pages: ParsedPage[]
│   ├── page_num: int
│   ├── width, height: float          # PDF points
│   ├── text: str                     # plain reconstructed text for this page
│   ├── markdown: str                 # rendered markdown for this page
│   └── text_items: TextItem[]        # atomic spatial text runs
│       ├── text: str
│       ├── x, y, width, height       # bounding box
│       ├── font_name, font_size
│       ├── confidence, rotation
│       └── words: WordBox[]          # optional per-word bboxes (emit_word_boxes=True)
└── images: ExtractedImage[]          # document-level extracted images
    ├── page_num, width, height, format
    └── (image bytes via screenshot API)
```

**What this means:**

| User expectation | LiteParse reality |
|----------------|-------------------|
| Page → typed blobs (text/table/image) | Page → **`text_items[]`** (spatial runs) + **`markdown`** string |
| Separate table objects | Tables appear as **markdown pipe syntax** inside `page.markdown` / `result.text` when `output_format=markdown` |
| Per-page images | Image **placeholders** in markdown (`![](image_p1_0.png)`); bytes via `parser.screenshot()` |

We store LiteParse **page-natively** under `liteparse/pages/{NNNN}/`:

```json
// liteparse/pages/0001/page.json
{
  "page_num": 1,
  "width": 595.0,
  "height": 842.0,
  "text_item_count": 55,
  "text_items": [
    {
      "text": "Life360",
      "x": 72.1, "y": 54.3, "width": 120.0, "height": 14.2,
      "font_name": "Arial-Bold", "font_size": 18.0,
      "word_count": 1
    }
  ],
  "images": [
    { "ref": "image_p1_0", "page_num": 1 }
  ]
}
```

```markdown
<!-- liteparse/pages/0001/page.md — verbatim page.markdown -->
![](image_p1_0.png)

# Life360

## ASX ANNOUNCEMENT
...
```

Optional derived layer (we build, not LiteParse): cluster `text_items` + markdown segments into **`blocks[]`** with `type: heading | text | markdown_table | image_ref` for Flash input. Stored under `liteparse/pages/{n}/blocks.json` if needed.

---

## Document metadata (`meta.json`)

Per document at `parsed/{TICKER}/{parse_folder}/{document_key}/meta.json`.

| Field | Source | Example |
|-------|--------|---------|
| `document_key` | announcements | `2924-02860163-2A1552191` |
| `ticker`, `entity_xid` | announcements / entities | `CBA` |
| `corpus_id`, `corpus_key` | registry | `01`, `annual_reports` |
| `published_date` | announcements `date` (announcement date) | `2024-09-25` |
| `headline` | announcements | `Annual Report to shareholders` |
| `announcement_types` | announcements | `["Annual Report", …]` |
| `s3_pdf_key` | fetch path | `entities/CBA/annual_reports/…` |
| `reporting_period` | **derived** (Flash or rules) | see below |
| `page_count` | LiteParse | `56` |
| `liteparse_version` | parser | `2.4.0` |

```json
{
  "reporting_period": {
    "label": "FY2024",
    "end_date": "2024-06-30",
    "start_date": "2023-07-01",
    "basis": "flash_extract"
  }
}
```

**Derivation order:** (1) headline regex (`FY24`, `year ended 30 June 2024`); (2) Flash pass on cover + contents pages; (3) null + flag for manual review. `published_date` is always the ASX announcement date; `reporting_period.end_date` is the financial year end (distinct fields).

---

## After Flash clean — page content + standalone statements

**Current pipeline scope: Gemini Flash only.** Mastra orchestration is specified separately (§ Mastra — deferred); not built in the parsing pilot.

### Per-page (`flash/pages/{NNNN}/`)

| File | Contents |
|------|----------|
| `content.md` | Cleaned reading-order text; tables as markdown blocks inline |
| `content.json` | Optional structured blocks if Flash returns JSON |

### Document-level statements (`flash/statements/`)

Meaningful financial tables **promoted** from pages — not every markdown table, only statement-level:

| File | `statement_type` |
|------|------------------|
| `income_statement.json` | `income` |
| `balance_sheet.json` | `balance_sheet` |
| `cash_flow.json` | `cash_flow` |
| `changes_in_equity.json` | `equity` |
| `notes.json` | `notes` (optional aggregate) |
| `manifest.json` | which statements exist, source `page_id`s, confidence |

```json
// flash/statements/income_statement.json
{
  "statement_id": "2924-02860163-2A1552191#sincome",
  "statement_type": "income",
  "title": "Consolidated statement of comprehensive income",
  "unit": "$m",
  "currency": "AUD",
  "columns": ["2024", "2023"],
  "rows": [
    { "label": "Revenue", "values": [98234, 89102] }
  ],
  "source_page_ids": ["2924-02860163-2A1552191#p0012", "…#p0013"],
  "confidence": 0.91
}
```

Indexed in `catalog/statements.csv` for cross-company queries (revenue time-series, etc.).

---

## Token usage tracking

At millions of pages, cost tracking is mandatory. Record at **run** and **document** granularity.

### `catalog/usage_runs.csv` — one row per pipeline run / batch

| Column | Description |
|--------|-------------|
| `run_id` | e.g. `20260716T120000Z-flash-clean` |
| `phase` | `3a_liteparse` \| `3b_split` \| `3c_flash_page` \| `3c_flash_statements` |
| `model` | `gemini-2.5-flash`, `none` (LiteParse is local) |
| `corpus_id` | or `all` |
| `documents_in` | count |
| `pages_in` | count |
| `input_tokens` | sum |
| `output_tokens` | sum |
| `cost_usd` | computed from model price table |
| `started_at`, `finished_at` | ISO |
| `status` | `running` \| `complete` \| `error` |

### `catalog/usage_documents.csv` — one row per document × phase

| Column | Description |
|--------|-------------|
| `run_id` | FK → usage_runs |
| `document_key` | FK → documents |
| `ticker`, `corpus_id` | denorm |
| `phase` | |
| `model` | |
| `pages_processed` | |
| `input_tokens`, `output_tokens`, `cost_usd` | |
| `updated_at` | |

### Per-document `usage.json`

Roll-up cached on the document folder for quick inspection:

```json
{
  "document_key": "2924-02860163-2A1552191",
  "phases": {
    "3c_flash_page": { "input_tokens": 12400, "output_tokens": 3200, "cost_usd": 0.0042 },
    "3c_flash_statements": { "input_tokens": 8200, "output_tokens": 1100, "cost_usd": 0.0028 }
  },
  "total_cost_usd": 0.0070
}
```

LiteParse (3A/3B) records `cost_usd: 0` but logs `elapsed_s` and `cpu_s` in `usage_runs` for capacity planning.

---

## Mastra — deferred (separate spec)

**Not in current parsing build.** When we add Mastra for durable multi-step enrichment:

- Orchestrates the same sub-steps as § After Flash clean (classify → clean → extract statements → merge)
- Adds workflow state, retries, human review gates
- Token usage flows into the **same** `usage_runs.csv` / `usage_documents.csv` with `orchestrator=mastra`
- Spec will live in `0-work/plans/stage3-mastra-enrichment.md` when scoped

For now: **sequential Python runner + `state.json`** per document, Gemini Flash direct API.

---

### Catalog CSVs (query layer)

#### `catalog/documents.csv`

| Column | Notes |
|--------|-------|
| `document_key` | PK |
| `ticker`, `entity_xid` | FK |
| `corpus_id`, `corpus_key` | from registry (not free-text) |
| `published_date`, `headline`, `announcement_types` | from announcements |
| `reporting_period_label`, `reporting_period_end` | from `meta.json` |
| `s3_pdf_key`, `parsed_prefix` | paths |
| `page_count` | |
| `status_3a`, `status_3b`, `status_3c_flash` | pipeline |
| `statement_types_found` | JSON array e.g. `["income","balance_sheet"]` |
| `flash_cost_usd` | roll-up |
| `updated_at` | |

#### `catalog/pages.csv` (~1.15M rows)

| Column | Notes |
|--------|-------|
| `page_id` | PK |
| `document_key`, `ticker`, `corpus_id` | FK / denorm |
| `page_num` | |
| `path_liteparse_json`, `path_liteparse_md` | |
| `path_page_png` | |
| `path_flash_content_md` | null until Flash |
| `has_tables` | bool |
| `flash_input_tokens`, `flash_output_tokens` | per-page Flash cost |
| `updated_at` | |

#### `catalog/statements.csv`

| Column | Notes |
|--------|-------|
| `statement_id` | PK |
| `document_key`, `ticker`, `corpus_id` | |
| `statement_type` | `income`, `balance_sheet`, `cash_flow`, … |
| `reporting_period_label` | denorm from meta |
| `unit`, `currency`, `columns` | JSON |
| `row_count`, `confidence` | |
| `path_statement_json` | |
| `source_page_ids` | JSON array |

### Traceability chain

```
entities.csv
  └─ ticker
       └─ entities/{TICKER}/{TICKER}_Announcements.csv
            └─ document_key + corpus_registry filter
                 ├─ entities/{TICKER}/{fetch_s3_folder}/…pdf
                 └─ parsed/{TICKER}/{parse_folder}/{document_key}/
                      ├─ meta.json
                      ├─ liteparse/pages/{n}/page.json + page.md
                      ├─ flash/pages/{n}/content.md
                      └─ flash/statements/*.json

catalog/documents.csv, pages.csv, statements.csv, usage_*.csv
```

### Example queries

```sql
-- CBA FY2024 income statement
SELECT s.*, d.reporting_period_label
FROM statements s
JOIN documents d ON s.document_key = d.document_key
WHERE d.ticker = 'CBA' AND s.statement_type = 'income'
  AND d.reporting_period_label = 'FY2024';

-- Token spend by corpus last run
SELECT corpus_id, phase, SUM(cost_usd), SUM(input_tokens)
FROM usage_documents
WHERE run_id = '20260716T120000Z-flash-clean'
GROUP BY corpus_id, phase;

-- Pages awaiting Flash
SELECT COUNT(*) FROM pages
WHERE path_flash_content_md IS NULL
  AND corpus_id = '01';
```

### Phase → artefact map

| Phase | Writes | Catalog update |
|-------|--------|----------------|
| Fetch | `entities/…` PDF | `documents.csv` (pending) |
| 3A LiteParse | `liteparse/pages/{n}/page.json`, `page.md` | `documents.status_3a`, `meta.json` |
| 3B Split | `pages/{n}/page.pdf`, `page.png` | `documents.page_count`, `pages.csv` |
| 3C Flash page | `flash/pages/{n}/content.md` | `pages.path_flash_content_md`, usage |
| 3C Flash statements | `flash/statements/*.json` | `statements.csv`, `documents.statement_types_found` |

### Scale estimates

| corpus_id | Docs | Avg pages | Total pages |
|----------:|-----:|----------:|------------:|
| 01 | 22,560 | ~50 | ~1,150,000 |
| 05 | 2,133 | ~1.4 | ~3,000 |

~6M files under page folders. **Always query via `catalog/*.csv`**, never walk S3.

### Local dev mirror

```
data/catalog/           # mirror of s3 catalog/
data/parsed/{TICKER}/   # pilot tickers only
```

---

## Phase 3C — LLM enrichment (Gemini + Mastra)

**Purpose:** Produce a **cleaned report** and **structured tables** from LiteParse draft + page images. Multi-step with explicit state.

### Why Mastra
- Workflow / agent orchestration with **durable step state**
- Retry, resume, and human-in-the-loop hooks
- Clean separation: parse (deterministic) vs enrich (LLM)
- Alternative: direct Gemini API in Python if Mastra is heavy for v1 — plan supports both; **Mastra preferred** for production state management

### State model (per document)

`parsed/{corpus}/{TICKER}/{document_key}/state.json`:

```json
{
  "document_key": "2924-02860163-2A1552191",
  "corpus": "annual",
  "ticker": "CBA",
  "s3_pdf_key": "entities/CBA/annual_reports/...",
  "page_count": 56,
  "pipeline_version": "3c-v1",
  "steps": {
    "3a_liteparse": { "status": "complete", "at": "..." },
    "3b_split":     { "status": "complete", "at": "...", "pages": 56 },
    "3c_classify":  { "status": "complete", "at": "...", "pages_done": 56 },
    "3c_clean":     { "status": "running",  "at": "...", "pages_done": 23 },
    "3c_tables":    { "status": "pending" },
    "3c_merge":     { "status": "pending" }
  },
  "pages": {
    "001": {
      "classify": "table_heavy",
      "clean_status": "complete",
      "tables_status": "complete",
      "error": null
    }
  }
}
```

**Rules**
- Each step writes only its slice of state; runner checks `steps.*.status` before advancing
- Page-level idempotency: skip pages where `clean_status == complete`
- Failures: set `error`, do not advance; retry from failed page
- Global manifest aggregates doc-level status for SNS / progress email

### Sub-steps

#### 3C-1 — Page classify
- **Input:** `pages/{n}/liteparse.md`, optional `page.png` thumbnail
- **Output:** `table_heavy` | `text_heavy` | `mixed` | `skip` (blank)
- **Model:** Gemini Flash (cheap, fast)
- **Purpose:** Route table-heavy pages to stronger table extraction prompt

#### 3C-2 — Page clean
- **Input:** `page.png` + `liteparse.md` (LiteParse draft as anchor)
- **Output:** `pages/{n}/cleaned.md` — corrected text, headings, reading order
- **Model:** Gemini Pro or Flash with vision
- **Prompt focus:** Fix column order, merge broken lines, preserve numbers exactly

#### 3C-3 — Table extract
- **Input:** `page.png` + `cleaned.md` (for table-heavy/mixed pages only)
- **Output:** `pages/{n}/tables.json` — array of tables matching schema (below)
- **Model:** Gemini Pro with structured output / JSON schema

#### 3C-4 — Document merge
- **Input:** all `cleaned.md` + all `tables.json` + original `document.md`
- **Output:**
  - `enriched/document.md` — full cleaned report
  - `enriched/tables.json` — merged table index with doc-level metadata
  - `enriched/manifest.json` — counts, model versions, token usage

### Table schema (v1 draft — lock on pilot)

```json
{
  "table_id": "p012_t01",
  "page_num": 12,
  "title": "Consolidated statement of comprehensive income",
  "statement_type": "income",
  "unit": "$m",
  "currency": "AUD",
  "columns": ["2024", "2023"],
  "rows": [
    { "label": "Revenue", "values": [98234, 89102] },
    { "label": "EBITDA", "values": [12345, 11200] }
  ],
  "confidence": 0.92,
  "source": "gemini-3c-3"
}
```

Refine after reviewing 10 pilot documents against [`parse-spec.md`](../docs/parse-spec.md) § Parse B.

### Mastra workflow sketch

```
workflows/
  enrich-document.ts       # orchestrates 3C-1 … 3C-4
  steps/
    classify-page.ts
    clean-page.ts
    extract-tables.ts
    merge-document.ts
```

Runner invokes Mastra via HTTP or CLI; Python parse stages (3A, 3B) remain in `0-work/scripts/`. Bridge: `26_invoke_enrich_workflow.py`.

### Cost control
| Lever | Approach |
|-------|----------|
| Model routing | Flash for classify + text pages; Pro for table-heavy only |
| Page budget | Cap pages/doc for v1 pilot; skip duplicates (revision PDFs) |
| Caching | Hash `(page.png, prompt_version)` → skip re-enrich on resume |
| Batch API | Gemini batch for 3C-2/3C-3 at scale (lower $, higher latency) |

**Rough estimate (annual corpus):** ~1.15M pages × ~$0.001–0.01/page (model-dependent) = **$1k–12k** vs LlamaParse ~11M credits. Pilot 100 docs first to measure actual tokens/page.

---

## Phase 3D — Scale-out

**Pattern:** Reuse Phase C fetch architecture (shards, EC2 workers, burn rotation, SNS, progress manifest).

### Stages at scale
1. **3A only** — LiteParse all annual reports (~22k PDFs). Local CPU, no LLM. ETA: hours on 10–20 workers.
2. **3B** — Page split for docs where 3A complete.
3. **3C** — Enrich in batches (e.g. 500 docs/batch) with Gemini rate limits.
4. **CFO corpus** — Same pipeline; 2,133 docs × ~1.4 pages ≈ trivial for 3C.

### Progress manifest
`s3://…/manifests/parse_enrich_progress.json`:
```json
{
  "run_id": "20260716T120000Z-enrich",
  "corpus": "annual",
  "documents_total": 22560,
  "3a_complete": 0,
  "3b_complete": 0,
  "3c_complete": 0,
  "pages_enriched": 0,
  "errors": 0,
  "token_usage": { "input": 0, "output": 0 }
}
```

### Scripts / infra
| Script | Role |
|--------|------|
| `27_build_enrich_shards.py` | Balanced shards by page count |
| `28_run_enrich_shard.py` | Worker: 3A → 3B → 3C per shard |
| `aws/run_parse_enrich.sh` | Orchestrator |
| `aws/enrich_wait_and_notify.sh` | Waiter + SNS |

---

## Parallel track — Expand fetch (Stage 2E)

While parse pipeline is built on annual + CFO, **extend the index-first fetch model** to additional announcement types.

### Candidate types (from probes)

| Priority | Signal | ~Rows | S3 prefix proposal | Research use |
|----------|--------|------:|-------------------|--------------|
| 1 | `Appendix 4G` | ~14,346 | `entities/{TICKER}/appendix_4g/{date}_{docKey}.pdf` | Governance / KMP |
| 2 | `Appendix 4E` / `Full Year Accounts` | TBD | `entities/{TICKER}/full_year_accounts/` | Financial statements bundle |
| 3 | CEO change (headline) | ~835 | `entities/{TICKER}/ceo_changes/` | Exec turnover (Q3) |
| 4 | `Director Appointment/Resignation` | TBD | `entities/{TICKER}/director_changes/` | Board changes |

### Process (mirror CFO fetch)
1. **Probe** — `15_probe_*` style script per type (headline regex + tag filter)
2. **Shard + fetch** — reuse `16_fetch_*` / `aws/run_*_fetch.sh` pattern
3. **Register corpus** in parse pipeline (`corpus` enum in state.json)
4. **Do not block** Stage 3 pilot on these — fetch can run in parallel on EC2

### Doc to create
- `0-work/docs/fetch-expansion-types.md` — per-type filter rules, counts, S3 paths

---

## Storage layout (S3 canonical)

```
s3://gypsy-danger-asx-691811257790/
  entities/{TICKER}/
    annual_reports/{YYYY}_{docKey}.pdf          # existing
    cfo_changes/{YYYY-MM-DD}_{docKey}.pdf       # existing
    appendix_4g/{YYYY-MM-DD}_{docKey}.pdf       # future
  parsed/
    annual/{TICKER}/{doc_id}/
      liteparse/ ...
      pages/ ...
      enriched/ ...
      state.json
    cfo/{TICKER}/{doc_id}/ ...
  manifests/
    parse_enrich_progress.json
    parse_enrich/{run_id}/summary.json
```

Local pilot mirror: `data/parsed/` for ≤100 docs during development.

---

## Implementation sequence

### Sprint 1 — Foundation (pilot tickers)
- [ ] Lock table schema v1 on 5 annual + 2 CFO samples from `parse-sample-corpus`
- [ ] Implement `22_liteparse_document.py` + `24_split_pdf_pages.py`
- [ ] Stand up Mastra workflow repo / folder (`0-work/enrich/` or `services/enrich/`)
- [ ] Implement 3C-1 classify + 3C-2 clean on **one** 10-page doc
- [ ] Manual QA: compare enriched vs source PDF

### Sprint 2 — Full page pipeline
- [ ] Complete 3C-3 table extract + 3C-4 merge
- [ ] `state.json` read/write + resume tests
- [ ] Run full pipeline on **10 pilot tickers** (~200–500 docs)
- [ ] Measure tokens/page, failure rate, table accuracy sample

### Sprint 3 — Scale 3A + 3B
- [ ] `23_liteparse_shard.py` + EC2 orchestrator
- [ ] LiteParse all annual reports (3A + 3B only; no LLM yet)
- [ ] Progress manifest + SNS

### Sprint 4 — Scale 3C
- [ ] Batch enrich in chunks (500 docs)
- [ ] CFO corpus through full pipeline
- [ ] Stage 4 handoff: `enriched/tables.json` → temporal graph ingest

### Parallel (ongoing)
- [ ] Probe + fetch Appendix 4G
- [ ] Document fetch expansion in `fetch-expansion-types.md`

---

## Open decisions (need owner input)

| # | Decision | Options | Default recommendation |
|---|----------|---------|------------------------|
| 1 | Mastra vs plain Gemini SDK | Mastra workflows / Python SDK only | Mastra for 3C if team knows TS; Python SDK for v0 proof in 48h |
| 2 | Gemini model mix | Flash / Pro / 2.5 Flash | Flash classify + clean; Pro table extract |
| 3 | OCR policy | Off globally / on for scanned pages / per classify | Off in 3A; enable in 3B when classify detects scan |
| 4 | Page PNG DPI | 150 / 300 | 200 DPI balance |
| 5 | Pilot ticker list | 10 from `pilot_tickers.txt` | CBA, BHP, WOW, TLS, GMG + 5 mid-cap |
| 6 | Enrich scope for CFO docs | Full pipeline / tables-only / skip 3C | Light 3C (clean only; tables optional) — CFO docs are 1–2 pages |
| 7 | Where Mastra runs | Same VM as parse / separate service / Vercel | Separate small VM or local Docker for pilot |

---

## Success criteria

| Gate | Metric |
|------|--------|
| Pilot complete | 10 tickers through 3A–3C; <5% page failures |
| Table quality | Manual review: ≥80% of income statement line items correct on 20 tables |
| Resume | Kill worker mid-doc; restart resumes from `state.json` without rework |
| Scale 3A | 22,560 annual PDFs LiteParse + split; <1% fail |
| Cost | Document $/PDF enrich cost from pilot; extrapolate before full 3C |

---

## References

- [LiteParse](https://github.com/run-llama/liteparse) · [Mastra](https://mastra.ai/)
- Pilot bundle: `data/parse-sample-corpus/`
- LiteParse experiment: `0-work/experiments/liteparse-sample/`
- CFO signals: `0-work/docs/cfo-change-signals.md`
- Announcement types: `0-work/docs/announcements-schema.md`
- Prior parse draft: `0-work/docs/parse-spec.md`
