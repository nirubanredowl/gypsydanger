#!/usr/bin/env python3
"""Stage 3B shard worker — page split for annual reports with 3A LiteParse complete."""

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
split = import_module("24_split_pdf_pages")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers-file", type=Path, required=True)
    parser.add_argument("--bucket", default=os.environ.get("GYPSY_S3_BUCKET", ""))
    parser.add_argument("--run-id", default=os.environ.get("GYPSY_SPLIT_RUN_ID", ""))
    parser.add_argument("--worker-id", default=os.environ.get("GYPSY_WORKER_ID", "00"))
    parser.add_argument("--ticker-index", type=int, default=0)
    parser.add_argument("--doc-offset", type=int, default=0)
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
    parser.add_argument("--render-dpi", type=int, default=split.DEFAULT_RENDER_DPI)
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
    tmp = Path(f"/tmp/split-progress-{os.getpid()}.json")
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
            ticker = tickers[idx]
            if not fetch.s3_object_exists(
                bucket,
                asx.s3_liteparse_manifest_key(ticker, corpus_key, document_key),
            ):
                continue
            if fetch.s3_object_exists(
                bucket,
                asx.s3_pages_manifest_key(ticker, corpus_key, document_key),
            ):
                continue
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
    skip_no_3a: int,
    fail: int,
    pages_split: int,
    elapsed: float,
    complete: bool,
    last_error: str | None,
) -> dict[str, object]:
    return {
        "worker_id": args.worker_id,
        "run_id": args.run_id or None,
        "rotation": args.rotation,
        "phase": "3b_split",
        "corpus_key": args.corpus_key,
        "ticker_index": resume_ticker_index,
        "doc_offset": resume_doc_offset,
        "current_ticker": current_ticker,
        "tickers_total": len(tickers),
        "tickers_done": tickers_completed,
        "documents_target": docs_target,
        "documents_done": docs_done,
        "split": ok,
        "skipped_existing": skip,
        "skipped_no_3a": skip_no_3a,
        "failed": fail,
        "pages_split": pages_split,
        "annual_filter": args.annual_filter,
        "render_dpi": args.render_dpi,
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
        f"3B split shard: worker={args.worker_id} rotation={args.rotation} "
        f"tickers={len(tickers)} resume_index={args.ticker_index} "
        f"doc_offset={args.doc_offset} targets={docs_target} "
        f"corpus={args.corpus_key} bucket={args.bucket}"
    )

    started = time.monotonic()
    ok = skip = skip_no_3a = fail = 0
    pages_split = 0
    docs_done = 0
    tickers_completed = args.ticker_index
    resume_ticker_index = args.ticker_index
    resume_doc_offset = args.doc_offset
    current_ticker = tickers[args.ticker_index]
    last_error: str | None = None

    for idx in range(args.ticker_index, len(tickers)):
        ticker = tickers[idx]
        current_ticker = ticker
        offset = args.doc_offset if idx == args.ticker_index else 0
        rows = fetch.load_announcements_csv(ticker, bucket=args.bucket, source="s3")
        targets = fetch.annual_report_rows(
            rows, mode=args.annual_filter, start_offset=offset
        )

        print(f"  ticker {ticker}: {len(targets)} rows (offset={offset})")

        for row in targets:
            document_key = (row.get("documentKey") or "").strip()
            if not document_key:
                continue
            date_str = row.get("date") or ""
            pdf_key = asx.s3_annual_report_key(ticker, date_str, document_key)

            lite_key = asx.s3_liteparse_manifest_key(
                ticker, args.corpus_key, document_key
            )
            if not fetch.s3_object_exists(args.bucket, lite_key):
                skip_no_3a += 1
                docs_done += 1
                continue

            try:
                result = split.split_document(
                    bucket=args.bucket,
                    ticker=ticker,
                    document_key=document_key,
                    corpus_key=args.corpus_key,
                    s3_pdf_key=pdf_key,
                    render_dpi=args.render_dpi,
                    skip_if_exists=True,
                )
                status = result["status"]
                if status == "skipped" and result.get("reason") == "pages_manifest_exists":
                    skip += 1
                elif status == "skipped":
                    skip_no_3a += 1
                elif status == "ok":
                    ok += 1
                    pages_split += int(result.get("pages_split") or 0)
                else:
                    fail += 1
                    last_error = str(result)
                docs_done += 1
                print(
                    f"    {status.upper()} {document_key} "
                    f"pages={result.get('page_count', result.get('reason', '?'))}"
                )
            except Exception as exc:  # noqa: BLE001
                fail += 1
                docs_done += 1
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
                skip_no_3a=skip_no_3a,
                fail=fail,
                pages_split=pages_split,
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
        skip_no_3a=skip_no_3a,
        fail=fail,
        pages_split=pages_split,
        elapsed=elapsed,
        complete=complete,
        last_error=last_error,
    )
    if args.result_json:
        args.result_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    upload_progress(args.progress_s3_uri, payload)

    print(
        f"\nSummary: split={ok} skipped={skip} no_3a={skip_no_3a} failed={fail} "
        f"pages={pages_split} tickers_done={tickers_completed}/{len(tickers)} "
        f"complete={complete}"
    )
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
