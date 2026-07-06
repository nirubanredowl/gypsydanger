"""Shared ASX / Markit Digital API helpers for Stage 1 scripts."""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "GypsyDanger/1.0 (ASX research pipeline)"
MARKIT_BASE = "https://asx.api.markitdigital.com/asx-research/1.0"
CDN_BASE = (
    "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/file"
)
DIRECTORY_URL = f"{MARKIT_BASE}/companies/directory/file"

ENTITY_COLUMNS = [
    "ticker",
    "name",
    "gics_industry_group",
    "listing_date",
    "market_cap_aud",
    "entity_xid",
]

ANNOUNCEMENT_COLUMNS = [
    "ticker",
    "entity_xid",
    "documentKey",
    "date",
    "headline",
    "fileSize",
    "isPriceSensitive",
    "symbol",
    "url",
    "announcementTypes",
    "companies",
    "companyInfo",
    "symbolsSecondary",
]

MIN_PDF_BYTES = 50 * 1024
MIN_PDF_MAGIC_BYTES = 1024


def is_valid_pdf(content: bytes) -> bool:
    """Accept real PDFs even when smaller than MIN_PDF_BYTES."""
    return len(content) >= MIN_PDF_MAGIC_BYTES and content.startswith(b"%PDF")

# Co-tags commonly present on ASX annual report filings (Appendix 4E bundle).
FULL_YEAR_ANNOUNCEMENT_TYPES = frozenset(
    {
        "Full Year Accounts",
        "Full Year Audit Review",
        "Full Year Directors' Report",
        "Full Year Directors' Statement",
        "Preliminary Final Report",
    }
)

# Headlines matching these patterns are tagged "Annual Report" but are not the
# primary statutory annual report PDF we want for parse/analysis.
ANNUAL_REPORT_HEADLINE_EXCLUDE_RE = re.compile(
    r"shareholder review|shareholder update|sustainability report|summary review|"
    r"form 20-f|economic contribution|non-renounceable|payments to governments",
    re.IGNORECASE,
)

ANNUAL_REPORT_HEADLINE_INCLUDE_RE = re.compile(
    r"annual report|appendix 4e",
    re.IGNORECASE,
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def data_dir() -> Path:
    return repo_root() / "data"


def docs_dir() -> Path:
    return repo_root() / "0-work" / "docs"


def entities_csv_path() -> Path:
    return data_dir() / "entities.csv"


def pilot_tickers_path() -> Path:
    return data_dir() / "pilot_tickers.txt"


def overrides_path() -> Path:
    return data_dir() / "overrides.json"


def fetch_log_path() -> Path:
    return data_dir() / "fetch_log.json"


def index_log_path() -> Path:
    return data_dir() / "logs" / "02_index_announcements.log"


def cache_dir() -> Path:
    path = data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def entity_dir(ticker: str) -> Path:
    return data_dir() / "entities" / ticker.upper()


def announcements_csv_filename(ticker: str) -> str:
    return f"{ticker.upper()}_Announcements.csv"


def announcements_csv_path(ticker: str) -> Path:
    ticker = ticker.upper()
    new_path = entity_dir(ticker) / announcements_csv_filename(ticker)
    legacy_path = entity_dir(ticker) / "announcements.csv"
    if new_path.exists():
        return new_path
    if legacy_path.exists():
        return legacy_path
    return new_path


def raw_pdf_path(ticker: str, document_key: str) -> Path:
    safe_key = safe_document_key(document_key)
    return entity_dir(ticker) / "raw" / f"{safe_key}.pdf"


def safe_document_key(document_key: str) -> str:
    """Filesystem-safe form of a Markit documentKey (slashes → underscores)."""
    return document_key.replace("/", "_")


def annual_report_year(date_str: str) -> str:
    """Extract YYYY from an ISO announcement date."""
    return (date_str or "")[:4] or "unknown"


def annual_report_pdf_basename(date_str: str, document_key: str) -> str:
    """Standard annual report PDF filename: {YYYY}_{documentKey}.pdf"""
    return f"{annual_report_year(date_str)}_{safe_document_key(document_key)}.pdf"


def local_annual_report_path(ticker: str, date_str: str, document_key: str) -> Path:
    return entity_dir(ticker) / "annual_reports" / annual_report_pdf_basename(
        date_str, document_key
    )


def s3_annual_report_key(ticker: str, date_str: str, document_key: str) -> str:
    """S3 object key for an annual report PDF."""
    name = annual_report_pdf_basename(date_str, document_key)
    return f"entities/{ticker.upper()}/annual_reports/{name}"


def cdn_pdf_url(document_key: str) -> str:
    return f"{CDN_BASE}/{document_key}&v=undefined"


def find_source_entities_csv() -> Path:
    matches = sorted(docs_dir().glob("ASX_Listed_Companies_*.csv"))
    if not matches:
        raise FileNotFoundError(
            f"No ASX_Listed_Companies_*.csv in {docs_dir()}"
        )
    return matches[-1]


def load_pilot_tickers() -> set[str]:
    path = pilot_tickers_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Pilot list not found: {path}. Create it before using --pilot-only."
        )
    tickers: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tickers.add(line.upper())
    if not tickers:
        raise ValueError(f"No tickers in {path}")
    return tickers


