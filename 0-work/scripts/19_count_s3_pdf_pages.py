#!/usr/bin/env python3
"""Count PDF pages in S3 corpora (annual reports and CFO change notices).

Downloads each PDF from S3 and counts pages with pypdf. Reports file counts,
total pages, and LlamaParse credit estimates (default 10 credits/page).

Example:
  python3 19_count_s3_pdf_pages.py
  python3 19_count_s3_pdf_pages.py --corpus cfo --workers 16
  python3 19_count_s3_pdf_pages.py --corpus annual --sample 100
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Literal

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "pypdf is required: pip install pypdf"
    ) from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

CorpusName = Literal["annual", "cfo", "all"]


@dataclass
class FileResult:
    key: str
    pages: int | None
    bytes: int
    error: str | None = None


@dataclass
class CorpusSummary:
    name: str
    prefix: str
    files: int = 0
    pages: int = 0
    bytes: int = 0
    errors: int = 0
    error_samples: list[dict[str, str]] = field(default_factory=list)
    elapsed_s: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket",
        default=os.environ.get("GYPSY_S3_BUCKET", ""),
        help="S3 bucket (default: GYPSY_S3_BUCKET)",
    )
    parser.add_argument(
        "--corpus",
        choices=("annual", "cfo", "all"),
        default="all",
        help="Which corpus to scan (default: all)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("GYPSY_PAGE_COUNT_WORKERS", "12")),
        help="Parallel S3 download + page-count workers",
    )
    parser.add_argument(
        "--credits-per-page",
        type=int,
        default=10,
        help="LlamaParse credits per page (default: 10)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Only process first N PDFs per corpus (0 = all)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=asx.data_dir() / "pdf_page_counts.json",
        help="Write machine-readable summary JSON here",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip keys already present in --output with a page count",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List PDF keys and byte sizes only; do not count pages",
    )
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


def aws_cmd_bytes(*parts: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    return subprocess.run(
        ["aws", *parts, "--no-cli-pager"],
        capture_output=True,
        env=env,
    )


def list_pdf_keys(bucket: str, *, corpus: CorpusName) -> dict[str, list[str]]:
    """Return S3 object keys grouped by corpus name."""
    prefixes: list[tuple[str, str]] = []
    if corpus in ("annual", "all"):
        prefixes.append(("annual", "entities/"))
    if corpus in ("cfo", "all"):
        prefixes.append(("cfo", "entities/"))

    out: dict[str, list[str]] = {name: [] for name, _ in prefixes}
    for name, prefix in prefixes:
        token = ""
        while True:
            cmd = [
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--prefix",
                prefix,
            ]
            if token:
                cmd.extend(["--continuation-token", token])
            result = aws_cmd(*cmd)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "list-objects-v2 failed")
            payload = json.loads(result.stdout or "{}")
            for row in payload.get("Contents") or []:
                key = row.get("Key") or ""
                if not key.endswith(".pdf"):
                    continue
                if name == "annual" and "/annual_reports/" not in key:
                    continue
                if name == "cfo" and "/cfo_changes/" not in key:
                    continue
                out[name].append(key)
            if not payload.get("IsTruncated"):
                break
            token = payload.get("NextContinuationToken") or ""
            if not token:
                break
        out[name].sort()
    return out


def s3_get_bytes(bucket: str, key: str) -> bytes:
    result = aws_cmd_bytes("s3", "cp", f"s3://{bucket}/{key}", "-")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def count_pages(data: bytes) -> int:
    reader = PdfReader(BytesIO(data), strict=False)
    return len(reader.pages)


def process_key(bucket: str, key: str) -> FileResult:
    try:
        data = s3_get_bytes(bucket, key)
        pages = count_pages(data)
        return FileResult(key=key, pages=pages, bytes=len(data))
    except Exception as exc:  # noqa: BLE001
        return FileResult(key=key, pages=None, bytes=0, error=str(exc))


def load_resume_pages(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    pages_by_key: dict[str, int] = {}
    for corpus in payload.get("corpora") or []:
        for row in corpus.get("entries") or []:
            key = row.get("key")
            pages = row.get("pages")
            if key and isinstance(pages, int):
                pages_by_key[key] = pages
    return pages_by_key


def summarise_corpus(
    *,
    name: str,
    prefix_label: str,
    keys: list[str],
    bucket: str,
    workers: int,
    resume_pages: dict[str, int],
    list_only: bool,
) -> tuple[CorpusSummary, list[dict[str, object]]]:
    summary = CorpusSummary(name=name, prefix=prefix_label)
    file_rows: list[dict[str, object]] = []
    started = time.monotonic()

    pending = [k for k in keys if k not in resume_pages]
    for key, pages in resume_pages.items():
        if key in keys:
            summary.files += 1
            summary.pages += pages
            file_rows.append({"key": key, "pages": pages, "bytes": None, "resumed": True})

    if list_only:
        for key in pending:
            result = aws_cmd("s3api", "head-object", "--bucket", bucket, "--key", key)
            if result.returncode != 0:
                summary.errors += 1
                if len(summary.error_samples) < 10:
                    summary.error_samples.append(
                        {"key": key, "error": result.stderr.strip() or "head-object failed"}
                    )
                continue
            meta = json.loads(result.stdout or "{}")
            size = int(meta.get("ContentLength") or 0)
            summary.files += 1
            summary.bytes += size
            file_rows.append({"key": key, "pages": None, "bytes": size})
        summary.elapsed_s = time.monotonic() - started
        return summary, file_rows

    if not pending:
        summary.elapsed_s = time.monotonic() - started
        return summary, file_rows

    workers = max(1, workers)
    done = 0
    total = len(pending)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_key, bucket, key): key for key in pending}
        for fut in as_completed(futures):
            result = fut.result()
            done += 1
            if result.error or result.pages is None:
                summary.errors += 1
                if len(summary.error_samples) < 10:
                    summary.error_samples.append(
                        {"key": result.key, "error": result.error or "unknown"}
                    )
            else:
                summary.files += 1
                summary.pages += result.pages
                summary.bytes += result.bytes
                file_rows.append(
                    {
                        "key": result.key,
                        "pages": result.pages,
                        "bytes": result.bytes,
                    }
                )
            if done % 100 == 0 or done == total:
                print(
                    f"  {name}: {done}/{total} counted "
                    f"(pages so far {summary.pages:,}, errors {summary.errors})",
                    flush=True,
                )

    summary.elapsed_s = time.monotonic() - started
    return summary, file_rows


def print_summary(
    summaries: list[CorpusSummary], *, credits_per_page: int, list_only: bool
) -> None:
    grand_files = sum(s.files for s in summaries)
    grand_pages = sum(s.pages for s in summaries)
    grand_bytes = sum(s.bytes for s in summaries)
    grand_errors = sum(s.errors for s in summaries)
    grand_credits = grand_pages * credits_per_page

    print()
    print("=" * 72)
    print("PDF page count summary")
    print("=" * 72)
    for s in summaries:
        credits = s.pages * credits_per_page
        print(f"\n{s.name.upper()} ({s.prefix})")
        print(f"  PDFs:      {s.files:,}")
        if list_only:
            print(f"  Bytes:     {s.bytes:,}")
        else:
            print(f"  Pages:     {s.pages:,}")
            print(f"  Bytes:     {s.bytes:,}")
            print(
                f"  LlamaParse credits (@ {credits_per_page}/page): "
                f"{credits:,}"
            )
        print(f"  Errors:    {s.errors:,}")
        print(f"  Elapsed:   {s.elapsed_s:.1f}s")
        if s.error_samples:
            print("  Error samples:")
            for row in s.error_samples[:5]:
                print(f"    - {row['key']}: {row['error']}")

    print("\nTOTAL")
    print(f"  PDFs:      {grand_files:,}")
    if list_only:
        print(f"  Bytes:     {grand_bytes:,}")
    else:
        print(f"  Pages:     {grand_pages:,}")
        print(f"  Bytes:     {grand_bytes:,}")
        print(
            f"  LlamaParse credits (@ {credits_per_page}/page): "
            f"{grand_credits:,}"
        )
    print(f"  Errors:    {grand_errors:,}")
    print("=" * 72)


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("error: --workers must be >= 1", file=sys.stderr)
        return 2

    print(f"Listing PDFs in s3://{args.bucket} (corpus={args.corpus})...")
    keys_by_corpus = list_pdf_keys(args.bucket, corpus=args.corpus)
    for name, keys in keys_by_corpus.items():
        if args.sample > 0:
            keys_by_corpus[name] = keys[: args.sample]
        print(f"  {name}: {len(keys_by_corpus[name]):,} PDFs")

    resume_pages = load_resume_pages(args.output) if args.resume else {}
    if resume_pages:
        print(f"Resume: {len(resume_pages):,} keys with prior page counts")

    corpora_payload: list[dict[str, object]] = []
    summaries: list[CorpusSummary] = []
    for name, keys in keys_by_corpus.items():
        if not keys:
            continue
        prefix_label = (
            "entities/{TICKER}/annual_reports/"
            if name == "annual"
            else "entities/{TICKER}/cfo_changes/"
        )
        print(f"\nCounting {name}...")
        summary, file_rows = summarise_corpus(
            name=name,
            prefix_label=prefix_label,
            keys=keys,
            bucket=args.bucket,
            workers=args.workers,
            resume_pages=resume_pages,
            list_only=args.list_only,
        )
        summaries.append(summary)
        corpora_payload.append(
            {
                "name": summary.name,
                "prefix": summary.prefix,
                "file_count": summary.files,
                "pages": summary.pages,
                "bytes": summary.bytes,
                "errors": summary.errors,
                "credits_per_page": args.credits_per_page,
                "llamaparse_credits": summary.pages * args.credits_per_page,
                "elapsed_s": round(summary.elapsed_s, 1),
                "error_samples": summary.error_samples,
                "entries": file_rows,
            }
        )

    payload = {
        "bucket": args.bucket,
        "corpus": args.corpus,
        "credits_per_page": args.credits_per_page,
        "list_only": args.list_only,
        "totals": {
            "files": sum(s.files for s in summaries),
            "pages": sum(s.pages for s in summaries),
            "bytes": sum(s.bytes for s in summaries),
            "errors": sum(s.errors for s in summaries),
            "llamaparse_credits": sum(s.pages for s in summaries)
            * args.credits_per_page,
        },
        "corpora": corpora_payload,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {args.output}")

    print_summary(
        summaries, credits_per_page=args.credits_per_page, list_only=args.list_only
    )
    return 1 if any(s.errors for s in summaries) else 0


if __name__ == "__main__":
    raise SystemExit(main())
