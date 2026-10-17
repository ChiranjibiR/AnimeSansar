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
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def make_key(brand: str, url: str) -> str:
    return f"{brand.strip().lower()}|{canonical_url(url)}"


def parse_price(value):
    if value is None:
        return "", ""
    s = str(value).strip()
    currency = ""
    for token, code in [("₹", "INR"), ("Rs.", "INR"), ("INR", "INR"),
                        ("€", "EUR"), ("£", "GBP"), ("$", "USD")]:
        if token.lower() in s.lower():
            currency = code
            break
    cleaned = re.sub(r"[^0-9.,]", "", s).replace(",", "")
    try:
        return str(float(cleaned)), currency
    except ValueError:
        return "", currency


def jsonld_product(objects):
    queue = list(objects)
    while queue:
        obj = queue.pop(0)
        if isinstance(obj, list):
            queue.extend(obj)
        elif isinstance(obj, dict):
            types = obj.get("@type", [])
            if not isinstance(types, list):
                types = [types]
            if any(str(t).lower() == "product" for t in types):
                return obj
            graph = obj.get("@graph")
            if graph:
                queue.extend(graph if isinstance(graph, list) else [graph])
    return {}


async def read_jsonld(page):
    found = []
    scripts = page.locator('script[type="application/ld+json"]')
    try:
        for i in range(min(await scripts.count(), 50)):
            raw = await scripts.nth(i).text_content(timeout=1500)
            if raw:
                try:
                    found.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    except Exception:
        pass
    return found


async def discover_product_links(page, listing_url: str, brand_cfg: dict | None = None,
                                max_scrolls: int = 4) -> list[str]:
    """
    Discover product URLs from a public listing page.

    Supports common Shopify-style /products/ URLs and several common product
    URL patterns. It also scrolls a limited number of times to trigger lazy-loaded
    product cards. This does not attempt to bypass HTTP 403s, CAPTCHAs, or other
    access restrictions.
    """
    brand_cfg = brand_cfg or {}
    host = urlparse(listing_url).netloc.lower().removeprefix("www.")
    configured_patterns = brand_cfg.get("product_url_patterns", [])
    patterns = tuple(p.lower() for p in (
        configured_patterns or [
            "/products/", "/product/", "/product-detail/", "/product-details/",
            "/productpage", "/product-page/", "/p/", "/item/"
        ]
    ))

    # Some storefronts load additional product cards while scrolling.
    for _ in range(max(0, int(max_scrolls))):
        try:
            before = await page.locator("a[href]").count()
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)
            after = await page.locator("a[href]").count()
            if after <= before:
                break
        except Exception:
            break

    found = set()
    try:
        links = await page.locator("a[href]").evaluate_all(
            """(els) => els.map(a => ({
                href: a.href,
                text: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                cls: String(a.className || ''),
                parentCls: String(a.parentElement?.className || ''),
                grandparentCls: String(a.parentElement?.parentElement?.className || '')
            })).filter(a => a.href)"""
        )
    except Exception:
        links = []

    # Extract product URLs referenced by JSON-LD Product records, when present.
    try:
        structured = await read_jsonld(page)
        queue = list(structured)
        while queue:
            obj = queue.pop(0)
            if isinstance(obj, list):
                queue.extend(obj)
            elif isinstance(obj, dict):
                types = obj.get("@type", [])
                if not isinstance(types, list):
                    types = [types]
                if any(str(t).lower() == "product" for t in types):
                    product_url = obj.get("url")
                    if isinstance(product_url, str) and product_url.strip():
                        found.add(urljoin(listing_url, product_url.strip()))
                for key in ("@graph", "itemListElement", "item", "mainEntity"):
                    child = obj.get(key)
                    if child:
                        queue.extend(child if isinstance(child, list) else [child])
    except Exception:
        pass

    for item in links:
        href = item.get("href", "")
        if not href.startswith("http"):
            href = urljoin(listing_url, href)
        parsed = urlparse(href)
        if parsed.netloc.lower().removeprefix("www.") != host:
            continue
        path = parsed.path.lower()
        if not any(p in path for p in patterns):
            continue
        if any(x in path for x in (
            "/search", "/help", "/cart", "/login", "/customer-service",
            "/account", "/checkout", "/wishlist", "/collections/"
        )):
            continue
        # Avoid non-product navigation links even if their path happens to match.
        clean = canonical_url(href)
        if clean:
            found.add(clean)

    # Keep only URLs on the same host and with a recognized product path.
    output = []
    for href in found:
        parsed = urlparse(href)
        if parsed.netloc.lower().removeprefix("www.") != host:
            continue
        path = parsed.path.lower()
        if any(p in path for p in patterns) and not any(
            x in path for x in ("/search", "/help", "/cart", "/login", "/customer-service",
                                "/account", "/checkout", "/wishlist", "/collections/")
        ):
            output.append(canonical_url(href))
    return sorted(set(output))


