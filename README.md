# Fashion Competitor Tracker — CSV-only, GitHub Actions

Tracks public product observations for Zara, H&M, Snitch and Urbanic. **No database is used.** CSV files committed to the Git repository provide persistent history.

## What is saved

- `data/daily/YYYY-MM-DD.csv`: daily snapshot. A rerun on the same date replaces that date's snapshot; older dates remain.
- `data/product_observations.csv`: cumulative product-by-day observations, used for historical comparisons.
- `data/product_history.csv`: one row per unique product, including first/last observed date and first/latest observed price.
- `reports/monthly_launch_summary.csv`: monthly count of products first observed by this tracker.
- `reports/monthly_price_summary.csv`: observed price ranges by brand/category/month.
- `reports/monthly_fit_summary.csv`: fit labels detected in product text.
- `reports/monthly_size_availability.csv`: size labels seen on product pages (not units sold).

`research_template.csv` is for manually recorded target customer, brand differentiation and IP research with evidence URLs. The scraper does not invent these facts.

## Set up the GitHub repository

1. Create a repository on GitHub, e.g. `fashion-market-tracker`.
2. Upload all files/folders from this project, including the hidden `.github/workflows/daily-tracker.yml`.
3. Ensure the repository's default branch contains the workflow.
4. Open **Settings → Actions → General → Workflow permissions**. Choose **Read and write permissions** if your organization/repository policy allows it.
5. Open **Actions**, select **Daily Fashion Competitor Tracker**, then choose **Run workflow** to test it.
6. After a successful run, inspect the commit history and `data/` and `reports/` folders.

The workflow has `contents: write` permission and commits changes automatically. If repository policy disallows write access, the push step will fail until permissions are corrected.

## Schedule

The workflow runs daily at `02:30 UTC`, which is `08:00 IST`. GitHub scheduled jobs can be delayed during busy periods. Use **Run workflow** for a manual run.

## Local run

Python 3.10+ is recommended.

```bash
python -m venv .venv
```

Activate the virtual environment:

Windows PowerShell:
```powershell
.venv\Scripts\Activate.ps1
```

Linux/macOS:
```bash
source .venv/bin/activate
```

Install and run:

```bash
pip install -r requirements.txt
playwright install chromium
python tracker.py run --brand "H&M"
python tracker.py run
python tracker.py report
```

## Important limitations

- This uses generic product-link detection and JSON-LD/meta price extraction. It may find few or zero products on some websites, because page structures vary. Replace listing URLs in `config.json` with appropriate current category/new-arrival URLs and implement brand-specific selectors if needed.
- If a run returns no products, the script intentionally does not update history. Check the Actions log and adjust selectors/URLs rather than treating a failed scrape as a zero-product day.
- `first_seen_date` means first seen by this tracker, not a confirmed launch date.
- Size labels are not size-level sales. A displayed size or an unavailable size cannot prove which size sold the most.
- Public web prices do not reveal internal manufacturing cost, advertising cost, realised sales or profit margins.
- Age targeting, differentiation and IP/licensing details require separate research with reliable public evidence. Use `research_template.csv` and include source URLs.
- Check each website's terms and robots.txt, keep request rates low, and don't bypass CAPTCHAs, login restrictions or anti-bot protections. If automated access is disallowed, use an authorized feed/API or manual data import.
- CSV history grows over time and each GitHub run checks out and commits the repository. For this small four-brand project it should be manageable initially, but very large histories may eventually make CSV processing and Git history less convenient.


## Add as many category/listing URLs as you need

Edit `config.json`. Each brand has a `listing_urls` array. Add every permitted category or new-arrivals URL you want monitored under that brand. For example:

```json
{
  "name": "H&M",
  "listing_urls": [
    "https://www2.hm.com/en_in/men/new-arrivals/view-all.html",
    "PASTE_THE_CURRENT_HM_WOMEN_LISTING_URL_HERE",
    "PASTE_THE_CURRENT_HM_KIDS_LISTING_URL_HERE",
    "PASTE_THE_CURRENT_HM_HOME_LISTING_URL_HERE"
  ]
}
```

Replace the placeholder strings with real URLs copied from the official website before running. Repeat this for Zara, Snitch and Urbanic. The scraper visits all configured listing URLs, combines discovered product links, and deduplicates canonical product URLs before inspecting product pages.

`max_products_per_brand_per_run` is set to `0` in the supplied config, meaning no per-brand product cap is applied to the links found on your configured pages. Set it to a positive number (for example `100`) if you want a limit for testing.

**Scope note:** adding many category URLs does not automatically crawl every page of the entire domain. Each supplied listing page must expose its product links to the generic link detector. If a category uses pagination, “load more”, infinite scrolling, or site-specific APIs, the code may need a site-specific pagination/scrolling adapter. Validate each section in the Actions logs. Only scrape where the website's terms and crawling rules permit.


## Current configured brands
- BearHouse: `https://thebearhouse.com/collections/all`
- BonkersCorner: `https://www.bonkerscorner.com/collections/mens-new-arrivals`
- Snitch: `https://www.snitch.com/shop`

The scraper supports common product URL patterns and performs a small number of page scrolls
to trigger lazy-loaded product cards. It writes `reports/scrape_status.csv` on every run,
including when a listing is blocked or no product links are found. Check this file first
when a scheduled run produces no new observations.

A public URL can still return HTTP 403 to automated requests. The tracker records this status
and does not attempt to bypass access controls. It also does not guarantee complete catalogue
coverage; verify the extracted product URLs and prices against the public pages.