def load_overrides() -> dict[str, Any]:
    path = overrides_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def entity_xid_override(ticker: str, overrides: dict[str, Any]) -> str | None:
    mapping = overrides.get("entity_xid", {})
    value = mapping.get(ticker.upper())
    return str(value) if value not in (None, "") else None


class AsxClient:
    def __init__(self, rate_limit_s: float = 1.0, use_cache: bool = True) -> None:
        self.rate_limit_s = rate_limit_s
        self.use_cache = use_cache
        self._last_request_at = 0.0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.rate_limit_s:
            time.sleep(self.rate_limit_s - elapsed)

    def _cache_path(self, url: str, suffix: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return cache_dir() / f"{digest}{suffix}"

    def get_json(
        self, url: str, *, retries: int = 3, use_cache: bool | None = None
    ) -> dict[str, Any]:
        use_cache = self.use_cache if use_cache is None else use_cache
        cache_file = self._cache_path(url, ".json")
        if use_cache and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        last_error: Exception | None = None
        for attempt in range(retries):
            self._wait()
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self._last_request_at = time.monotonic()
                if use_cache:
                    cache_file.write_text(
                        json.dumps(payload), encoding="utf-8"
                    )
                return payload
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"GET failed after {retries} attempts: {url}") from last_error

    def get_bytes(
        self, url: str, *, retries: int = 3, use_cache: bool | None = None
    ) -> bytes:
        use_cache = self.use_cache if use_cache is None else use_cache
        cache_file = self._cache_path(url, ".bin")
        if use_cache and cache_file.exists():
            return cache_file.read_bytes()

        last_error: Exception | None = None
        for attempt in range(retries):
            self._wait()
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}
            )
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = resp.read()
                self._last_request_at = time.monotonic()
                if use_cache:
                    cache_file.write_bytes(payload)
                return payload
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"GET failed after {retries} attempts: {url}") from last_error


def markets_announcements_url(
    *,
    entity_xid: int | str | None = None,
    page: int = 0,
    items_per_page: int = 100,
) -> str:
    params: dict[str, str | int] = {
        "page": page,
        "itemsPerPage": items_per_page,
    }
    if entity_xid is not None and str(entity_xid).strip():
        params["entityXids"] = str(entity_xid)
    return f"{MARKIT_BASE}/markets/announcements?{urllib.parse.urlencode(params)}"


def item_references_ticker(item: dict[str, Any], ticker: str) -> bool:
    ticker = ticker.upper()
    if (item.get("symbol") or "").upper() == ticker:
        return True
    for sym in item.get("symbolsSecondary") or []:
        if (sym or "").upper() == ticker:
            return True
    for info in item.get("companyInfo") or []:
        if (info.get("symbol") or "").upper() == ticker:
            return True
    return False