def infer_category(text: str, url: str) -> str:
    s = f"{text} {url}".lower()
    rules = [
        ("T-shirt", ["t-shirt", "tshirt", "tee"]),
        ("Shirt", ["shirt", "overshirt"]),
        ("Jeans/Denim", ["jeans", "denim"]),
        ("Trousers", ["trouser", "pants", "chino"]),
        ("Dress", ["dress"]),
        ("Top/Blouse", ["blouse", "crop top", " top "]),
        ("Jacket/Outerwear", ["jacket", "coat", "outerwear", "blazer"]),
        ("Hoodie/Sweatshirt", ["hoodie", "sweatshirt", "sweater", "jumper"]),
        ("Shorts", ["shorts"]),
        ("Footwear", ["shoe", "sneaker", "footwear", "sandal"]),
        ("Accessories", ["bag", "belt", "cap", "hat", "accessory"]),
    ]
    for category, words in rules:
        if any(w in f" {s} " for w in words):
            return category
    return "Unclassified"


def infer_fit(text: str) -> list[str]:
    s = text.lower()
    labels = [
        "oversized", "relaxed fit", "regular fit", "slim fit", "skinny fit",
        "straight fit", "loose fit", "boxy fit", "wide leg", "tapered fit",
        "cropped", "comfort fit"
    ]
    return [label for label in labels if label in s]


def infer_sizes(text: str) -> list[str]:
    # Heuristic only: labels may occur in descriptions, not necessarily selectable stock.
    candidates = ["XXXS", "XXS", "XXL", "XXXL", "XS", "XL", "S", "M", "L",
                  "2XS", "3XS", "2XL", "3XL", "4XL"]
    found = []
    for size in candidates:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(size)}(?![A-Za-z0-9])", text, re.I):
            found.append(size)
    return list(dict.fromkeys(found))


async def scrape_product(page, brand: str, url: str, listing_url: str):
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(700)
        status = str(response.status) if response else "unknown"
        title = normalize(await page.title())
        body = normalize(await page.locator("body").inner_text(timeout=5000))
        product = jsonld_product(await read_jsonld(page))

        name = normalize(product.get("name"))
        if not name:
            try:
                name = normalize(await page.locator('meta[property="og:title"]').get_attribute("content"))
            except Exception:
                name = title

        pid = normalize(product.get("sku") or product.get("productID") or product.get("mpn"))
        offers = product.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        offers = offers if isinstance(offers, dict) else {}
        price, inferred_currency = parse_price(offers.get("price"))
        currency = normalize(offers.get("priceCurrency")) or inferred_currency
        if not price:
            for selector in (
                'meta[property="product:price:amount"]',
                'meta[property="og:price:amount"]',
                'meta[itemprop="price"]',
            ):
                try:
                    val = await page.locator(selector).first.get_attribute("content", timeout=1000)
                    price, inferred = parse_price(val)
                    currency = currency or inferred
                    if price:
                        break
                except Exception:
                    pass

        if not name and not pid:
            return None

        return {
            "brand": brand,
            "product_key": make_key(brand, url),
            "product_url": canonical_url(url),
            "product_id": pid,
            "product_name": name,
            "category": infer_category(name + " " + body[:2500], url),
            "price": price,
            "currency": currency or "INR",
            "fit_labels": "|".join(infer_fit(body)),
            "sizes_observed": "|".join(infer_sizes(body)),
            "http_status": status,
            "source_listing_url": listing_url,
        }
    except PlaywrightTimeoutError:
        print(f"  [timeout] {url}")
    except Exception as exc:
        print(f"  [page error] {url}: {exc}")
    return None


