#!/usr/bin/env python3
"""
CSV-only fashion competitor tracker.

Persistent storage is kept in the Git repository:
  data/daily/YYYY-MM-DD.csv          one daily snapshot (same-day reruns replace that date)
  data/product_observations.csv      cumulative product-by-day observations
  data/product_history.csv           one row per product with first/last observed dates
  reports/monthly_launch_summary.csv monthly first-observed product counts
  reports/monthly_price_summary.csv  observed price ranges by brand/category/month
  reports/monthly_fit_summary.csv    observed fit labels by brand/month
  reports/monthly_size_availability.csv size labels observed on product pages (NOT sales)

No database is used. Each run reads the existing CSVs, merges today's observations,
rebuilds the derived CSV reports, and GitHub Actions commits the changes.

Caveats:
- First-seen means first observed by this tracker, not verified launch date.
- Generic product extraction may need per-site adjustments.
- Size availability is not units sold; cannot prove best-selling size.
- Target demographics, verified unit economics, and full IP/licensing details
  require separate research with cited public sources.
- Respect website terms, robots.txt, and rate limits. Never bypass CAPTCHAs,
  anti-bot measures, login walls, or access restrictions.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"

OBS_FIELDS = [
    "observed_date", "observed_at_utc", "brand", "product_key", "product_url",
    "product_id", "product_name", "category", "price", "currency",
    "fit_labels", "sizes_observed", "http_status", "source_listing_url"
]
HISTORY_FIELDS = [
    "brand", "product_key", "product_url", "product_id", "product_name", "category",
    "first_seen_date", "last_seen_date", "first_seen_price", "latest_price",
    "currency", "fit_labels_latest", "sizes_observed_latest", "source_listing_url"
]
DAILY_FIELDS = OBS_FIELDS[:]
DATA_DIR = ROOT / "data"
DAILY_DIR = DATA_DIR / "daily"
REPORTS_DIR = ROOT / "reports"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_local() -> str:
    return date.today().isoformat()


def read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    tmp.replace(path)


def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def normalize(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def canonical_url(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
