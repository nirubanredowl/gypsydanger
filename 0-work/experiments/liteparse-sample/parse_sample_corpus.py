#!/usr/bin/env python3
"""Parse page-count sample PDFs (annual reports + CFO changes) with LiteParse.

Reads S3 keys from data/pdf_page_counts.json, downloads PDFs from S3, parses with
LiteParse (https://github.com/run-llama/liteparse), and writes per-document JSON
+ markdown under output/.

Designed to run on a VM (see run_vm.sh). LiteParse runs locally — no LlamaParse
credits required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from liteparse import LiteParse

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
        help="JSON from 19_count_s3_pdf_pages.py",
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
    )
    parser.add_argument("--limit", type=int, default=0, help="Max PDFs per corpus")
    parser.add_argument("--only", nargs="*", help="Only these tickers")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--output-format",
        choices=("markdown", "text", "json"),
        default="markdown",
        help="LiteParse output format (default: markdown)",
    )
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="Enable OCR for scanned pages / embedded images",
    )
    parser.add_argument(
        "--emit-word-boxes",
        action="store_true",
        help="Include per-word bounding boxes on text items",
    )
    parser.add_argument(
        "--include-text-items",
        action="store_true",
        help="Store per-page text_items in JSON (larger files)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="LiteParse max_pages (0 = all pages)",
    )
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
    return corpus, ticker, f"{ticker}_{basename}"


def cache_path_for_key(key: str) -> Path:
    return CACHE_DIR / key.replace("/", "__")


def download_pdf(bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = aws_cmd_bytes("s3", "cp", f"s3://{bucket}/{key}", str(dest))
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"s3 cp failed: {key}")


def serialize_text_item(item: object, *, emit_word_boxes: bool) -> dict[str, Any]:
    words = getattr(item, "words", []) or []
    payload = {
        "text": getattr(item, "text", ""),
        "x": getattr(item, "x", None),
        "y": getattr(item, "y", None),
        "width": getattr(item, "width", None),
        "height": getattr(item, "height", None),
        "font_name": getattr(item, "font_name", None),
        "font_size": getattr(item, "font_size", None),
        "confidence": getattr(item, "confidence", None),
        "rotation": getattr(item, "rotation", None),
    }
    if emit_word_boxes:
        payload["words"] = [
            {
                "text": getattr(w, "text", ""),
                "x": getattr(w, "x", None),
                "y": getattr(w, "y", None),
                "width": getattr(w, "width", None),
                "height": getattr(w, "height", None),
            }
            for w in words
        ]
    else:
        payload["word_count"] = len(words)
    return payload


def serialize_page(
    page: object, *, include_text_items: bool, emit_word_boxes: bool
) -> dict[str, Any]:
    text = getattr(page, "text", "") or ""
    markdown = getattr(page, "markdown", "") or ""
    items = getattr(page, "text_items", []) or []
    payload: dict[str, Any] = {
        "page_num": getattr(page, "page_num", None),
        "width": getattr(page, "width", None),
        "height": getattr(page, "height", None),
        "text_chars": len(text),
        "markdown_chars": len(markdown),
        "text_item_count": len(items),
        "text_preview": text[:500],
    }
    if include_text_items:
        payload["text_items"] = [
            serialize_text_item(item, emit_word_boxes=emit_word_boxes) for item in items
        ]
    return payload


def count_markdown_tables(text: str) -> int:
    """Rough count of markdown pipe tables."""
    blocks = re.split(r"\n\s*\n", text)
    tables = 0
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) >= 2 and "|" in lines[0] and re.match(r"^\|?[\s\-:|]+\|?$", lines[1]):
            tables += 1
    return tables


def parse_pdf(
    pdf_path: Path,
    *,
    output_format: str,
    ocr_enabled: bool,
    emit_word_boxes: bool,
    max_pages: int,
    include_text_items: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "output_format": output_format,
        "quiet": True,
        "ocr_enabled": ocr_enabled,
        "emit_word_boxes": emit_word_boxes,
        "image_mode": "placeholder",
        "extract_links": True,
    }
    if max_pages > 0:
        kwargs["max_pages"] = max_pages

    parser = LiteParse(**kwargs)
    started = time.monotonic()
    result = parser.parse(pdf_path.read_bytes())
    elapsed = time.monotonic() - started

    text = result.text or ""
    pages = [
        serialize_page(
            page,
            include_text_items=include_text_items,
            emit_word_boxes=emit_word_boxes,
        )
        for page in result.pages
    ]
    images = [
        {
            "page_num": getattr(img, "page_num", None),
            "width": getattr(img, "width", None),
            "height": getattr(img, "height", None),
            "format": getattr(img, "format", None),
        }
        for img in (result.images or [])
    ]

    return {
        "elapsed_s": round(elapsed, 1),
        "page_count": len(result.pages),
        "text_chars": len(text),
        "markdown_tables": count_markdown_tables(text),
        "image_count": len(images),
        "text": text,
        "pages": pages,
        "images": images,
    }


def write_outputs(result: dict[str, Any], out_json: Path, out_md: Path) -> None:
    json_payload = {
        k: v
        for k, v in result.items()
        if k != "text"
    }
    json_payload["text_preview"] = (result.get("text") or "")[:2000]
    out_json.write_text(json.dumps(json_payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# {result['slug']}",
        "",
        f"- Corpus: {result['corpus']}",
        f"- S3 key: `{result['s3_key']}`",
        f"- Pages: {result.get('page_count')}",
        f"- Text chars: {result.get('text_chars'):,}",
        f"- Markdown tables (heuristic): {result.get('markdown_tables')}",
        f"- Parse time: {result.get('elapsed_s')}s",
        "",
        "## Parsed content",
        "",
        result.get("text") or "",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")


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
        f"LiteParse sample corpus: {len(entries)} PDFs "
        f"(corpus={args.corpus}, format={args.output_format}, ocr={args.ocr})"
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
                output_format=args.output_format,
                ocr_enabled=args.ocr,
                emit_word_boxes=args.emit_word_boxes,
                max_pages=args.max_pages,
                include_text_items=args.include_text_items,
            )
            result = {
                "slug": slug,
                "ticker": ticker,
                "corpus": corpus,
                "s3_key": key,
                "pages": entry.get("pages"),
                "bytes": entry.get("bytes"),
                "parser": "liteparse",
                "output_format": args.output_format,
                "ocr_enabled": args.ocr,
                **parsed,
                "status": "ok",
            }
            out_dir.mkdir(parents=True, exist_ok=True)
            write_outputs(result, out_json, out_md)
            results.append(
                {
                    "slug": slug,
                    "corpus": corpus,
                    "ticker": ticker,
                    "s3_key": key,
                    "page_count": parsed["page_count"],
                    "text_chars": parsed["text_chars"],
                    "markdown_tables": parsed["markdown_tables"],
                    "elapsed_s": parsed["elapsed_s"],
                    "status": "ok",
                }
            )
            ok += 1
            print(
                f"  OK pages={parsed['page_count']} tables={parsed['markdown_tables']} "
                f"chars={parsed['text_chars']} time={parsed['elapsed_s']}s"
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
        "parser": "liteparse",
        "manifest": str(args.manifest),
        "corpus_filter": args.corpus,
        "output_format": args.output_format,
        "ocr_enabled": args.ocr,
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