async def scrape_all(brand_filter: str | None = None) -> list[dict]:
    config = load_config()
    settings = config["settings"]
    limit = int(settings.get("max_products_per_brand_per_run", 0))
    delay = float(settings.get("delay_seconds_between_pages", 2))
    timeout = int(settings.get("timeout_ms", 45000))
    max_scrolls = int(settings.get("max_scrolls_per_listing", 4))
    records = []
    status_rows = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=bool(settings.get("headless", True)))
        context = await browser.new_context(
            locale="en-IN",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"}
        )
        page = await context.new_page()
        page.set_default_timeout(timeout)

        for brand_cfg in config["brands"]:
            brand = brand_cfg["name"]
            if brand_filter and brand.lower() != brand_filter.lower():
                continue
            print(f"\n=== {brand} ===")
            links_and_sources = []
            brand_listing_statuses = []

            for listing_url in brand_cfg.get("listing_urls", []):
                print(f"Listing: {listing_url}")
                http_status = ""
                candidate_count = 0
                outcome = "unknown"
                error_message = ""
                try:
                    response = await page.goto(
                        listing_url, wait_until="domcontentloaded", timeout=timeout
                    )
                    await page.wait_for_timeout(1800)
                    http_status = str(response.status) if response else "no_response"
                    if response and response.status >= 400:
                        outcome = f"http_{response.status}"
                        error_message = "Server returned an HTTP error; listing was not scraped."
                        print(f"  HTTP {response.status}; skipping listing")
                    else:
                        found = await discover_product_links(
                            page, listing_url, brand_cfg=brand_cfg, max_scrolls=max_scrolls
                        )
                        candidate_count = len(found)
                        outcome = "products_found" if found else "no_candidate_links"
                        print(f"  Candidate links: {candidate_count}")
                        links_and_sources.extend((url, listing_url) for url in found)
                except Exception as exc:
                    outcome = "listing_error"
                    error_message = normalize(str(exc))[:500]
                    print(f"  [listing error] {error_message}")
                status = {
                    "run_date": today_local(),
                    "brand": brand,
                    "listing_url": listing_url,
                    "http_status": http_status,
                    "candidate_links": candidate_count,
                    "outcome": outcome,
                    "error_message": error_message
                }
                status_rows.append(status)
                brand_listing_statuses.append(status)
                await asyncio.sleep(delay)

            unique = {}
            for url, source in links_and_sources:
                unique.setdefault(url, source)
            candidates = list(unique.items())
            if limit > 0:
                candidates = candidates[:limit]
            print(f"  Inspecting {len(candidates)} unique product pages from all configured listing URLs"
                  + (f" (limit={limit})" if limit > 0 else " (no per-brand cap)"))

            extracted_by_source = defaultdict(int)
            for index, (url, source) in enumerate(candidates, 1):
                item = await scrape_product(page, brand, url, source)
                if item:
                    records.append(item)
                    extracted_by_source[source] += 1
                    print(f"  [{index}/{len(candidates)}] {item['product_name'][:70]} | "
                          f"{item['price']} {item['currency']}")
                await asyncio.sleep(delay)

            for status in brand_listing_statuses:
                status["products_extracted_from_listing"] = extracted_by_source.get(
                    status["listing_url"], 0
                )
                if status["outcome"] == "products_found" and not status["products_extracted_from_listing"]:
                    status["outcome"] = "candidate_pages_failed_extraction"

        await context.close()
        await browser.close()

    status_path = REPORTS_DIR / "scrape_status.csv"
    write_csv(status_path, status_rows, [
        "run_date", "brand", "listing_url", "http_status", "candidate_links",
        "products_extracted_from_listing", "outcome", "error_message"
    ])
    print(f"Wrote scrape diagnostics: {status_path.relative_to(ROOT)}")
    return records