def xid_entity_for_ticker(item: dict[str, Any], ticker: str) -> int | None:
    ticker = ticker.upper()
    for info in item.get("companyInfo") or []:
        if (info.get("symbol") or "").upper() == ticker:
            value = info.get("xidEntity")
            if value is not None:
                return int(value)
    if (item.get("symbol") or "").upper() == ticker:
        for info in item.get("companyInfo") or []:
            value = info.get("xidEntity")
            if value is not None:
                return int(value)
    return None


def predictive_search_url(search_text: str) -> str:
    params = urllib.parse.urlencode({"searchText": search_text})
    return f"{MARKIT_BASE}/search/predictive?{params}"


def entity_xid_from_predictive_search(
    client: AsxClient, ticker: str
) -> str | None:
    """Resolve xidEntity via the same lookup the ASX announcements page uses."""
    ticker = ticker.upper()
    payload = client.get_json(predictive_search_url(ticker))
    items = (payload.get("data") or {}).get("items") or []
    for item in items:
        if (item.get("symbol") or "").upper() != ticker:
            continue
        xid = item.get("xidEntity")
        if xid is not None:
            return str(xid)
    return None


def resolve_entity_xid(
    client: AsxClient,
    ticker: str,
    overrides: dict[str, Any],
    *,
    max_scan_pages: int = 500,
) -> str:
    ticker = ticker.upper()
    cached = entity_xid_override(ticker, overrides)
    if cached:
        return cached

    from_predictive = entity_xid_from_predictive_search(client, ticker)
    if from_predictive:
        return from_predictive

    for page in range(max_scan_pages):
        url = markets_announcements_url(page=page, items_per_page=100)
        payload = client.get_json(url)
        items = (payload.get("data") or {}).get("items") or []
        if not items:
            break
        for item in items:
            if not item_references_ticker(item, ticker):
                continue
            xid = xid_entity_for_ticker(item, ticker)
            if xid is not None:
                return str(xid)
    raise RuntimeError(
        f"Could not resolve entity_xid for {ticker} "
        f"(predictive search + {max_scan_pages} market pages). "
        f"Add to {overrides_path()} under entity_xid."
    )


def fetch_all_announcements(
    client: AsxClient, entity_xid: str, *, items_per_page: int = 100
) -> list[dict[str, Any]]:
    entity_xid = str(entity_xid)
    all_items: list[dict[str, Any]] = []
    page = 0
    total: int | None = None

    while True:
        url = markets_announcements_url(
            entity_xid=entity_xid, page=page, items_per_page=items_per_page
        )
        payload = client.get_json(url)
        data = payload.get("data") or {}
        items = data.get("items") or []
        if total is None:
            total = int(data.get("count") or 0)
        if not items:
            break
        all_items.extend(items)
        page += 1
        if total is not None and len(all_items) >= total:
            break
        if len(items) < items_per_page:
            break

    return all_items


def announcement_row(
    ticker: str, entity_xid: str, item: dict[str, Any]
) -> dict[str, str]:
    return {
        "ticker": ticker.upper(),
        "entity_xid": str(entity_xid),
        "documentKey": item.get("documentKey") or "",
        "date": item.get("date") or "",
        "headline": item.get("headline") or "",
        "fileSize": item.get("fileSize") or "",
        "isPriceSensitive": str(item.get("isPriceSensitive", "")).lower(),
        "symbol": item.get("symbol") or "",
        "url": item.get("url") or "",
        "announcementTypes": json.dumps(
            item.get("announcementTypes") or [], ensure_ascii=False
        ),
        "companies": json.dumps(item.get("companies") or [], ensure_ascii=False),
        "companyInfo": json.dumps(
            item.get("companyInfo") or [], ensure_ascii=False
        ),
        "symbolsSecondary": json.dumps(
            item.get("symbolsSecondary") or [], ensure_ascii=False
        ),
    }


def parse_announcement_types(raw: str | None) -> set[str]:
    """Parse the JSON-encoded announcementTypes column from announcements CSV."""
    if not raw:
        return set()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if value}


