#!/usr/bin/env python3
"""Submit AWS remote login authorization code without truncating long tokens."""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys


def normalize_code(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("Y29kZT"):
        decoded = base64.b64decode(raw).decode("utf-8")
        if decoded.startswith("code="):
            decoded = decoded.split("code=", 1)[1]
        if "&state=" in decoded:
            decoded = decoded.split("&state=", 1)[0]
        return decoded
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="Complete aws login --remote with a code.")
    parser.add_argument("code", help="Authorization code (raw or base64 from browser)")
    args = parser.parse_args()
    code = normalize_code(args.code)

    proc = subprocess.run(
        ["aws", "login", "--remote", "--region", "ap-southeast-2", "--no-cli-pager"],
        input=f"{code}\n",
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
