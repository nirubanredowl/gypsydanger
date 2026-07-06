#!/usr/bin/env python3
"""Step 3 — Download PDFs for every documentKey in announcements.csv."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 3: fetch PDFs from CDN → entities/{TICKER}/raw/"
    )
    parser.add_argument(
        "--pilot-only",
        action="store_true",
        help="Process only tickers in data/pilot_tickers.txt",
    )
    parser.add_argument(
        "--ticker",
        action="append",
        help="Process specific ticker(s) only; may repeat",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable HTTP response cache",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=asx.MIN_PDF_BYTES,
        help=f"Skip re-download if file exists and >= this size (default {asx.MIN_PDF_BYTES})",
    )
    parser.add_argument(
        "--annual-reports-only",
        action="store_true",
        help="Fetch only rows classified as annual reports (see announcements-schema.md)",
    )
    parser.add_argument(
        "--annual-filter",
        choices=("strict", "loose"),
        default="loose",
        help="Annual report filter mode when --annual-reports-only is set (default: loose)",
    )
    parser.add_argument(
        "--burn-error-pct",
        type=float,
        default=1.0,
        help="Rolling-window 429+503 pct that marks the IP burned (default 1.0)",
    )
    parser.add_argument(
        "--burn-consecutive-429",
        type=int,
        default=5,
        help="Consecutive 429 responses that mark the IP burned (default 5)",
    )
    return parser.parse_args()


def load_fetch_log(path: Path) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {"success": [], "skipped": [], "failed": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("success", "skipped", "failed"):
        data.setdefault(key, [])
    return data


def save_fetch_log(path: Path, log: dict[str, list[dict[str, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=2), encoding="utf-8")


def tickers_to_process(args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return sorted({t.upper() for t in args.ticker})

    entities_path = asx.entities_csv_path()
    if not entities_path.exists():
        raise FileNotFoundError(
            f"{entities_path} not found. Run 01_normalise_entities.py first."
        )

    pilot = asx.load_pilot_tickers() if args.pilot_only else None
    tickers: list[str] = []
    with entities_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or "").upper()
            if not ticker:
                continue
            if pilot is not None and ticker not in pilot:
                continue
            tickers.append(ticker)
    return sorted(set(tickers))


def fetch_ticker(
    client: asx.AsxClient,
    ticker: str,
    log: dict[str, list[dict[str, str]]],
    *,
    min_bytes: int,
    annual_reports_only: bool = False,
    annual_filter: str = "loose",
    burn: asx.CdnBurnTracker | None = None,
) -> tuple[int, int, int, bool]:
    ann_path = asx.announcements_csv_path(ticker)
    if not ann_path.exists():
        raise FileNotFoundError(
            f"{ann_path} not found. Run 02_index_announcements.py for {ticker}."
        )

    raw_dir = asx.entity_dir(ticker) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    ok = skip = fail = filtered = 0
    burned = False
    with ann_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if annual_reports_only and not asx.is_annual_report_announcement(
                row, mode=annual_filter
            ):
                filtered += 1
                continue

            document_key = (row.get("documentKey") or "").strip()
            if not document_key:
                continue

            dest = asx.raw_pdf_path(ticker, document_key)
            url = asx.cdn_pdf_url(document_key)

            if dest.exists() and dest.stat().st_size >= min_bytes:
                log["skipped"].append(
                    {
                        "ticker": ticker,
                        "document_key": document_key,
                        "file": str(dest),
                        "reason": "exists",
                    }
                )
                skip += 1
                continue

            try:
                content = client.get_bytes(url)
                if len(content) < min_bytes:
                    raise ValueError(
                        f"download too small ({len(content)} bytes): {url}"
                    )
                dest.write_bytes(content)
                log["success"].append(
                    {
                        "ticker": ticker,
                        "document_key": document_key,
                        "file": str(dest),
                        "url": url,
                        "bytes": str(len(content)),
                    }
                )
                ok += 1
                if burn is not None:
                    burned = burn.record_success()
            except Exception as exc:  # noqa: BLE001
                status = asx.http_error_status(exc)
                log["failed"].append(
                    {
                        "ticker": ticker,
                        "document_key": document_key,
                        "url": url,
                        "error": str(exc),
                    }
                )
                fail += 1
                print(f"    FAIL {document_key}: {exc}")
                if burn is not None:
                    burned = burn.record_error(status)

            if burned:
                print(
                    f"  BURNED on {ticker} after {burn.total_requests} CDN requests "
                    f"(429={burn.counts['429']}) — stop for IP rotation"
                )
                break

    if annual_reports_only and filtered:
        print(f"  filtered_out={filtered} (non-annual rows)")

    return ok, skip, fail, burned


def main() -> int:
    args = parse_args()
    tickers = tickers_to_process(args)
    if not tickers:
        print("No tickers to process.")
        return 1

    client = asx.AsxClient(use_cache=not args.no_cache)
    log = load_fetch_log(asx.fetch_log_path())
    burn = asx.CdnBurnTracker(
        burn_error_pct=args.burn_error_pct,
        consecutive_429_limit=args.burn_consecutive_429,
    )

    total_ok = total_skip = total_fail = 0
    ip_burned = False
    for ticker in tickers:
        label = "annual report PDFs" if args.annual_reports_only else "PDFs"
        filter_note = f" ({args.annual_filter})" if args.annual_reports_only else ""
        print(f"[{ticker}] fetching {label}{filter_note}...")
        try:
            ok, skip, fail, burned = fetch_ticker(
                client,
                ticker,
                log,
                min_bytes=args.min_bytes,
                annual_reports_only=args.annual_reports_only,
                annual_filter=args.annual_filter,
                burn=burn,
            )
            total_ok += ok
            total_skip += skip
            total_fail += fail
            print(f"  success={ok} skipped={skip} failed={fail}")
            if burned:
                ip_burned = True
                break
        except FileNotFoundError as exc:
            print(f"  ERROR: {exc}")
            total_fail += 1

    save_fetch_log(asx.fetch_log_path(), log)
    print(
        f"\nDone. success={total_ok} skipped={total_skip} failed={total_fail} "
        f"→ {asx.fetch_log_path()}"
    )
    if ip_burned:
        print("IP burned — terminate this EC2 instance and launch a replacement.")
        return asx.BURN_EXIT_CODE
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
