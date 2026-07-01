#!/usr/bin/env python3
"""Step 2 — Resolve entity_xid and index all announcements per ticker."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Step 2: index announcements → entities/{TICKER}/announcements.csv"
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
        "--max-scan-pages",
        type=int,
        default=500,
        help="Max market pages to scan when resolving entity_xid (default 500)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable HTTP response cache",
    )
    return parser.parse_args()


def load_entities(
    path: Path, *, pilot_only: bool, tickers: list[str] | None
) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run 01_normalise_entities.py first."
        )
    pilot = asx.load_pilot_tickers() if pilot_only else None
    filter_set = {t.upper() for t in tickers} if tickers else None

    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))

    out: list[dict[str, str]] = []
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if not ticker:
            continue
        if pilot is not None and ticker not in pilot:
            continue
        if filter_set is not None and ticker not in filter_set:
            continue
        out.append(row)
    return out


def write_entities(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=asx.ENTITY_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def append_run_log(message: str) -> None:
    path = asx.index_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} {message}\n")
        fh.flush()


def dedupe_by_document_key(
    rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for row in rows:
        key = row.get("documentKey") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def index_ticker(
    client: asx.AsxClient,
    row: dict[str, str],
    overrides: dict,
    *,
    max_scan_pages: int,
) -> tuple[dict[str, str], int]:
    ticker = row["ticker"].upper()
    entity_xid = (row.get("entity_xid") or "").strip()

    if not entity_xid:
        entity_xid = asx.resolve_entity_xid(
            client,
            ticker,
            overrides,
            max_scan_pages=max_scan_pages,
        )
        row["entity_xid"] = entity_xid
        print(f"  resolved entity_xid={entity_xid}")

    items = asx.fetch_all_announcements(client, entity_xid)
    ann_rows = [
        asx.announcement_row(ticker, entity_xid, item)
        for item in items
        if item.get("documentKey")
    ]
    ann_rows = dedupe_by_document_key(ann_rows)

    out_dir = asx.entity_dir(ticker)
    out_dir.mkdir(parents=True, exist_ok=True)
    ann_path = asx.announcements_csv_path(ticker)

    with ann_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=asx.ANNOUNCEMENT_COLUMNS)
        writer.writeheader()
        writer.writerows(ann_rows)

    print(f"  indexed {len(ann_rows)} announcements → {ann_path}")
    return row, len(ann_rows)


def main() -> int:
    args = parse_args()
    entities_path = asx.entities_csv_path()
    entities = load_entities(
        entities_path, pilot_only=args.pilot_only, tickers=args.ticker
    )
    if not entities:
        print("No entities to process.")
        return 1

    client = asx.AsxClient(use_cache=not args.no_cache)
    overrides = asx.load_overrides()

    updated: list[dict[str, str]] = []
    total_ann = 0
    errors: list[tuple[str, str]] = []

    # Reload full entities file so we can merge xid updates back
    with entities_path.open(encoding="utf-8", newline="") as fh:
        all_rows = list(csv.DictReader(fh))
    by_ticker = {(r.get("ticker") or "").upper(): r for r in all_rows}

    scope = "pilot" if args.pilot_only else "all"
    if args.ticker:
        scope = f"tickers={','.join(args.ticker)}"
    append_run_log(f"RUN START scope={scope} count={len(entities)}")
    print(f"Run log: {asx.index_log_path()}")

    for row in entities:
        ticker = row["ticker"].upper()
        print(f"[{ticker}] indexing...")
        append_run_log(f"START {ticker}")
        try:
            updated_row, count = index_ticker(
                client,
                row,
                overrides,
                max_scan_pages=args.max_scan_pages,
            )
            by_ticker[ticker] = updated_row
            total_ann += count
            write_entities(
                entities_path, [by_ticker[k] for k in sorted(by_ticker)]
            )
            append_run_log(
                f"OK {ticker} entity_xid={updated_row.get('entity_xid', '')} "
                f"announcements={count}"
            )
        except Exception as exc:  # noqa: BLE001 — log and continue
            errors.append((ticker, str(exc)))
            print(f"  ERROR: {exc}")
            append_run_log(f"ERROR {ticker} {exc}")

    append_run_log(
        f"RUN DONE announcements={total_ann} errors={len(errors)}"
    )

    print(f"\nDone. {total_ann} announcements indexed across {len(entities)} tickers.")
    if errors:
        print(f"Errors ({len(errors)}):")
        for ticker, msg in errors:
            print(f"  {ticker}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
