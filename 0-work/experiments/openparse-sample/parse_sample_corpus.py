#!/usr/bin/env python3
"""Parse page-count sample PDFs (annual reports + CFO changes) with open-parse.

Reads S3 keys from data/pdf_page_counts.json, downloads PDFs from S3, parses with
open-parse, and writes per-document JSON + markdown previews under output/.

Designed to run on a VM (see run_vm.sh). Default pipeline is no-op to avoid PIL
errors on some annual reports; see 0-work/experiments/open-parse/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import openparse
from openparse.doc_parser import DocumentParser

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
DEFAULT_MANIFEST = WORKSPACE / "data" / "pdf_page_counts.json"
CACHE_DIR = ROOT / "cache" / "pdfs"
OUTPUT_DIR = ROOT / "output"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="JSON from 19_count_s3_pdf_pages.py (default: data/pdf_page_counts.json)",
    )
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
        help="S3 bucket (default: GYPSY_S3_BUCKET)",
    )
    parser.add_argument(
        "--corpus",
        choices=("annual", "cfo", "all"),
        default="all",
        help="Which corpus entries to parse",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max PDFs per corpus (0 = all entries in manifest)",
    )
    parser.add_argument(
        "--only",
        nargs="*",
        help="Parse only these tickers (e.g. --only 360 BHP)",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Assume PDFs already in cache/pdfs/",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip documents whose output JSON already exists",
    )
    parser.add_argument(
        "--ml-tables",
        action="store_true",
        help="Use openparse[ml] unitable (pip install 'openparse[ml]' + openparse-download)",
    )
    parser.add_argument(
        "--basic-pipeline",
        action="store_true",
        help="Run BasicIngestionPipeline (can fail on some PDFs with PIL errors)",
    )
    parser.add_argument("--min-table-confidence", type=float, default=0.8)
    return parser.parse_args()


def aws_cmd_bytes(*parts: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    return subprocess.run(
        ["aws", *parts, "--no-cli-pager"],
        capture_output=True,
        env=env,
    )


def load_manifest(path: Path, *, corpus: str) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for block in payload.get("corpora") or []:
        name = block.get("name")
        if corpus != "all" and name != corpus:
            continue
        for entry in block.get("entries") or []:
            rows.append(
                {
                    "corpus": name,
                    "key": entry["key"],
                    "pages": entry.get("pages"),
                    "bytes": entry.get("bytes"),
                }
            )
    return rows


def slug_from_key(key: str) -> tuple[str, str, str]:
    """Return (corpus, ticker, slug) from an S3 key."""
    parts = key.split("/")
    if len(parts) < 4 or parts[0] != "entities":
        raise ValueError(f"unexpected S3 key: {key}")
    ticker = parts[1]
    folder = parts[2]
    basename = parts[-1].removesuffix(".pdf")
    if folder == "annual_reports":
        corpus = "annual"
    elif folder == "cfo_changes":
        corpus = "cfo"
    else:
        raise ValueError(f"unexpected folder in key: {key}")
    slug = f"{ticker}_{basename}"
    return corpus, ticker, slug


def cache_path_for_key(key: str) -> Path:
    return CACHE_DIR / key.replace("/", "__")


def download_pdf(bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = aws_cmd_bytes("s3", "cp", f"s3://{bucket}/{key}", str(dest))
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"s3 cp failed: {key}")


def node_payload(node: object) -> dict:
    if hasattr(node, "model_dump"):
        data = node.model_dump(mode="json")
    elif hasattr(node, "dict"):
        data = node.dict()
    else:
        data = {"repr": repr(node)}
    text = data.get("text") or getattr(node, "text", "") or ""
    data["text_chars"] = len(text)
    data["text_preview"] = text[:500].replace("\n", " ")
    return data


def is_table_like(text: str) -> bool:
    return "|" in text and "---" in text


def parse_pdf(
    pdf_path: Path,
    *,
    use_ml_tables: bool,
    min_table_confidence: float,
    use_basic_pipeline: bool,
) -> dict:
    table_args = None
    if use_ml_tables:
        table_args = {
            "parsing_algorithm": "unitable",
            "min_table_confidence": min_table_confidence,
        }
    pipeline = (
        openparse.processing.BasicIngestionPipeline()
        if use_basic_pipeline
        else None
    )
    parser = DocumentParser(processing_pipeline=pipeline, table_args=table_args)
    started = time.monotonic()
    parsed = parser.parse(str(pdf_path))
    elapsed = time.monotonic() - started

    nodes = [node_payload(n) for n in parsed.nodes]
    table_nodes = [n for n in nodes if is_table_like(n.get("text") or "")]
    total_chars = sum(int(n.get("text_chars", 0)) for n in nodes)

    return {
        "elapsed_s": round(elapsed, 1),
        "node_count": len(nodes),
        "table_like_nodes": len(table_nodes),
        "total_text_chars": total_chars,
        "nodes": nodes,
    }


def write_markdown_preview(result: dict, path: Path) -> None:
    lines = [
        f"# {result['slug']}",
        "",
        f"- Corpus: {result['corpus']}",
        f"- S3 key: `{result['s3_key']}`",
        f"- Pages (manifest): {result.get('pages')}",
        f"- Nodes: {result['node_count']}",
        f"- Table-like nodes: {result['table_like_nodes']}",
        f"- Total chars: {result['total_text_chars']:,}",
        f"- Parse time: {result['elapsed_s']}s",
        "",
        "## First 5 nodes",
        "",
    ]
    for i, node in enumerate(result["nodes"][:5]):
        lines.append(f"### Node {i + 1}")
        lines.append("")
        text = node.get("text") or node.get("text_preview") or ""
        preview = text[:1200]
        lines.append(preview)
        if len(text) > 1200:
            lines.append("\n…")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if not args.manifest.exists():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    if not args.skip_download and not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2

    entries = load_manifest(args.manifest, corpus=args.corpus)
    if args.only:
        tickers = {t.upper() for t in args.only}
        entries = [e for e in entries if slug_from_key(str(e["key"]))[1] in tickers]

    if args.limit > 0:
        limited: list[dict[str, object]] = []
        per_corpus: dict[str, int] = {}
        for entry in entries:
            corpus = str(entry["corpus"])
            if per_corpus.get(corpus, 0) >= args.limit:
                continue
            limited.append(entry)
            per_corpus[corpus] = per_corpus.get(corpus, 0) + 1
        entries = limited

    print(
        f"open-parse sample corpus: {len(entries)} PDFs "
        f"(corpus={args.corpus}, pipeline={'basic' if args.basic_pipeline else 'noop'})"
    )

    results: list[dict[str, object]] = []
    ok = skip = fail = 0

    for entry in entries:
        key = str(entry["key"])
        corpus, ticker, slug = slug_from_key(key)
        out_dir = OUTPUT_DIR / corpus
        out_json = out_dir / f"{slug}.json"
        out_md = out_dir / f"{slug}.md"

        if args.resume and out_json.exists():
            skip += 1
            print(f"SKIP resume {slug}")
            continue

        cache_path = cache_path_for_key(key)
        print(f"Parse {slug} ({corpus}) ...", flush=True)
        try:
            if not args.skip_download:
                if not cache_path.exists():
                    download_pdf(args.bucket, key, cache_path)
            elif not cache_path.exists():
                raise FileNotFoundError(f"missing cached PDF: {cache_path}")

            parsed = parse_pdf(
                cache_path,
                use_ml_tables=args.ml_tables,
                min_table_confidence=args.min_table_confidence,
                use_basic_pipeline=args.basic_pipeline,
            )
            result = {
                "slug": slug,
                "ticker": ticker,
                "corpus": corpus,
                "s3_key": key,
                "pages": entry.get("pages"),
                "bytes": entry.get("bytes"),
                "pipeline": "basic" if args.basic_pipeline else "noop",
                "ml_tables": args.ml_tables,
                **parsed,
                "status": "ok",
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            out_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            write_markdown_preview(result, out_md)
            results.append(
                {
                    "slug": slug,
                    "corpus": corpus,
                    "ticker": ticker,
                    "s3_key": key,
                    "node_count": parsed["node_count"],
                    "table_like_nodes": parsed["table_like_nodes"],
                    "total_text_chars": parsed["total_text_chars"],
                    "elapsed_s": parsed["elapsed_s"],
                    "status": "ok",
                }
            )
            ok += 1
            print(
                f"  OK nodes={parsed['node_count']} tables={parsed['table_like_nodes']} "
                f"chars={parsed['total_text_chars']} time={parsed['elapsed_s']}s"
            )
        except Exception as exc:  # noqa: BLE001
            fail += 1
            results.append(
                {
                    "slug": slug,
                    "corpus": corpus,
                    "ticker": ticker,
                    "s3_key": key,
                    "status": "error",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            print(f"  ERROR {exc}", file=sys.stderr)

    summary = {
        "parser": "open-parse",
        "manifest": str(args.manifest),
        "corpus_filter": args.corpus,
        "pipeline": "basic" if args.basic_pipeline else "noop",
        "ml_tables": args.ml_tables,
        "parsed": ok,
        "skipped": skip,
        "failed": fail,
        "results": results,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = OUTPUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {summary_path} (ok={ok} skip={skip} fail={fail})")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
