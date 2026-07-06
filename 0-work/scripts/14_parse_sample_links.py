#!/usr/bin/env python3
"""Print presigned S3 URLs for parse-spec sample annual reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SAMPLES = [
    ("GMG", "2022", "2924-02574374-2A1401779", "Goodman Group Sustainability Report and 2022 Annual Report"),
    ("GRR", "2021", "2924-02366720-3A565819", "Annual Report to shareholders"),
    ("HFR", "2020", "2924-02219266-6A973367", "Annual Report to shareholders"),
    ("ALK", "2016", "2995-01789611-6A794312", "Annual Report to shareholders - 2016"),
    ("MCP", "2018", "2995-02030256-2A1108135", "FY18 McPhersons Annual Report"),
    ("UNT", "2021", "2924-02413413-3A574136", "Annual Report and Appendix 4E"),
    ("A1N", "2019", "2995-02074914-2A1132251", "2018 HT&E Annual Report"),
    ("CDM", "2013", "2995-01447152-2A757665", "Annual Report June 2013"),
]

EXPIRES_S = 604800  # 7 days


def presign(bucket: str, key: str) -> str:
    env = {**os.environ, "AWS_PAGER": ""}
    result = subprocess.run(
        [
            "aws",
            "s3",
            "presign",
            f"s3://{bucket}/{key}",
            "--expires-in",
            str(EXPIRES_S),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.strip()


def main() -> int:
    bucket = os.environ.get("GYPSY_S3_BUCKET", "")
    if not bucket:
        print("Set GYPSY_S3_BUCKET", file=sys.stderr)
        return 2

    rows = []
    print(f"# Sample annual reports (presigned links expire in {EXPIRES_S // 86400} days)\n")
    for ticker, year, dockey, headline in SAMPLES:
        key = f"entities/{ticker}/annual_reports/{year}_{dockey}.pdf"
        url = presign(bucket, key)
        rows.append(
            {
                "ticker": ticker,
                "year": year,
                "headline": headline,
                "s3_key": key,
                "presigned_url": url,
            }
        )
        print(f"## {ticker} FY{year} — {headline}\n")
        print(f"{url}\n")

    out = Path("/tmp/parse-sample-links.json")
    out.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"(Wrote {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