def is_annual_report_announcement(
    row: dict[str, str],
    *,
    mode: str = "strict",
) -> bool:
    """Return True when an announcements CSV row is an annual report filing.

    Modes:
    - ``loose``: ``announcementTypes`` contains ``Annual Report``.
    - ``strict`` (default): loose match plus either a full-year co-tag or an
      annual-report headline, excluding known false-positive headline patterns
      (Shareholder Review, Form 20-F, sustainability summaries, etc.).
    """
    if mode not in {"loose", "strict"}:
        raise ValueError(f"unsupported annual report filter mode: {mode}")

    types = parse_announcement_types(row.get("announcementTypes"))
    if "Annual Report" not in types:
        return False
    if mode == "loose":
        return True

    headline = row.get("headline") or ""
    if ANNUAL_REPORT_HEADLINE_EXCLUDE_RE.search(headline):
        return False
    if types & FULL_YEAR_ANNOUNCEMENT_TYPES:
        return True
    return bool(ANNUAL_REPORT_HEADLINE_INCLUDE_RE.search(headline))


BURN_EXIT_CODE = 2


def http_error_status(exc: BaseException) -> int | None:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    return None


class CdnBurnTracker:
    """Detect CDN IP burn (sustained 429/503) during fetch or soak runs."""

    def __init__(
        self,
        *,
        window_size: int = 50,
        burn_error_pct: float = 1.0,
        consecutive_429_limit: int = 5,
        min_requests: int = 20,
        simulate_burn_after: int | None = None,
    ) -> None:
        self.window_size = window_size
        self.burn_error_pct = burn_error_pct
        self.consecutive_429_limit = consecutive_429_limit
        self.min_requests = min_requests
        self.simulate_burn_after = simulate_burn_after
        self._simulated_burn = False
        self._window: list[str] = []
        self._consecutive_429 = 0
        self.total_requests = 0
        self.counts = {
            "success": 0,
            "429": 0,
            "503": 0,
            "other_errors": 0,
        }

    def record_success(self) -> bool:
        self.total_requests += 1
        self.counts["success"] += 1
        self._consecutive_429 = 0
        self._push("ok")
        if (
            self.simulate_burn_after is not None
            and self.counts["success"] >= self.simulate_burn_after
        ):
            self._simulated_burn = True
            return True
        return self.is_burned()

    def record_error(self, status: int | None) -> bool:
        self.total_requests += 1
        if status == 429:
            self.counts["429"] += 1
            self._consecutive_429 += 1
            self._push("429")
        elif status == 503:
            self.counts["503"] += 1
            self._consecutive_429 = 0
            self._push("503")
        else:
            self.counts["other_errors"] += 1
            self._consecutive_429 = 0
            self._push("other")
        return self.is_burned()

    def _push(self, kind: str) -> None:
        self._window.append(kind)
        if len(self._window) > self.window_size:
            self._window.pop(0)

    def is_burned(self) -> bool:
        if self._simulated_burn:
            return True
        if self._consecutive_429 >= self.consecutive_429_limit:
            return True
        if self.total_requests < self.min_requests:
            return False
        window = self._window[-self.window_size :]
        if len(window) < self.min_requests:
            return False
        rate_errors = sum(1 for kind in window if kind in ("429", "503"))
        err_pct = 100.0 * rate_errors / len(window)
        return err_pct > self.burn_error_pct

    def snapshot(self) -> dict[str, int | float | bool]:
        err = self.counts["429"] + self.counts["503"] + self.counts["other_errors"]
        err_pct = (100.0 * err / self.total_requests) if self.total_requests else 0.0
        return {
            **self.counts,
            "requests": self.total_requests,
            "error_pct": round(err_pct, 2),
            "burned": self.is_burned(),
            "simulated_burn": self._simulated_burn,
            "consecutive_429": self._consecutive_429,
        }


def normalise_market_cap(value: str) -> str:
    value = (value or "").strip()
    if not value or value.upper() == "SUSPENDED":
        return value.upper() if value.upper() == "SUSPENDED" else ""
    if re.fullmatch(r"-?\d+", value):
        return value
    return value
