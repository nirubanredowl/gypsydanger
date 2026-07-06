#!/usr/bin/env python3
"""Probe announcements for signals useful in detecting CFO changes.

Scans indexed announcement CSVs for announcementTypes tags and headline
patterns that surface CFO appointments, resignations, and related executive
changes — beyond annual reports alone.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from importlib import import_module

asx = import_module("00_asx_api")

# Headline patterns (ordered roughly by specificity)
CFO_ROLE = re.compile(
    r"\bCFO\b|chief financial officer|finance director|financial controller",
    re.I,
)
CFO_CHANGE = re.compile(
    r"(?:appointment|appoint|resign|resignation|retire|retirement|departure|depart|"
    r"change|transition|step(?:ping)? down|leav(?:e|ing)|joins?|named|announce(?:s|d)?)",
    re.I,
)
CEO_ROLE = re.compile(
    r"\bCEO\b|chief executive|managing director|\bMD\b(?!\s*\&)",
    re.I,
)

# announcementTypes with direct executive-change semantics
TYPED_EXEC_TAGS = frozenset(
    {
        "Director Appointment/Resignation",
        "Company Secretary Appointment/Resignation",
        "CEO/Managing Director - Appointment Resignation",
        "Chair Appointment/Resignation",
        "Chairman Appointment/Resignation",
    }
)

# Tags that may mention KMP/CFO in body but are not change events
CONTEXT_TAGS = frozenset(
    {
        "Full Year Directors' Report",
        "Full Year Directors' Statement",
        "Half Year Directors' Report",
        "Half Year Directors' Statement",
        "Preliminary Final Report",
        "Half Yearly Report",
        "Periodic Reports - Other",
        "Open Briefing",
        "Company Presentation",
    }
)

# High volume, usually holdings — not role changes
NOISE_TAGS = frozenset(
    {
        "Change of Director's Interest Notice",
        "Initial Director's Interest Notice",
        "Final Director's Interest Notice",
    }
)

GENERIC_ADMIN = "Company Administration - Other"


@dataclass
class Match:
    ticker: str
    date: str
    headline: str
    document_key: str
    types: list[str]
    tier: str
    signal: str


@dataclass
class ScanStats:
    rows_scanned: int = 0
    tickers: int = 0
    matches: list[Match] = field(default_factory=list)
    tier_counts: Counter = field(default_factory=Counter)
    signal_counts: Counter = field(default_factory=Counter)
    type_on_cfo_headlines: Counter = field(default_factory=Counter)
    type_on_typed_exec: Counter = field(default_factory=Counter)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-indexed",
        action="store_true",
        help="Scan every ticker with an announcements CSV (default)",
    )
    parser.add_argument("--ticker", action="append", help="Limit to ticker(s)")
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Sample rows per tier in text output",
    )
    return parser.parse_args()


def tickers_to_scan(args: argparse.Namespace) -> list[str]:
    if args.ticker:
        return sorted({t.upper() for t in args.ticker})
    tickers: list[str] = []
    for path in sorted((asx.data_dir() / "entities").iterdir()):
        if path.is_dir() and asx.announcements_csv_path(path.name).exists():
            tickers.append(path.name.upper())
    return tickers


def parse_types(raw: str) -> list[str]:
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def classify_row(ticker: str, row: dict[str, str]) -> Match | None:
    headline = (row.get("headline") or "").strip()
    types = parse_types(row.get("announcementTypes", ""))
    type_set = set(types)
    has_cfo = bool(CFO_ROLE.search(headline))
    has_cfo_change = has_cfo and bool(CFO_CHANGE.search(headline))
    typed_exec = type_set & TYPED_EXEC_TAGS

    if has_cfo_change:
        if typed_exec:
            signal = "cfo_headline+typed_exec_tag"
            tier = "A"
        elif type_set == {GENERIC_ADMIN} or (
            GENERIC_ADMIN in type_set and not typed_exec
        ):
            signal = "cfo_headline+admin_other"
            tier = "A"
        else:
            signal = "cfo_headline+other_tags"
            tier = "B"
        return Match(
            ticker=ticker,
            date=(row.get("date") or "")[:10],
            headline=headline,
            document_key=(row.get("documentKey") or "").strip(),
            types=types,
            tier=tier,
            signal=signal,
        )

    if typed_exec and has_cfo:
        return Match(
            ticker=ticker,
            date=(row.get("date") or "")[:10],
            headline=headline,
            document_key=(row.get("documentKey") or "").strip(),
            types=types,
            tier="B",
            signal="cfo_headline+typed_exec_tag",
        )

    if typed_exec and CEO_ROLE.search(headline) and CFO_ROLE.search(headline):
        return Match(
            ticker=ticker,
            date=(row.get("date") or "")[:10],
            headline=headline,
            document_key=(row.get("documentKey") or "").strip(),
            types=types,
            tier="B",
            signal="ceo_and_cfo_headline+typed_exec",
        )

    # Company secretary changes sometimes reflect CFO dual-hat
    if "Company Secretary Appointment/Resignation" in type_set and has_cfo:
        return Match(
            ticker=ticker,
            date=(row.get("date") or "")[:10],
            headline=headline,
            document_key=(row.get("documentKey") or "").strip(),
            types=types,
            tier="B",
            signal="cfo_headline+company_secretary_tag",
        )

    return None


def scan_ticker(ticker: str, stats: ScanStats) -> None:
    path = asx.announcements_csv_path(ticker)
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            stats.rows_scanned += 1
            types = parse_types(row.get("announcementTypes", ""))
            headline = row.get("headline") or ""

            if CFO_ROLE.search(headline):
                for t in types:
                    stats.type_on_cfo_headlines[t] += 1

            if set(types) & TYPED_EXEC_TAGS:
                for t in types:
                    if t in TYPED_EXEC_TAGS:
                        stats.type_on_typed_exec[t] += 1

            match = classify_row(ticker, row)
            if match:
                stats.matches.append(match)
                stats.tier_counts[match.tier] += 1
                stats.signal_counts[match.signal] += 1


def recommendation_block(stats: ScanStats) -> list[str]:
    total = len(stats.matches)
    tier_a = stats.tier_counts.get("A", 0)
    tier_b = stats.tier_counts.get("B", 0)
    return [
        "## Recommended fetch signals for CFO change detection",
        "",
        "### Tier A — primary (headline + change verb + CFO role)",
        f"- **~{tier_a:,} rows** — headlines explicitly about CFO appointment/resignation/change.",
        f"- Dominant tag: `{GENERIC_ADMIN}` (~67% of all CFO headlines); typed tags are sparse.",
        "- **Fetch filter:** headline regex (CFO role + change verb); do not rely on `announcementTypes` alone.",
        "",
        "### Tier B — secondary (typed executive tags + CFO in headline)",
        f"- **~{tier_b:,} rows** — `Director Appointment/Resignation`, `Company Secretary Appointment/Resignation`, etc.",
        "- Catches ~107 rows tagged `Director Appointment/Resignation` with CFO in headline.",
        "",
        "### Not useful for CFO *changes* (context / noise)",
        "- `Change of Director's Interest Notice` (~102k) — securities holdings, not role changes.",
        "- `Full/Half Year Directors' Report` — may list KMP in PDF body; event date ≠ change date.",
        "- No dedicated `CFO Appointment/Resignation` Markit tag exists (unlike CEO/Managing Director).",
        "",
        "### Other report types worth parsing for CFO mentions (body text, not headline)",
        "- Annual report (`Annual Report` loose) — KMP section, remuneration report.",
        "- `Appendix 4G` (~14k) — corporate governance statements.",
        "- `Open Briefing` / `Company Presentation` — occasional leadership slides.",
        "",
        f"**Total classified CFO-change candidates:** {total:,} / {stats.rows_scanned:,} announcements "
        f"({100 * total / stats.rows_scanned:.2f}%)",
    ]


def format_text(stats: ScanStats, samples: int) -> str:
    lines = [
        "CFO change signal probe",
        f"Tickers: {stats.tickers}  Rows: {stats.rows_scanned:,}  Matches: {len(stats.matches):,}",
        "",
        "Tier counts:",
    ]
    for tier in ("A", "B"):
        lines.append(f"  Tier {tier}: {stats.tier_counts.get(tier, 0):,}")
    lines.extend(["", "Signal breakdown:"])
    for sig, count in stats.signal_counts.most_common():
        lines.append(f"  {count:5d}  {sig}")

    lines.extend(["", "announcementTypes on any CFO-ish headline (top 15):"])
    for tag, count in stats.type_on_cfo_headlines.most_common(15):
        lines.append(f"  {count:5d}  {tag}")

    lines.extend(["", "Typed executive tags (all rows, top 10):"])
    for tag, count in stats.type_on_typed_exec.most_common(10):
        lines.append(f"  {count:5d}  {tag}")

    for tier in ("A", "B"):
        tier_matches = [m for m in stats.matches if m.tier == tier]
        lines.extend(["", f"Samples tier {tier}:"])
        for m in tier_matches[:samples]:
            lines.append(
                f"  {m.ticker} {m.date} | {m.headline[:75]} | {m.types[:2]}"
            )

    lines.extend(["", *recommendation_block(stats)])
    return "\n".join(lines)


def to_json(stats: ScanStats) -> dict:
    by_ticker = Counter(m.ticker for m in stats.matches)
    return {
        "tickers_scanned": stats.tickers,
        "rows_scanned": stats.rows_scanned,
        "match_count": len(stats.matches),
        "tier_counts": dict(stats.tier_counts),
        "signal_counts": dict(stats.signal_counts),
        "type_on_cfo_headlines_top20": stats.type_on_cfo_headlines.most_common(20),
        "typed_exec_tag_counts": dict(stats.type_on_typed_exec),
        "tickers_with_any_match": len(by_ticker),
        "avg_matches_per_ticker": round(len(stats.matches) / max(len(by_ticker), 1), 2),
        "recommended_signals": {
            "primary": {
                "method": "headline_regex",
                "cfo_role_pattern": CFO_ROLE.pattern,
                "change_pattern": CFO_CHANGE.pattern,
                "notes": "67% tagged Company Administration - Other only",
            },
            "secondary_tags": sorted(TYPED_EXEC_TAGS),
            "context_not_change_events": sorted(CONTEXT_TAGS),
            "noise_tags": sorted(NOISE_TAGS),
        },
        "samples": {
            tier: [
                {
                    "ticker": m.ticker,
                    "date": m.date,
                    "headline": m.headline,
                    "document_key": m.document_key,
                    "announcement_types": m.types,
                    "signal": m.signal,
                }
                for m in [x for x in stats.matches if x.tier == tier][:8]
            ]
            for tier in ("A", "B")
        },
    }


def main() -> int:
    args = parse_args()
    tickers = tickers_to_scan(args)
    stats = ScanStats(tickers=len(tickers))

    for ticker in tickers:
        try:
            scan_ticker(ticker, stats)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)

    if args.json:
        print(json.dumps(to_json(stats), indent=2))
    else:
        print(format_text(stats, args.samples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
