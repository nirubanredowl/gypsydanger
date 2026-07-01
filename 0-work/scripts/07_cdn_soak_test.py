#!/usr/bin/env python3
"""CDN soak probe — measure rate limits without writing PDFs to disk."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download PDF bytes from CDN for soak testing (no disk writes)."
    )
    parser.add_argument(
        "--ticker",
        required=True,
        help="Ticker whose announcements.csv supplies documentKeys",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=500,
        help="Stop after this many CDN GET attempts (default 500)",
    )
    parser.add_argument(
        "--rate-limit-s",
        type=float,
        default=1.0,
        help="Minimum seconds between requests (default 1.0)",
    )
    parser.add_argument(
        "--single-key",
        action="store_true",
        help="Repeat the first documentKey only (micro-probe; use with --no-cache)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable HTTP response cache (required for meaningful soak)",
    )
    parser.add_argument(
        "--keys-file",
        type=Path,
        help="Text file with one documentKey per line (overrides --ticker)",
    )
    parser.add_argument(
        "--key-offset",
        type=int,
        default=0,
        help="Skip first N keys after loading (use with --ticker)",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Worker label included in summary (e.g. shard_02)",
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        help="Write machine-readable summary JSON to this path",
    )
    return parser.parse_args()


def load_keys_from_file(path: Path) -> list[str]:
    keys: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            key = line.strip()
            if key and not key.startswith("#"):
                keys.append(key)
    if not keys:
        raise ValueError(f"No documentKeys in {path}")
    return keys


def load_document_keys(ticker: str) -> list[str]:
    path = asx.announcements_csv_path(ticker)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run 02_index_announcements.py first.")
    keys: list[str] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("documentKey") or "").strip()
            if key:
                keys.append(key)
    if not keys:
        raise ValueError(f"No documentKey rows in {path}")
    return keys


def http_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    return None


def main() -> int:
    args = parse_args()
    ticker = args.ticker.upper()
    if args.keys_file:
        keys = load_keys_from_file(args.keys_file)
        source = str(args.keys_file)
    else:
        keys = load_document_keys(ticker)
        source = ticker
        if args.key_offset:
            keys = keys[args.key_offset :]
    if args.single_key:
        keys = [keys[0]]

    client = asx.AsxClient(
        rate_limit_s=args.rate_limit_s,
        use_cache=not args.no_cache,
    )

    stats = {
        "requests": 0,
        "success": 0,
        "429": 0,
        "503": 0,
        "other_errors": 0,
        "bytes": 0,
    }
    started = time.monotonic()

    print(
        f"Soak: source={source} max_requests={args.max_requests} "
        f"rate_limit_s={args.rate_limit_s} single_key={args.single_key} "
        f"no_cache={args.no_cache} unique_keys={len(keys)} label={args.label or '-'}"
    )

    idx = 0
    while stats["requests"] < args.max_requests:
        document_key = keys[idx % len(keys)]
        idx += 1
        url = asx.cdn_pdf_url(document_key)
        stats["requests"] += 1

        try:
            payload = client.get_bytes(url, use_cache=not args.no_cache)
            stats["success"] += 1
            stats["bytes"] += len(payload)
        except Exception as exc:  # noqa: BLE001
            status = http_status(exc)
            if status == 429:
                stats["429"] += 1
            elif status == 503:
                stats["503"] += 1
            else:
                stats["other_errors"] += 1
            if stats["requests"] <= 5 or status in (429, 503):
                print(f"  FAIL [{stats['requests']}] {document_key}: {exc}")

        if stats["requests"] % 50 == 0:
            elapsed = time.monotonic() - started
            rps = stats["requests"] / elapsed if elapsed else 0.0
            print(
                f"  progress requests={stats['requests']} success={stats['success']} "
                f"429={stats['429']} 503={stats['503']} rps={rps:.2f}"
            )

    elapsed = time.monotonic() - started
    err = stats["429"] + stats["503"] + stats["other_errors"]
    err_pct = (100.0 * err / stats["requests"]) if stats["requests"] else 0.0
    docs_hr = (3600.0 * stats["success"] / elapsed) if elapsed else 0.0

    print("\n--- soak summary ---")
    if args.label:
        print(f"label:           {args.label}")
    print(f"source:          {source}")
    print(f"elapsed_s:       {elapsed:.1f}")
    print(f"requests:        {stats['requests']}")
    print(f"success:         {stats['success']}")
    print(f"429:             {stats['429']}")
    print(f"503:             {stats['503']}")
    print(f"other_errors:    {stats['other_errors']}")
    print(f"error_pct:       {err_pct:.2f}%")
    print(f"bytes:           {stats['bytes']}")
    print(f"effective_rps:   {stats['requests'] / elapsed:.2f}" if elapsed else "effective_rps:   n/a")
    print(f"docs_hr:         {docs_hr:.0f}")

    if args.result_json:
        payload = {
            "label": args.label or None,
            "source": source,
            "max_requests": args.max_requests,
            "rate_limit_s": args.rate_limit_s,
            "elapsed_s": round(elapsed, 1),
            "requests": stats["requests"],
            "success": stats["success"],
            "429": stats["429"],
            "503": stats["503"],
            "other_errors": stats["other_errors"],
            "error_pct": round(err_pct, 2),
            "bytes": stats["bytes"],
            "docs_hr": round(docs_hr),
        }
        args.result_json.parent.mkdir(parents=True, exist_ok=True)
        args.result_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"result_json:     {args.result_json}")

    return 1 if err_pct > 1.0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