def merge_observations(today_rows: list[dict], run_date: str) -> list[dict]:
    observations_path = DATA_DIR / "product_observations.csv"
    previous = read_csv(observations_path)
    # Avoid duplicate product rows if the workflow is manually rerun on the same date.
    existing_keys = {(r.get("observed_date", ""), r.get("product_key", "")) for r in previous}
    stamp = utc_now()
    new_rows = []
    for item in today_rows:
        key = (run_date, item["product_key"])
        if key in existing_keys:
            continue
        row = {
            "observed_date": run_date,
            "observed_at_utc": stamp,
            **item
        }
        new_rows.append(row)
        existing_keys.add(key)
    combined = previous + new_rows
    combined.sort(key=lambda r: (r.get("observed_date", ""), r.get("brand", ""), r.get("product_key", "")))
    write_csv(observations_path, combined, OBS_FIELDS)

    # Daily snapshot is one file per date. A rerun on the same date replaces that day's snapshot.
    daily_path = DAILY_DIR / f"{run_date}.csv"
    daily_rows = [{"observed_date": run_date, "observed_at_utc": stamp, **item} for item in today_rows]
    daily_rows.sort(key=lambda r: (r.get("brand", ""), r.get("product_key", "")))
    write_csv(daily_path, daily_rows, DAILY_FIELDS)
    print(f"Added {len(new_rows)} new product-day observations; cumulative rows: {len(combined)}")
    print(f"Wrote daily snapshot: {daily_path.relative_to(ROOT)}")


