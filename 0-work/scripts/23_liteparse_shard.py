#!/usr/bin/env python3
"""Stage 3A shard worker — LiteParse annual report PDFs from S3."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")
fetch = import_module("11_fetch_annual_reports_s3")
lite = import_module("22_liteparse_document")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers-file", type=Path, required=True)
    parser.add_argument("--bucket", default=os.environ.get("GYPSY_S3_BUCKET", ""))
    parser.add_argument("--run-id", default=os.environ.get("GYPSY_PARSE_RUN_ID", ""))
    parser.add_argument("--worker-id", default=os.environ.get("GYPSY_WORKER_ID", "00"))
    parser.add_argument(
        "--ticker-index",
        type=int,
        default=0,
        help="Resume at this ticker index in the shard",
    )
    parser.add_argument(
        "--doc-offset",
        type=int,
        default=0,
        help="Skip first N documents in the current ticker",
    )
    parser.add_argument("--rotation", type=int, default=0)
    parser.add_argument(
        "--corpus-key",
        default=os.environ.get("GYPSY_PARSE_CORPUS_KEY", "annual_reports"),
    )
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
    )
    parser.add_argument("--ocr", action="store_true")
    parser.add_argument("--result-json", type=Path)
    parser.add_argument(
        "--progress-s3-uri",
        default=os.environ.get("GYPSY_PROGRESS_S3_URI", ""),
    )
    return parser.parse_args()


def load_tickers(path: Path) -> list[str]:
    tickers = [
        line.strip().upper()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not tickers:
        raise ValueError(f"empty ticker shard: {path}")
    return tickers


def upload_progress(uri: str, payload: dict[str, object]) -> None:
    if not uri:
        return
    tmp = Path(f"/tmp/parse-progress-{os.getpid()}.json")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        result = fetch.aws_cmd("s3", "cp", str(tmp), uri)
        if result.returncode != 0:
            print(f"  progress upload failed: {result.stderr.strip()}", file=sys.stderr)
    finally:
        tmp.unlink(missing_ok=True)


def shard_doc_count(
    tickers: list[str],
    *,
    bucket: str,
    mode: str,
    corpus_key: str,
    from_index: int,
    from_offset: int,
) -> int:
    total = 0
    for idx in range(from_index, len(tickers)):
        rows = fetch.load_announcements_csv(tickers[idx], bucket=bucket, source="s3")
        matched = fetch.annual_report_rows(rows, mode=mode, start_offset=0)
        if idx == from_index:
            matched = matched[from_offset:]
        for row in matched:
            document_key = (row.get("documentKey") or "").strip()
            if not document_key:
                continue
            date_str = row.get("date") or ""
            pdf_key = asx.s3_annual_report_key(tickers[idx], date_str, document_key)
            if fetch.s3_object_exists(bucket, pdf_key):
                total += 1
    return total


def build_payload(
    *,
    args: argparse.Namespace,
    tickers: list[str],
    current_ticker: str,
    resume_ticker_index: int,
    resume_doc_offset: int,
    tickers_completed: int,
    docs_target: int,
    docs_done: int,
    ok: int,
    skip: int,
    fail: int,
    pages_parsed: int,
    elapsed: float,
    complete: bool,
    last_error: str | None,
) -> dict[str, object]:
    return {
        "worker_id": args.worker_id,
        "run_id": args.run_id or None,
        "rotation": args.rotation,
        "phase": "3a_liteparse",
        "corpus_key": args.corpus_key,
        "ticker_index": resume_ticker_index,
        "doc_offset": resume_doc_offset,
        "current_ticker": current_ticker,
        "tickers_total": len(tickers),
        "tickers_done": tickers_completed,
        "documents_target": docs_target,
        "documents_done": docs_done,
        "parsed": ok,
        "skipped_existing": skip,
        "failed": fail,
        "pages_parsed": pages_parsed,
        "annual_filter": args.annual_filter,
        "elapsed_s": round(elapsed, 1),
        "complete": complete,
        "last_error": last_error,
        "bucket": args.bucket,
    }


def main() -> int:
    args = parse_args()
    if not args.bucket:
        print("error: --bucket or GYPSY_S3_BUCKET required", file=sys.stderr)
        return 2

    tickers = load_tickers(args.tickers_file)
    if args.ticker_index >= len(tickers):
        print(
            f"error: ticker_index {args.ticker_index} >= shard size {len(tickers)}",
            file=sys.stderr,
        )
        return 2

    docs_target = shard_doc_count(
        tickers,
        bucket=args.bucket,
        mode=args.annual_filter,
        corpus_key=args.corpus_key,
        from_index=args.ticker_index,
        from_offset=args.doc_offset,
    )

    print(
        f"3A LiteParse shard: worker={args.worker_id} rotation={args.rotation} "
        f"tickers={len(tickers)} resume_index={args.ticker_index} "
        f"doc_offset={args.doc_offset} targets={docs_target} "
        f"corpus={args.corpus_key} bucket={args.bucket}"
    )

    started = time.monotonic()
    ok = skip = fail = 0
    pages_parsed = 0
    docs_done = 0
    tickers_completed = args.ticker_index
    resume_ticker_index = args.ticker_index
    resume_doc_offset = args.doc_offset
    current_ticker = tickers[args.ticker_index]
    last_error: str | None = None
    failed = False

    for idx in range(args.ticker_index, len(tickers)):
        ticker = tickers[idx]
        current_ticker = ticker
        offset = args.doc_offset if idx == args.ticker_index else 0
        rows = fetch.load_announcements_csv(ticker, bucket=args.bucket, source="s3")
        targets = fetch.annual_report_rows(
            rows, mode=args.annual_filter, start_offset=offset
        )

        print(f"  ticker {ticker}: {len(targets)} targets (offset={offset})")
        processed_in_ticker = 0

        for row in targets:
            document_key = (row.get("documentKey") or "").strip()
            if not document_key:
                continue
            date_str = row.get("date") or ""
            pdf_key = asx.s3_annual_report_key(ticker, date_str, document_key)

            if not fetch.s3_object_exists(args.bucket, pdf_key):
                fail += 1
                docs_done += 1
                processed_in_ticker += 1
                last_error = f"missing PDF: {pdf_key}"
                print(f"    FAIL missing PDF {pdf_key}")
                continue

            try:
                result = lite.parse_document(
                    bucket=args.bucket,
                    ticker=ticker,
                    document_key=document_key,
                    corpus_key=args.corpus_key,
                    s3_pdf_key=pdf_key,
                    published_date=date_str,
                    headline=row.get("headline") or "",
                    entity_xid=row.get("entity_xid") or "",
                    announcement_types=row.get("announcementTypes") or "",
                    skip_if_exists=True,
                    ocr_enabled=args.ocr,
                )
                if result["status"] == "skipped":
                    skip += 1
                else:
                    ok += 1
                    pages_parsed += int(result.get("page_count") or 0)
                docs_done += 1
                processed_in_ticker += 1
                print(
                    f"    {result['status'].upper()} {document_key} "
                    f"pages={result.get('page_count', '?')}"
                )
            except Exception as exc:  # noqa: BLE001
                fail += 1
                docs_done += 1
                processed_in_ticker += 1
                failed = True
                last_error = str(exc)
                print(f"    FAIL {document_key}: {exc}")

        tickers_completed = idx + 1
        resume_ticker_index = tickers_completed
        resume_doc_offset = 0
        upload_progress(
            args.progress_s3_uri,
            build_payload(
                args=args,
                tickers=tickers,
                current_ticker=current_ticker,
                resume_ticker_index=resume_ticker_index,
                resume_doc_offset=resume_doc_offset,
                tickers_completed=tickers_completed,
                docs_target=docs_target,
                docs_done=docs_done,
                ok=ok,
                skip=skip,
                fail=fail,
                pages_parsed=pages_parsed,
                elapsed=time.monotonic() - started,
                complete=False,
                last_error=last_error,
            ),
        )

    elapsed = time.monotonic() - started
    complete = tickers_completed >= len(tickers)

    payload = build_payload(
        args=args,
        tickers=tickers,
        current_ticker=current_ticker,
        resume_ticker_index=resume_ticker_index,
        resume_doc_offset=resume_doc_offset,
        tickers_completed=tickers_completed,
        docs_target=docs_target,
        docs_done=docs_done,
        ok=ok,
        skip=skip,
        fail=fail,
        pages_parsed=pages_parsed,
        elapsed=elapsed,
        complete=complete,
        last_error=last_error,
    )
    if args.result_json:
        args.result_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    upload_progress(args.progress_s3_uri, payload)

    print(
        f"\nSummary: parsed={ok} skipped={skip} failed={fail} "
        f"pages={pages_parsed} tickers_done={tickers_completed}/{len(tickers)} "
        f"complete={complete}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
