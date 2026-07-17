#!/usr/bin/env python3
"""Stage 3B — split source PDF into per-page PDF/PNG + LiteParse slice on S3."""

from __future__ import annotations

import argparse
import json
import os
import shutil
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
    import fitz  # PyMuPDF
except ImportError:
    fitz = None  # type: ignore[assignment]

DEFAULT_RENDER_DPI = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--document-key", required=True)
    parser.add_argument("--corpus-key", default="annual_reports")
    parser.add_argument("--bucket", default=os.environ.get("GYPSY_S3_BUCKET", ""))
    parser.add_argument("--s3-pdf-key", default="")
    parser.add_argument("--render-dpi", type=int, default=DEFAULT_RENDER_DPI)
    parser.add_argument("--skip-if-exists", action="store_true")
    parser.add_argument("--pdf-file", type=Path)
    parser.add_argument("--output-dir", type=Path)
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
    tmp = Path(f"/tmp/gypsy-split-{os.getpid()}.pdf")
    result = aws_cmd("s3", "cp", f"s3://{bucket}/{key}", str(tmp))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 download failed: {key}")
    try:
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


def s3_download_text(bucket: str, key: str) -> str:
    result = aws_cmd("s3", "cp", f"s3://{bucket}/{key}", "-")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 download failed: {key}")
    return result.stdout


def s3_upload_file(bucket: str, local: Path, key: str) -> None:
    result = aws_cmd("s3", "cp", str(local), f"s3://{bucket}/{key}")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 upload failed: {key}")


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


def s3_sync_prefix_down(bucket: str, s3_prefix: str, local_dir: Path) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    result = aws_cmd(
        "s3",
        "sync",
        f"s3://{bucket}/{s3_prefix}",
        str(local_dir),
        "--only-show-errors",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"s3 sync down failed: {s3_prefix}")


def page_id(page_num: int) -> str:
    return f"{page_num:04d}"


