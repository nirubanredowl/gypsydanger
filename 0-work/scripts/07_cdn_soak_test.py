#!/usr/bin/env python3
"""CDN soak probe — measure rate limits without writing PDFs to disk."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
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
        help="Ticker whose announcements.csv supplies documentKeys (omit if --keys-file)",
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
        "--start-offset",
        type=int,
        default=None,
        help="Alias for --key-offset (used by burn rotation to resume a shard)",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Worker label included in summary (e.g. shard_02)",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        default=0,
        help="IP rotation count for this worker slot (metadata only)",
    )
    parser.add_argument(
        "--result-json",
        type=Path,
        help="Write machine-readable summary JSON to this path",
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


def write_result(path: Path | None, payload: dict[str, object]) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    start_offset = args.start_offset if args.start_offset is not None else args.key_offset

    if args.keys_file:
        keys = load_keys_from_file(args.keys_file)
        source = str(args.keys_file)
        ticker = (args.ticker or "POOL").upper()
    else:
        if not args.ticker:
            print("error: --ticker required unless --keys-file is set", file=sys.stderr)
            return 2
        ticker = args.ticker.upper()
        keys = load_document_keys(ticker)
        source = ticker

    if start_offset:
        keys = keys[start_offset:]
    if args.single_key and keys:
        keys = [keys[0]]
    if not keys:
        print("error: no keys left after offset", file=sys.stderr)
        return 1

    client = asx.AsxClient(
        rate_limit_s=args.rate_limit_s,
        use_cache=not args.no_cache,
    )
    burn = asx.CdnBurnTracker(
        burn_error_pct=args.burn_error_pct,
        consecutive_429_limit=args.burn_consecutive_429,
    )

    started = time.monotonic()
    keys_done = 0
    burned = False
    bytes_total = 0

    print(
        f"Soak: source={source} max_requests={args.max_requests} "
        f"rate_limit_s={args.rate_limit_s} single_key={args.single_key} "
        f"no_cache={args.no_cache} unique_keys={len(keys)} "
        f"start_offset={start_offset} label={args.label or '-'} rotation={args.rotation}"
    )

    idx = 0
    while keys_done < args.max_requests:
        document_key = keys[idx % len(keys)]
        idx += 1
        url = asx.cdn_pdf_url(document_key)

        try:
            payload = client.get_bytes(url, use_cache=not args.no_cache)
            burned = burn.record_success()
            keys_done += 1
            bytes_total += len(payload)
        except Exception as exc:  # noqa: BLE001
            status = asx.http_error_status(exc)
            burned = burn.record_error(status)
            keys_done += 1
            if burn.total_requests <= 5 or status in (429, 503) or burned:
                print(f"  FAIL [{burn.total_requests}] {document_key}: {exc}")

        if burned:
            print(
                f"  BURNED after {burn.total_requests} requests "
                f"(429={burn.counts['429']} 503={burn.counts['503']}) — exit for IP rotation"
            )
            break

        if burn.total_requests % 50 == 0:
            elapsed = time.monotonic() - started
            rps = burn.total_requests / elapsed if elapsed else 0.0
            print(
                f"  progress requests={burn.total_requests} success={burn.counts['success']} "
                f"429={burn.counts['429']} 503={burn.counts['503']} rps={rps:.2f}"
            )

    elapsed = time.monotonic() - started
    snap = burn.snapshot()
    err_pct = float(snap["error_pct"])
    docs_hr = (3600.0 * burn.counts["success"] / elapsed) if elapsed else 0.0

    print("\n--- soak summary ---")
    if args.label:
        print(f"label:           {args.label}")
    print(f"source:          {source}")
    print(f"rotation:        {args.rotation}")
    print(f"start_offset:    {start_offset}")
    print(f"keys_done:       {keys_done}")
    print(f"elapsed_s:       {elapsed:.1f}")
    print(f"requests:        {snap['requests']}")
    print(f"success:         {snap['success']}")
    print(f"429:             {snap['429']}")
    print(f"503:             {snap['503']}")
    print(f"other_errors:    {snap['other_errors']}")
    print(f"error_pct:       {err_pct:.2f}%")
    print(f"burned:          {burned}")
    print(f"bytes:           {bytes_total}")
    print(f"effective_rps:   {snap['requests'] / elapsed:.2f}" if elapsed else "effective_rps:   n/a")
    print(f"docs_hr:         {docs_hr:.0f}")

    result_payload: dict[str, object] = {
        "label": args.label or None,
        "source": source,
        "rotation": args.rotation,
        "start_offset": start_offset,
        "keys_done": keys_done,
        "absolute_offset": start_offset + keys_done,
        "max_requests": args.max_requests,
        "rate_limit_s": args.rate_limit_s,
        "elapsed_s": round(elapsed, 1),
        "requests": snap["requests"],
        "success": snap["success"],
        "429": snap["429"],
        "503": snap["503"],
        "other_errors": snap["other_errors"],
        "error_pct": err_pct,
        "burned": burned,
        "bytes": bytes_total,
        "docs_hr": round(docs_hr),
        "complete": (not burned) and keys_done >= args.max_requests,
    }
    write_result(args.result_json, result_payload)
    if args.result_json:
        print(f"result_json:     {args.result_json}")

    if burned:
        return asx.BURN_EXIT_CODE
    return 1 if err_pct > 1.0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
