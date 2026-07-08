# open-parse — page-count sample corpus

Parse the **100 sampled PDFs** from `data/pdf_page_counts.json` (50 annual reports + 50 CFO change notices) using [open-parse](https://github.com/Filimoa/open-parse/) — a local alternative to LlamaParse.

Separate from `0-work/experiments/open-parse/` (the eight hand-picked annual reports in parse-spec).

## Layout

```
openparse-sample/
  parse_sample_corpus.py   # main parser
  run_vm.sh                # VM entrypoint (venv + deps + run)
  cache/pdfs/              # downloaded PDFs (gitignored)
  output/
    annual/                # per-document JSON + markdown preview
    cfo/
    summary.json           # run aggregate
```

## VM setup

```bash
cd 0-work/experiments/openparse-sample
chmod +x run_vm.sh

# Full sample (100 PDFs; annual reports are slow — ~2,500 pages total)
./run_vm.sh

# Quick smoke test
./run_vm.sh --corpus cfo --limit 5

# Annual reports only
./run_vm.sh --corpus annual

# Resume after interruption
./run_vm.sh --resume
```

Requires `GYPSY_S3_BUCKET` and AWS credentials in `0-work/scripts/.env` (or environment).

## Options

| Flag | Default | Effect |
|------|---------|--------|
| `--manifest` | `data/pdf_page_counts.json` | Input key list |
| `--corpus` | `all` | `annual`, `cfo`, or `all` |
| `--limit N` | 0 (all) | Max PDFs **per corpus** |
| `--only TICKER …` | — | Filter by ticker |
| `--resume` | off | Skip docs with existing `output/{corpus}/{slug}.json` |
| `--skip-download` | off | Use PDFs already in `cache/pdfs/` |
| `--basic-pipeline` | off | `BasicIngestionPipeline` (can hit PIL errors) |
| `--ml-tables` | off | Unitable (`openparse[ml]` + `openparse-download`) |

**Default pipeline is no-op** (`processing_pipeline=None`) — same lesson as the parse-spec experiment: several annual reports crash `BasicIngestionPipeline` with `PIL.UnidentifiedImageError`.

## Output

Per document:

| File | Contents |
|------|----------|
| `output/{corpus}/{TICKER}_{basename}.json` | Full open-parse nodes (text, bbox, variants) |
| `output/{corpus}/{TICKER}_{basename}.md` | Human preview (first 5 nodes) |

Run summary: `output/summary.json`

## Production notes

- Run on a VM with ~4 GB RAM; annual-report samples can be large (up to ~15 MB / 80+ pages).
- Parsing is **sequential** (one PDF at a time) — safe for open-parse / pdfminer.
- Re-run with `--resume` after VM preemption.
- For table-heavy Parse B, try `--ml-tables` after `pip install "openparse[ml]"` and `openparse-download`.

## Regenerate the input manifest

```bash
cd 0-work/scripts
python3 19_count_s3_pdf_pages.py --sample 50
```
