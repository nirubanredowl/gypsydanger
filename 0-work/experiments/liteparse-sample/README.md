# LiteParse — page-count sample corpus

Parse the **100 sampled PDFs** from `data/pdf_page_counts.json` (50 annual reports + 50 CFO change notices) using [LiteParse](https://github.com/run-llama/liteparse) — local document parsing from LlamaIndex (no cloud credits).

Parallel experiment to:
- `0-work/experiments/openparse-sample/` (open-parse)
- `0-work/experiments/open-parse/` (eight hand-picked annual reports)

## Layout

```
liteparse-sample/
  parse_sample_corpus.py   # main parser
  run_vm.sh                # VM entrypoint
  cache/pdfs/              # downloaded PDFs (gitignored)
  output/
    annual/                # per-document JSON + markdown
    cfo/
    summary.json
```

## VM setup

```bash
cd 0-work/experiments/liteparse-sample
chmod +x run_vm.sh

# Full sample (100 PDFs)
./run_vm.sh

# Quick smoke test
./run_vm.sh --corpus cfo --limit 5

# Resume after interruption
./run_vm.sh --resume
```

Requires `GYPSY_S3_BUCKET` and AWS credentials in `0-work/scripts/.env`.

## Options

| Flag | Default | Effect |
|------|---------|--------|
| `--manifest` | `data/pdf_page_counts.json` | Input key list |
| `--corpus` | `all` | `annual`, `cfo`, or `all` |
| `--limit N` | 0 (all) | Max PDFs per corpus |
| `--only TICKER …` | — | Filter by ticker |
| `--resume` | off | Skip docs with existing output JSON |
| `--output-format` | `markdown` | `markdown`, `text`, or `json` |
| `--ocr` | off | Enable OCR for scanned pages |
| `--include-text-items` | off | Store spatial text items in JSON |
| `--emit-word-boxes` | off | Include word-level bboxes (with `--include-text-items`) |
| `--max-pages N` | 0 (all) | Cap pages parsed per PDF |

## Output

Per document:

| File | Contents |
|------|----------|
| `output/{corpus}/{slug}.json` | Stats, page summaries, text preview |
| `output/{corpus}/{slug}.md` | Full parsed markdown/text |

Run summary: `output/summary.json`

LiteParse markdown output includes headings, tables, and image placeholders — useful for comparing against open-parse and LlamaParse.

## Compare parsers on the same sample

```bash
# LiteParse
cd 0-work/experiments/liteparse-sample && ./run_vm.sh --corpus cfo --limit 10

# open-parse
cd 0-work/experiments/openparse-sample && ./run_vm.sh --corpus cfo --limit 10
```

Both read the same `data/pdf_page_counts.json` manifest.

## Production notes

- LiteParse runs fully local (Rust core via Python bindings).
- Default **no OCR** — faster; add `--ocr` for scanned annual reports.
- Annual-report sample is ~2,500 pages; CFO sample is ~70 pages.
- Use `--resume` on long VM runs.

## Regenerate input manifest

```bash
cd 0-work/scripts
python3 19_count_s3_pdf_pages.py --sample 50
```