def rebuild_history_and_reports():
    observations = read_csv(DATA_DIR / "product_observations.csv")
    by_key = defaultdict(list)
    for row in observations:
        by_key[row.get("product_key", "")].append(row)

    history = []
    for key, rows in by_key.items():
        rows.sort(key=lambda r: (r.get("observed_date", ""), r.get("observed_at_utc", "")))
        first, last = rows[0], rows[-1]
        history.append({
            "brand": last.get("brand", ""),
            "product_key": key,
            "product_url": last.get("product_url", ""),
            "product_id": last.get("product_id", ""),
            "product_name": last.get("product_name", ""),
            "category": last.get("category", ""),
            "first_seen_date": first.get("observed_date", ""),
            "last_seen_date": last.get("observed_date", ""),
            "first_seen_price": first.get("price", ""),
            "latest_price": last.get("price", ""),
            "currency": last.get("currency", ""),
            "fit_labels_latest": last.get("fit_labels", ""),
            "sizes_observed_latest": last.get("sizes_observed", ""),
            "source_listing_url": last.get("source_listing_url", ""),
        })
    history.sort(key=lambda r: (r["brand"], r["first_seen_date"], r["product_name"]))
    write_csv(DATA_DIR / "product_history.csv", history, HISTORY_FIELDS)

    # Summary by first-observed month. This is a proxy for additions detected by the tracker.
    monthly_counts = defaultdict(list)
    for row in history:
        month = row.get("first_seen_date", "")[:7]
        if month:
            monthly_counts[(month, row.get("brand", ""))].append(row)

    launch_summary = []
    for (month, brand), rows in sorted(monthly_counts.items()):
        prices = []
        for r in rows:
            try:
                if r.get("first_seen_price"):
                    prices.append(float(r["first_seen_price"]))
            except ValueError:
                pass
        launch_summary.append({
            "month": month,
            "brand": brand,
            "newly_observed_unique_products": len(rows),
            "categories_seen": "|".join(sorted({r.get("category", "") for r in rows if r.get("category")})),
            "median_first_observed_price": round(sorted(prices)[len(prices)//2], 2) if prices else "",
            "note": "First observed by tracker; not a verified brand launch date."
        })
    write_csv(REPORTS_DIR / "monthly_launch_summary.csv", launch_summary, [
        "month", "brand", "newly_observed_unique_products", "categories_seen",
        "median_first_observed_price", "note"
    ])

    # Observed price ranges per month and category (each row is a product-day observation).
    price_groups = defaultdict(list)
    fit_groups = defaultdict(set)
    size_groups = defaultdict(set)
    for row in observations:
        month = row.get("observed_date", "")[:7]
        if not month:
            continue
        brand, category = row.get("brand", ""), row.get("category", "") or "Unclassified"
        try:
            if row.get("price") not in ("", None):
                price_groups[(month, brand, category)].append(float(row["price"]))
        except ValueError:
            pass
        for fit in filter(None, (row.get("fit_labels", "")).split("|")):
            fit_groups[(month, brand)].add(fit)
        for size in filter(None, (row.get("sizes_observed", "")).split("|")):
            size_groups[(month, brand)].add(size)

    price_summary = []
    for (month, brand, category), prices in sorted(price_groups.items()):
        price_summary.append({
            "month": month, "brand": brand, "category": category,
            "observations_with_price": len(prices),
            "min_observed_price": min(prices), "median_observed_price": sorted(prices)[len(prices)//2],
            "max_observed_price": max(prices),
            "note": "Observed web prices, not verified transaction prices or profit margins."
        })
    write_csv(REPORTS_DIR / "monthly_price_summary.csv", price_summary, [
        "month", "brand", "category", "observations_with_price",
        "min_observed_price", "median_observed_price", "max_observed_price", "note"
    ])

    fit_rows = []
    for (month, brand), labels in sorted(fit_groups.items()):
        fit_rows.append({"month": month, "brand": brand, "fit_labels_observed": "|".join(sorted(labels)),
                         "note": "Heuristic text detection; validate on product pages."})
    write_csv(REPORTS_DIR / "monthly_fit_summary.csv", fit_rows,
              ["month", "brand", "fit_labels_observed", "note"])

    size_rows = []
    for (month, brand), labels in sorted(size_groups.items()):
        size_rows.append({"month": month, "brand": brand, "size_labels_observed": "|".join(sorted(labels)),
                          "note": "Observed size labels, NOT units sold or best-selling sizes."})
    write_csv(REPORTS_DIR / "monthly_size_availability.csv", size_rows,
              ["month", "brand", "size_labels_observed", "note"])

    print(f"Rebuilt product history for {len(history)} unique products.")
    print("Reports rebuilt under reports/.")


async def command_run(brand_filter: str | None):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    run_date = today_local()
    rows = await scrape_all(brand_filter)
    if not rows:
        print("WARNING: no products were extracted. See reports/scrape_status.csv for per-URL diagnostics.")
        print("No empty/failed scrape will be treated as proof that a brand has no products.")
        # Preserve prior product history; still refresh report files from existing observations.
        rebuild_history_and_reports()
        return
    merge_observations(rows, run_date)
    rebuild_history_and_reports()


def command_report():
    rebuild_history_and_reports()


def main():
    parser = argparse.ArgumentParser(description="CSV-only fashion competitor tracker")
    parser.add_argument("command", choices=["run", "report"])
    parser.add_argument("--brand", help="Optional brand: Zara, H&M, Snitch, Urbanic")
    args = parser.parse_args()
    if args.command == "run":
        asyncio.run(command_run(args.brand))
    else:
        command_report()


if __name__ == "__main__":
    main()