def is_blank_page(page: Any, pixmap: Any, *, min_text_chars: int = 1) -> bool:
    text = (page.get_text() or "").strip()
    if len(text) >= min_text_chars:
        return False
    samples = pixmap.samples
    if not samples:
        return True
    step = max(len(samples) // 4000, 3)
    chunk = samples[::step]
    if len(chunk) < 12:
        return True
    return max(chunk) < 250


def split_pdf_to_pages(
    pdf_bytes: bytes,
    *,
    pages_root: Path,
    liteparse_pages_root: Path | None,
    render_dpi: int,
) -> dict[str, Any]:
    if fitz is None:
        raise RuntimeError("pymupdf not installed — pip install pymupdf")

    pages_root.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page_count = doc.page_count
    page_ids: list[str] = []
    skipped_empty: list[str] = []
    total_bytes_png = 0

    try:
        for idx in range(page_count):
            page_num = idx + 1
            pid = page_id(page_num)
            page_dir = pages_root / pid
            page_dir.mkdir(parents=True, exist_ok=True)

            single = fitz.open()
            single.insert_pdf(doc, from_page=idx, to_page=idx)
            pdf_path = page_dir / "page.pdf"
            single.save(str(pdf_path))
            single.close()

            page = doc[idx]
            pix = page.get_pixmap(dpi=render_dpi, alpha=False)
            png_path = page_dir / "page.png"
            pix.save(str(png_path))
            total_bytes_png += png_path.stat().st_size

            if is_blank_page(page, pix):
                skipped_empty.append(pid)

            lp_json = page_dir / "liteparse.json"
            lp_md = page_dir / "liteparse.md"
            if liteparse_pages_root:
                src_dir = liteparse_pages_root / pid
                src_json = src_dir / "page.json"
                src_md = src_dir / "page.md"
                if src_json.is_file():
                    shutil.copy2(src_json, lp_json)
                if src_md.is_file():
                    shutil.copy2(src_md, lp_md)
            page_ids.append(pid)
    finally:
        doc.close()

    manifest = {
        "page_count": page_count,
        "render_dpi": render_dpi,
        "pages": page_ids,
        "skipped_empty_pages": skipped_empty,
        "split_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (pages_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "page_count": page_count,
        "pages_split": len(page_ids),
        "skipped_empty": len(skipped_empty),
        "png_bytes": total_bytes_png,
        "manifest": manifest,
    }


def merge_state_3b(
    state: dict[str, Any],
    *,
    split_result: dict[str, Any],
    elapsed_s: float,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.setdefault("3a_liteparse", {})
    state["3b_split"] = {
        "status": "complete",
        "at": now,
        "elapsed_s": round(elapsed_s, 2),
        "page_count": split_result["page_count"],
        "render_dpi": split_result["manifest"]["render_dpi"],
        "skipped_empty_pages": split_result["skipped_empty"],
    }
    state.setdefault("3c_flash", {"status": "pending"})
    return state


def split_document(
    *,
    bucket: str,
    ticker: str,
    document_key: str,
    corpus_key: str,
    s3_pdf_key: str,
    render_dpi: int = DEFAULT_RENDER_DPI,
    skip_if_exists: bool = False,
    pdf_file: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    ticker = ticker.upper()
    doc_prefix = asx.s3_parsed_doc_prefix(ticker, corpus_key, document_key)
    liteparse_key = asx.s3_liteparse_manifest_key(ticker, corpus_key, document_key)
    pages_key = asx.s3_pages_manifest_key(ticker, corpus_key, document_key)

    if skip_if_exists and bucket and s3_object_exists(bucket, pages_key):
        return {
            "status": "skipped",
            "reason": "pages_manifest_exists",
            "ticker": ticker,
            "document_key": document_key,
            "s3_pages_manifest_key": pages_key,
        }

    if bucket and not s3_object_exists(bucket, liteparse_key):
        return {
            "status": "skipped",
            "reason": "no_3a_liteparse",
            "ticker": ticker,
            "document_key": document_key,
            "s3_liteparse_manifest_key": liteparse_key,
        }

    if pdf_file:
        pdf_bytes = pdf_file.read_bytes()
    elif bucket:
        pdf_bytes = s3_download_bytes(bucket, s3_pdf_key)
    else:
        raise ValueError("bucket or pdf_file required")

    started = time.monotonic()
    work = output_dir or Path(f"/tmp/gypsy-split-{os.getpid()}")
    if work.exists() and not output_dir:
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)

    liteparse_local = work / "liteparse_pages"
    if bucket:
        s3_sync_prefix_down(
            bucket, f"{doc_prefix}/liteparse/pages", liteparse_local
        )

    pages_root = work / "pages"
    split_result = split_pdf_to_pages(
        pdf_bytes,
        pages_root=pages_root,
        liteparse_pages_root=liteparse_local if liteparse_local.is_dir() else None,
        render_dpi=render_dpi,
    )
    elapsed = time.monotonic() - started

    state_key = f"{doc_prefix}/state.json"
    if bucket and s3_object_exists(bucket, state_key):
        state = json.loads(s3_download_text(bucket, state_key))
    else:
        state = {}
    state = merge_state_3b(state, split_result=split_result, elapsed_s=elapsed)
    (work / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    if output_dir:
        return {
            "status": "ok",
            "ticker": ticker,
            "document_key": document_key,
            "output_dir": str(output_dir),
            **{k: split_result[k] for k in ("page_count", "pages_split", "skipped_empty")},
            "elapsed_s": round(elapsed, 2),
        }

    if bucket:
        s3_sync_dir(bucket, pages_root, f"{doc_prefix}/pages")
        s3_upload_file(bucket, work / "state.json", state_key)

    if not output_dir and work.exists() and str(work).startswith("/tmp/"):
        shutil.rmtree(work, ignore_errors=True)

    return {
        "status": "ok",
        "ticker": ticker,
        "document_key": document_key,
        "s3_prefix": doc_prefix,
        "s3_pages_manifest_key": pages_key,
        **{k: split_result[k] for k in ("page_count", "pages_split", "skipped_empty")},
        "elapsed_s": round(elapsed, 2),
    }


def main() -> int:
    args = parse_args()
    if not args.bucket and not args.output_dir and not args.pdf_file:
        print("error: --bucket, --output-dir, or --pdf-file required", file=sys.stderr)
        return 2

    pdf_key = args.s3_pdf_key
    if not pdf_key and args.corpus_key == "annual_reports":
        print("error: --s3-pdf-key required for annual_reports", file=sys.stderr)
        return 2

    out = args.output_dir
    if out:
        out = (
            out
            / args.ticker.upper()
            / asx.corpus_info(args.corpus_key)[1]
            / asx.parsed_doc_folder(args.document_key)
        )

    result = split_document(
        bucket=args.bucket or "",
        ticker=args.ticker,
        document_key=args.document_key,
        corpus_key=args.corpus_key,
        s3_pdf_key=pdf_key,
        render_dpi=args.render_dpi,
        skip_if_exists=args.skip_if_exists,
        pdf_file=args.pdf_file,
        output_dir=out,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
