#!/usr/bin/env python3
"""Parse parse-spec sample annual reports with open-parse."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import openparse
from openparse.doc_parser import DocumentParser

SAMPLES = [
    ("CDM", "2013"),
    ("A1N", "2019"),
    ("MCP", "2018"),
    ("ALK", "2016"),
    ("UNT", "2021"),
    ("HFR", "2020"),
    ("GRR", "2021"),
    ("GMG", "2022"),
]

ROOT = Path(__file__).resolve().parent
PDF_DIR = ROOT / "pdfs"
OUT_DIR = ROOT / "output"


def node_payload(node: object) -> dict:
    """Serialize an open-parse node to a JSON-safe dict."""
    if hasattr(node, "model_dump"):
        data = node.model_dump(mode="json")
    elif hasattr(node, "dict"):
        data = node.dict()
    else:
        data = {"repr": repr(node)}
    text = data.get("text") or getattr(node, "text", "") or ""
    data["text_chars"] = len(text)
    data["text_preview"] = text[:500].replace("\n", " ")
    return data


def parsed_payload(parsed: object) -> dict:
    if hasattr(parsed, "model_dump"):
        return parsed.model_dump(mode="json")
    if hasattr(parsed, "dict"):
        return parsed.dict()
    return {"nodes": [node_payload(n) for n in parsed.nodes]}


def parse_one(
    pdf_path: Path,
    *,
    use_ml_tables: bool,
    min_table_confidence: float,
    no_processing: bool,
) -> dict:
    table_args = None
    if use_ml_tables:
        table_args = {
            "parsing_algorithm": "unitable",
            "min_table_confidence": min_table_confidence,
        }
    pipeline = None if no_processing else openparse.processing.BasicIngestionPipeline()
    parser = DocumentParser(
        processing_pipeline=pipeline,
        table_args=table_args,
    )
    started = time.monotonic()
    parsed = parser.parse(str(pdf_path))
    elapsed = time.monotonic() - started

    nodes = [node_payload(n) for n in parsed.nodes]
    table_nodes = [
        n for n in nodes if "|" in (n.get("text") or "") and "---" in (n.get("text") or "")
    ]
    total_chars = sum(int(n.get("text_chars", 0)) for n in nodes)

    return {
        "pdf": pdf_path.name,
        "pipeline": "noop" if no_processing else "basic",
        "elapsed_s": round(elapsed, 1),
        "node_count": len(nodes),
        "table_like_nodes": len(table_nodes),
        "total_text_chars": total_chars,
        "nodes": nodes,
    }


def write_markdown_summary(result: dict, path: Path) -> None:
    lines = [
        f"# {result['pdf']}",
        "",
        f"- Nodes: {result['node_count']}",
        f"- Table-like nodes (markdown pipe tables): {result['table_like_nodes']}",
        f"- Total chars: {result['total_text_chars']:,}",
        f"- Parse time: {result['elapsed_s']}s",
        "",
        "## First 5 nodes (preview)",
        "",
    ]
    for i, node in enumerate(result["nodes"][:5]):
        lines.append(f"### Node {i + 1}")
        lines.append("")
        text = node.get("text") or node.get("text_preview") or ""
        preview = text[:1200]
        lines.append(preview)
        if len(text) > 1200:
            lines.append("\n…")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="*",
        help="Ticker codes to parse (default: all samples)",
    )
    parser.add_argument(
        "--ml-tables",
        action="store_true",
        help="Use openparse[ml] unitable (requires openparse-download)",
    )
    parser.add_argument(
        "--basic-pipeline",
        action="store_true",
        help="Run BasicIngestionPipeline (default: no-op pipeline, avoids PIL image errors)",
    )
    parser.add_argument("--min-table-confidence", type=float, default=0.8)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    selected = SAMPLES
    if args.only:
        tickers = {t.upper() for t in args.only}
        selected = [(t, y) for t, y in SAMPLES if t in tickers]

    summary_rows = []
    for ticker, year in selected:
        pdf_path = PDF_DIR / f"{ticker}_{year}.pdf"
        if not pdf_path.exists():
            print(f"SKIP missing {pdf_path}", file=sys.stderr)
            continue
        print(f"Parsing {pdf_path.name} ...", flush=True)
        try:
            result = parse_one(
                pdf_path,
                use_ml_tables=args.ml_tables,
                min_table_confidence=args.min_table_confidence,
                no_processing=not args.basic_pipeline,
            )
            out_json = OUT_DIR / f"{ticker}_{year}.json"
            out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
            write_markdown_summary(result, OUT_DIR / f"{ticker}_{year}.md")
            summary_rows.append(
                {
                    "ticker": ticker,
                    "year": year,
                    "node_count": result["node_count"],
                    "table_like_nodes": result["table_like_nodes"],
                    "total_text_chars": result["total_text_chars"],
                    "elapsed_s": result["elapsed_s"],
                    "status": "ok",
                }
            )
            print(
                f"  OK nodes={result['node_count']} tables={result['table_like_nodes']} "
                f"chars={result['total_text_chars']} time={result['elapsed_s']}s"
            )
        except Exception as exc:
            summary_rows.append(
                {
                    "ticker": ticker,
                    "year": year,
                    "status": "error",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            print(f"  ERROR {exc}", file=sys.stderr)

    summary = {
        "parser": "open-parse",
        "ml_tables": args.ml_tables,
        "pipeline": "basic" if args.basic_pipeline else "noop",
        "results": summary_rows,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {OUT_DIR / 'summary.json'}")
    return 1 if any(r.get("status") == "error" for r in summary_rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
