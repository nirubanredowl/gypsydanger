#!/usr/bin/env python3
"""Run aws login --remote, print URL, then wait for a code file."""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import time
from pathlib import Path


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--code-file",
        default="/tmp/aws_login_code.txt",
        help="Write authorization code to this file to complete login",
    )
    parser.add_argument(
        "--poll-s",
        type=float,
        default=2.0,
        help="Poll interval while waiting for code file",
    )
    args = parser.parse_args()
    code_path = Path(args.code_file)

    proc = subprocess.Popen(
        ["stdbuf", "-oL", "aws", "login", "--remote", "--region", "ap-southeast-2", "--no-cli-pager"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    assert proc.stdin is not None

    while True:
        line = proc.stdout.readline()
        if not line:
            return proc.wait() or 1
        print(line, end="")
        if "Enter the authorization code" in line:
            break

    print(f"\nWaiting for code in {code_path} ...", flush=True)
    while proc.poll() is None:
        if code_path.exists():
            code = normalize_code(code_path.read_text(encoding="utf-8"))
            code_path.unlink(missing_ok=True)
            proc.stdin.write(code + "\n")
            proc.stdin.flush()
            break
        time.sleep(args.poll_s)

    for line in proc.stdout:
        print(line, end="")

    return proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
