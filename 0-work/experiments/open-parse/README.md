# open-parse experiment — sample ASX annual reports

Parse the eight [parse-spec](../../docs/parse-spec.md) sample PDFs with [open-parse](https://github.com/Filimoa/open-parse/).

## Setup

```bash
cd 0-work/experiments/open-parse
pip install -r requirements.txt
```

Download PDFs from S3 (once):

```bash
set -a && source ../../scripts/.env && set +a
# see pdfs/ — already populated from Phase C bucket
```

## Run

```bash
python3 run_samples.py
```

Outputs land in `output/`:

| File | Contents |
|------|----------|
| `{TICKER}_{YEAR}.json` | Full node list (text, bbox, tokens, variants) |
| `{TICKER}_{YEAR}.md` | Human-readable preview (first 5 nodes) |
| `summary.json` | Aggregate stats per sample |

### Options

| Flag | Effect |
|------|--------|
| `--only CDM MCP` | Parse subset of tickers |
| `--basic-pipeline` | Use `BasicIngestionPipeline` (groups nodes; can hit PIL errors on some PDFs) |
| `--ml-tables` | Enable unitable (`pip install "openparse[ml]"` + `openparse-download`) |

Default pipeline is **no-op** (`processing_pipeline=None`) because several annual reports trigger `PIL.UnidentifiedImageError` under the basic pipeline.

## Notes from first run

- Core parser extracts layout-aware **nodes** with markdown-ish text and bounding boxes.
- **Table-like nodes** detected via markdown pipe syntax (`| … |` + `---`).
- GMG / HFR / MCP fail with `--basic-pipeline` on corrupt/embedded image bytes.
- PDFs often set “no text extraction” metadata; pdfminer ignores and proceeds.

See `output/summary.json` after each run.

## Results (2026-07-06, noop pipeline)

| Ticker | Nodes | Chars | Time | Markdown tables |
|--------|------:|------:|-----:|----------------:|
| CDM 2013 | 1,893 | 99k | 1.8s | 0 |
| A1N 2019 | 3,712 | 308k | 5.0s | 0 |
| MCP 2018 | 5,412 | 285k | 8.0s | 0 |
| ALK 2016 | 4,272 | 241k | 9.0s | 0 |
| UNT 2021 | 3,430 | 228k | 5.7s | 0 |
| HFR 2020 | 6,069 | 292k | 17.5s | 0 |
| GRR 2021 | 3,233 | 246k | 4.6s | 0 |
| GMG 2022 | 17,301 | 837k | 21.1s | 0 |

**Observations**

- Core parser runs on all 8 samples with `processing_pipeline=None` (no-op).
- `BasicIngestionPipeline` crashes on MCP/HFR/GMG with `PIL.UnidentifiedImageError`.
- Default run does **not** emit markdown pipe tables; financial tables stay as fragmented text nodes.
- Older PDFs (CDM 2013) show heavy fragmentation (single characters / vertical text).
- For Parse B table extraction, likely need `openparse[ml]` + `unitable` or a dedicated table pass.

Preview markdown summaries: `output/{TICKER}_{YEAR}.md` (gitignored JSON holds full nodes).

