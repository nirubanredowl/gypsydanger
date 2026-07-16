#!/usr/bin/env python3
"""LiteParse one PDF and write Stage 3A artefacts (local dir or S3)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

try:
    from liteparse import LiteParse
except ImportError:
    LiteParse = None  # type: ignore[misc, assignment]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--document-key", required=True)
    parser.add_argument("--corpus-key", default="annual_reports")
    parser.add_argument("--bucket", default=os.environ.get("GYPSY_S3_BUCKET", ""))
    parser.add_argument(
        "--pdf-key",
        help="S3 PDF key (default: derived from ticker/date/document-key)",
    )
    parser.add_argument("--published-date", default="")
    parser.add_argument("--headline", default="")
    parser.add_argument("--entity-xid", default="")
    parser.add_argument("--announcement-types", default="")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Write locally instead of uploading to S3",
    )
    parser.add_argument(
        "--pdf-file",
        type=Path,
        help="Local PDF path (skips S3 download)",
    )
    parser.add_argument("--skip-if-exists", action="store_true")
    parser.add_argument("--ocr", action="store_true")
    return parser.parse_args()


def aws_cmd(*parts: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    return subprocess.run(
        ["aws", *parts, "--no-cli-pager"],
        capture_output=True,
        text=True,
        env=env,
    )


def s3_object_exists(bucket: str, key: str) -> bool:
    return aws_cmd("s3api", "head-object", "--bucket", bucket, "--key", key).returncode == 0


def s3_download_bytes(bucket: str, key: str) -> bytes:
    return _download_binary(bucket, key)


def _download_binary(bucket: str, key: str) -> bytes:
    tmp = Path(f"/tmp/gypsy-pdf-{os.getpid()}.pdf")
    result = aws_cmd("s3", "cp", f"s3://{bucket}/{key}", str(tmp))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 download failed: {key}")
    try:
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def s3_sync_dir(bucket: str, local_dir: Path, s3_prefix: str) -> None:
    result = aws_cmd(
        "s3",
        "sync",
        str(local_dir),
        f"s3://{bucket}/{s3_prefix}",
        "--only-show-errors",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 sync failed: {s3_prefix}")


def serialize_text_item(item: object) -> dict[str, Any]:
    words = getattr(item, "words", []) or []
    return {
        "text": getattr(item, "text", ""),
        "x": getattr(item, "x", None),
        "y": getattr(item, "y", None),
        "width": getattr(item, "width", None),
        "height": getattr(item, "height", None),
        "font_name": getattr(item, "font_name", None),
        "font_size": getattr(item, "font_size", None),
        "confidence": getattr(item, "confidence", None),
        "rotation": getattr(item, "rotation", None),
        "word_count": len(words),
    }


def serialize_page(page: object) -> dict[str, Any]:
    items = getattr(page, "text_items", []) or []
    text = getattr(page, "text", "") or ""
    markdown = getattr(page, "markdown", "") or ""
    return {
        "page_num": getattr(page, "page_num", None),
        "width": getattr(page, "width", None),
        "height": getattr(page, "height", None),
        "text_chars": len(text),
        "markdown_chars": len(markdown),
        "text_item_count": len(items),
        "text_items": [serialize_text_item(item) for item in items],
    }


def count_markdown_tables(text: str) -> int:
    blocks = re.split(r"\n\s*\n", text)
    tables = 0
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) >= 2 and "|" in lines[0] and re.match(r"^\|?[\s\-:|]+\|?$", lines[1]):
            tables += 1
    return tables


def liteparse_version() -> str:
    try:
        import liteparse as lp  # noqa: PLC0415

        return getattr(lp, "__version__", "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def run_liteparse(pdf_bytes: bytes, *, ocr_enabled: bool) -> dict[str, Any]:
    if LiteParse is None:
        raise RuntimeError("liteparse not installed — pip install liteparse")

    parser = LiteParse(
        output_format="markdown",
        quiet=True,
        ocr_enabled=ocr_enabled,
        image_mode="placeholder",
        extract_links=True,
    )
    started = time.monotonic()
    result = parser.parse(pdf_bytes)
    elapsed = time.monotonic() - started

    pages = [serialize_page(page) for page in result.pages]
    full_text = result.text or ""
    page_markdowns = [
        (getattr(page, "page_num", idx + 1), getattr(page, "markdown", "") or "")
        for idx, page in enumerate(result.pages)
    ]

    return {
        "elapsed_s": round(elapsed, 2),
        "page_count": len(result.pages),
        "text_chars": len(full_text),
        "markdown_tables": count_markdown_tables(full_text),
        "full_text": full_text,
        "pages": pages,
        "page_markdowns": page_markdowns,
        "liteparse_version": liteparse_version(),
    }


def build_meta(
    *,
    ticker: str,
    document_key: str,
    corpus_key: str,
    published_date: str,
    headline: str,
    entity_xid: str,
    announcement_types: str,
    s3_pdf_key: str,
    parse_result: dict[str, Any],
) -> dict[str, Any]:
    corpus_id, parse_folder, fetch_folder = asx.corpus_info(corpus_key)
    types = sorted(asx.parse_announcement_types(announcement_types)) if announcement_types else []
    return {
        "document_key": document_key,
        "ticker": ticker.upper(),
        "entity_xid": entity_xid or None,
        "corpus_id": corpus_id,
        "corpus_key": corpus_key,
        "parse_folder": parse_folder,
        "fetch_s3_folder": fetch_folder,
        "published_date": (published_date or "")[:10] or None,
        "headline": headline or None,
        "announcement_types": types,
        "s3_pdf_key": s3_pdf_key,
        "reporting_period": None,
        "page_count": parse_result["page_count"],
        "liteparse_version": parse_result["liteparse_version"],
        "markdown_tables": parse_result["markdown_tables"],
        "parsed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_state(*, parse_result: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "3a_liteparse": {
            "status": "complete",
            "at": now,
            "elapsed_s": parse_result["elapsed_s"],
            "page_count": parse_result["page_count"],
        },
        "3b_split": {"status": "pending"},
        "3c_flash": {"status": "pending"},
    }


def write_document_outputs(
    output_root: Path,
    *,
    meta: dict[str, Any],
    state: dict[str, Any],
    parse_result: dict[str, Any],
) -> Path:
    liteparse_dir = output_root / "liteparse"
    pages_dir = liteparse_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    for page in parse_result["pages"]:
        page_num = int(page["page_num"] or 0)
        page_id = f"{page_num:04d}"
        page_dir = pages_dir / page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.json").write_text(
            json.dumps(page, indent=2) + "\n", encoding="utf-8"
        )

    for page_num, markdown in parse_result["page_markdowns"]:
        page_id = f"{int(page_num):04d}"
        page_dir = pages_dir / page_id
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.md").write_text(markdown, encoding="utf-8")

    (liteparse_dir / "document.md").write_text(
        parse_result["full_text"], encoding="utf-8"
    )
    manifest = {
        "page_count": parse_result["page_count"],
        "markdown_tables": parse_result["markdown_tables"],
        "text_chars": parse_result["text_chars"],
        "elapsed_s": parse_result["elapsed_s"],
        "liteparse_version": parse_result["liteparse_version"],
        "pages_prefix": "pages/",
    }
    (liteparse_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (output_root / "meta.json").write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    (output_root / "state.json").write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )
    return liteparse_dir / "manifest.json"


def parse_document(
    *,
    bucket: str,
    ticker: str,
    document_key: str,
    corpus_key: str,
    s3_pdf_key: str,
    published_date: str = "",
    headline: str = "",
    entity_xid: str = "",
    announcement_types: str = "",
    output_dir: Path | None = None,
    pdf_file: Path | None = None,
    skip_if_exists: bool = False,
    ocr_enabled: bool = False,
) -> dict[str, Any]:
    ticker = ticker.upper()
    manifest_key = asx.s3_liteparse_manifest_key(ticker, corpus_key, document_key)
    doc_prefix = asx.s3_parsed_doc_prefix(ticker, corpus_key, document_key)

    if skip_if_exists and bucket and s3_object_exists(bucket, manifest_key):
        return {
            "status": "skipped",
            "ticker": ticker,
            "document_key": document_key,
            "s3_manifest_key": manifest_key,
        }

    if pdf_file:
        pdf_bytes = pdf_file.read_bytes()
    elif bucket:
        pdf_bytes = s3_download_bytes(bucket, s3_pdf_key)
    else:
        raise ValueError("bucket or pdf_file required")

    parse_result = run_liteparse(pdf_bytes, ocr_enabled=ocr_enabled)
    meta = build_meta(
        ticker=ticker,
        document_key=document_key,
        corpus_key=corpus_key,
        published_date=published_date,
        headline=headline,
        entity_xid=entity_xid,
        announcement_types=announcement_types,
        s3_pdf_key=s3_pdf_key,
        parse_result=parse_result,
    )
    state = build_state(parse_result=parse_result)

    if output_dir:
        write_document_outputs(output_dir, meta=meta, state=state, parse_result=parse_result)
        return {
            "status": "ok",
            "ticker": ticker,
            "document_key": document_key,
            "output_dir": str(output_dir),
            **{k: parse_result[k] for k in ("page_count", "elapsed_s", "markdown_tables")},
        }

    work = Path(f"/tmp/gypsy-parse-{os.getpid()}")
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        write_document_outputs(work, meta=meta, state=state, parse_result=parse_result)
        s3_sync_dir(bucket, work, doc_prefix)
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    return {
        "status": "ok",
        "ticker": ticker,
        "document_key": document_key,
        "s3_prefix": doc_prefix,
        "s3_manifest_key": manifest_key,
        **{k: parse_result[k] for k in ("page_count", "elapsed_s", "markdown_tables")},
    }


def main() -> int:
    args = parse_args()
    if not args.bucket and not args.output_dir and not args.pdf_file:
        print("error: --bucket, --output-dir, or --pdf-file required", file=sys.stderr)
        return 2

    pdf_key = args.pdf_key
    if not pdf_key:
        if args.corpus_key == "annual_reports":
            pdf_key = asx.s3_annual_report_key(
                args.ticker, args.published_date, args.document_key
            )
        elif args.corpus_key == "cfo_changes":
            pdf_key = asx.s3_cfo_change_key(
                args.ticker, args.published_date, args.document_key
            )
        else:
            print("error: --pdf-key required for this corpus", file=sys.stderr)
            return 2

    output_dir = args.output_dir
    if output_dir:
        output_dir = (
            output_dir
            / args.ticker.upper()
            / asx.corpus_info(args.corpus_key)[1]
            / asx.parsed_doc_folder(args.document_key)
        )

    result = parse_document(
        bucket=args.bucket or "",
        ticker=args.ticker,
        document_key=args.document_key,
        corpus_key=args.corpus_key,
        s3_pdf_key=pdf_key,
        published_date=args.published_date,
        headline=args.headline,
        entity_xid=args.entity_xid,
        announcement_types=args.announcement_types,
        output_dir=output_dir,
        pdf_file=args.pdf_file,
        skip_if_exists=args.skip_if_exists,
        ocr_enabled=args.ocr,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
