#!/usr/bin/env python3
"""Bundle page-count sample PDFs + open-parse + LiteParse outputs into one folder."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "data" / "pdf_page_counts.json"
DEFAULT_BUNDLE = ROOT / "data" / "parse-sample-corpus"
OPENPARSE_OUT = ROOT / "0-work" / "experiments" / "openparse-sample" / "output"
LITEPARSE_OUT = ROOT / "0-work" / "experiments" / "liteparse-sample" / "output"
OPENPARSE_CACHE = ROOT / "0-work" / "experiments" / "openparse-sample" / "cache" / "pdfs"
LITEPARSE_CACHE = ROOT / "0-work" / "experiments" / "liteparse-sample" / "cache" / "pdfs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--bucket", default=os.environ.get("GYPSY_S3_BUCKET", ""))
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def aws_cmd_bytes(*parts: str) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["AWS_PAGER"] = ""
    env["AWS_CLI_PAGER"] = ""
    return subprocess.run(["aws", *parts], capture_output=True, env=env)


def slug_from_key(key: str) -> tuple[str, str]:
    parts = key.split("/")
    ticker, folder, basename = parts[1], parts[2], parts[-1].removesuffix(".pdf")
    corpus = "annual" if folder == "annual_reports" else "cfo"
    return corpus, f"{ticker}_{basename}"


def cache_path(cache_root: Path, key: str) -> Path:
    return cache_root / key.replace("/", "__")


def download_pdf(bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = aws_cmd_bytes("s3", "cp", f"s3://{bucket}/{key}", str(dest))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip())


def copy_if_exists(src: Path, dest: Path) -> bool:
    if not src.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return True


def load_manifest(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, object]] = []
    for block in payload.get("corpora") or []:
        for entry in block.get("entries") or []:
            rows.append(
                {
                    "corpus": block["name"],
                    "key": entry["key"],
                    "pages": entry.get("pages"),
                    "bytes": entry.get("bytes"),
                }
            )
    return rows


def main() -> int:
    args = parse_args()
    bundle = args.bundle_dir
    pdfs_dir = bundle / "pdfs"
    open_dir = bundle / "openparse"
    lite_dir = bundle / "liteparse"

    entries = load_manifest(args.manifest)
    index: list[dict[str, object]] = []

    for entry in entries:
        key = str(entry["key"])
        corpus, slug = slug_from_key(key)
        pdf_dest = pdfs_dir / corpus / f"{slug}.pdf"

        if not pdf_dest.exists():
            copied = False
            for cache in (OPENPARSE_CACHE, LITEPARSE_CACHE):
                cached = cache_path(cache, key)
                if cached.exists():
                    pdf_dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(cached, pdf_dest)
                    copied = True
                    break
            if not copied and not args.skip_download:
                if not args.bucket:
                    print("error: --bucket required to download PDFs", file=sys.stderr)
                    return 2
                download_pdf(args.bucket, key, pdf_dest)

        row = {
            "slug": slug,
            "corpus": corpus,
            "s3_key": key,
            "pages": entry.get("pages"),
            "bytes": entry.get("bytes"),
            "pdf": str(pdf_dest.relative_to(bundle)),
            "openparse_json": None,
            "openparse_md": None,
            "liteparse_json": None,
            "liteparse_md": None,
        }

        for parser, src_root, dest_root in (
            ("openparse", OPENPARSE_OUT, open_dir),
            ("liteparse", LITEPARSE_OUT, lite_dir),
        ):
            for ext, field in (("json", f"{parser}_json"), ("md", f"{parser}_md")):
                src = src_root / corpus / f"{slug}.{ext}"
                dest = dest_root / corpus / f"{slug}.{ext}"
                if copy_if_exists(src, dest):
                    row[field] = str(dest.relative_to(bundle))

        index.append(row)

    shutil.copy2(args.manifest, bundle / "manifest.json")
    for name, src in (
        ("openparse-summary.json", OPENPARSE_OUT / "summary.json"),
        ("liteparse-summary.json", LITEPARSE_OUT / "summary.json"),
    ):
        if src.exists():
            shutil.copy2(src, bundle / name)

    (bundle / "index.json").write_text(
        json.dumps({"entries": index, "count": len(index)}, indent=2) + "\n",
        encoding="utf-8",
    )

    missing_open = sum(1 for r in index if not r["openparse_json"])
    missing_lite = sum(1 for r in index if not r["liteparse_json"])
    missing_pdf = sum(1 for r in index if not Path(bundle / str(r["pdf"])).exists())

    print(f"Bundle: {bundle}")
    print(f"  PDFs:      {len(index) - missing_pdf}/{len(index)}")
    print(f"  openparse: {len(index) - missing_open}/{len(index)}")
    print(f"  liteparse: {len(index) - missing_lite}/{len(index)}")
    print(f"  index:     {bundle / 'index.json'}")
    return 1 if missing_open or missing_lite or missing_pdf else 0


if __name__ == "__main__":
    raise SystemExit(main())
