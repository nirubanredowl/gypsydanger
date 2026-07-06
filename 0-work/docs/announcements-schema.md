# Announcements CSV schema

Reference for columns and `announcementTypes` tags on `data/entities/{TICKER}/{TICKER}_Announcements.csv`. Rows are written by `02_index_announcements.py` from the Markit Digital announcements API.

API URLs and pagination: [`links.md`](links.md).

---

## Columns

| Column | Source API field | Description |
|--------|------------------|-------------|
| `ticker` | (derived) | ASX code from `entities.csv` |
| `entity_xid` | (derived) | Company id used to paginate `markets/announcements` |
| `documentKey` | `documentKey` | CDN file id — use to download the PDF |
| `date` | `date` | Announcement datetime (ISO 8601) |
| `headline` | `headline` | Human-readable title shown on ASX |
| `fileSize` | `fileSize` | Declared file size string from API |
| `isPriceSensitive` | `isPriceSensitive` | `true` / `false` — price-sensitive flag |
| `symbol` | `symbol` | Primary symbol on the announcement |
| `url` | `url` | ASX web URL for the announcement page |
| `announcementTypes` | `announcementTypes` | JSON array of Markit classification strings |
| `companies` | `companies` | JSON array of related company names |
| `companyInfo` | `companyInfo` | JSON array of objects (`symbol`, `xidEntity`, …) |
| `symbolsSecondary` | `symbolsSecondary` | JSON array of additional ASX codes on the filing |

Each JSON column is stored as a UTF-8 JSON string in the CSV (same encoding as returned by the API).

---

## `announcementTypes` — what the tags mean

`announcementTypes` is a **multi-label classifier**. One PDF can carry several tags (for example an Appendix 4E bundle tagged with both `Annual Report` and `Full Year Accounts`).

### Tags used for annual report filtering

| Tag | Typical meaning |
|-----|-----------------|
| `Annual Report` | Primary signal for statutory annual report PDFs. Also appears on some summaries, US Form 20-F filings, sustainability reports, and other edge cases — do not use alone without refinement. |
| `Full Year Accounts` | Full-year financial statements (often part of Appendix 4E) |
| `Full Year Audit Review` | Auditor's report on full-year accounts |
| `Full Year Directors' Report` | Directors' report for the financial year |
| `Full Year Directors' Statement` | Directors' declaration / statement |
| `Preliminary Final Report` | Preliminary final report (often co-released with annual report) |
| `Top 20 shareholders` | Top-20 shareholder schedule bundled with annual report |

### Common non-annual tags on otherwise “annual” rows

| Tag | Why it matters |
|-----|----------------|
| `Periodic Reports - Other` | US Form 20-F, sustainability summaries, economic contribution reports |
| `Company Administration - Other` | Admin updates mis-tagged with `Annual Report` |
| `Non-Renounceable Issue` | Offer documents occasionally co-tagged |
| `Dividend Pay Date` / `Dividend Record Date` / `Dividend Rate` | Dividend metadata bundled into Appendix 4E announcements |
| `Sustainability/Climate Action Report` | ESG report released alongside the annual report |
| `Corporate Governance` | Governance statement bundled with annual report |

These co-tags are informational. The fetch filter uses headline and full-year co-tags to drop obvious false positives.

---

## Annual report filter (implemented)

Helpers live in `00_asx_api.py`:

- `parse_announcement_types(row["announcementTypes"])`
- `is_annual_report_announcement(row, mode="strict"|"loose")`

### `loose`

Include when `"Annual Report"` appears in `announcementTypes`.

Probe on sample tickers (CBA, BHP, WOW, QGL, TLS): **112 rows** out of **12,642** announcement rows (~0.9%).

### `strict` (default for analysis probes)

Include when:

1. `"Annual Report"` is in `announcementTypes`, **and**
2. Headline does **not** match excluded patterns (Shareholder Review/Update, Form 20-F, sustainability/summary reports, rights issues, etc.), **and**
3. Either:
   - at least one [full-year co-tag](#tags-used-for-annual-report-filtering) is present, **or**
   - headline matches `annual report` or `appendix 4e` (covers older filings with sparse tagging, e.g. WOW 2012).

Same sample tickers: **78 strict rows** (~0.6%). Full indexed corpus (Jul 2026, 1,838 tickers): **22,128 strict** / **22,573 loose** annual report PDFs vs **1,260,035** total announcements (~1.8%).

### Fetch usage

```bash
# Dry-run sizing first
python3 0-work/scripts/10_probe_annual_reports.py --all-indexed

# Fetch annual reports only (loose filter — default when --annual-reports-only is set)
python3 0-work/scripts/03_fetch_documents.py --annual-reports-only --ticker CBA

# Stricter filter (fewer false positives)
python3 0-work/scripts/03_fetch_documents.py --annual-reports-only --annual-filter strict
```

---

## Probe script

`10_probe_annual_reports.py` reports per-ticker counts and optional excluded samples:

```bash
python3 0-work/scripts/10_probe_annual_reports.py --show-excluded
python3 0-work/scripts/10_probe_annual_reports.py --all-indexed --json
```
