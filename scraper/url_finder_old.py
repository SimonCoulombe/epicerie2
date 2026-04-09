"""URL finder — discovers product page URLs for all 4 store chains.

Usage:
    # Search for a single product by name
    python -m scraper.url_finder "Bœuf haché"

    # Batch mode: find URLs for all products in products.csv that lack targets
    python -m scraper.url_finder --batch

    # Batch with limit (useful for testing)
    python -m scraper.url_finder --batch --limit 5

Output is CSV rows ready to append to config/targets.csv.
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

PRODUCTS_CSV = Path(__file__).resolve().parent.parent / "config" / "products.csv"
TARGETS_CSV = Path(__file__).resolve().parent.parent / "config" / "targets.csv"

STORES = {
    "superc": {
        "slug": "superc-default",
        "search_url": "https://www.superc.ca/recherche?filter={query}",
        "product_pattern": re.compile(r"https://www\.superc\.ca/allees/.+/p/\d+"),
        "use_playwright": "true",
    },
    "maxi": {
        "slug": "maxi-default",
        "search_url": "https://www.maxi.ca/fr/search?search-bar={query}",
        "product_pattern": re.compile(r"https://www\.maxi\.ca/fr/.+/p/.+"),
        "use_playwright": "true",
    },
    "metro": {
        "slug": "metro-default",
        "search_url": "https://www.metro.ca/epicerie-en-ligne/recherche?filter={query}",
        "product_pattern": re.compile(r"https://www\.metro\.ca/epicerie-en-ligne/allees/.+/p/\d+"),
        "use_playwright": "true",
    },
    "iga": {
        "slug": "iga-default",
        "search_url": "https://www.iga.ca/fr?query={query}&tab=products",
        "product_pattern": re.compile(r"https://www\.iga\.ca/fr/produits/.+"),
        "use_playwright": "false",
    },
}

# Strip query params (tracking, source, etc.) from URLs
_STRIP_PARAMS = re.compile(r"\?.*$")


def _clean_url(url: str) -> str:
    return _STRIP_PARAMS.sub("", url)


def _load_existing_targets() -> set[tuple[str, str]]:
    """Return set of (product_slug, store_slug) already in targets.csv."""
    existing = set()
    if TARGETS_CSV.exists():
        with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((row["product_slug"], row["store_slug"]))
    return existing


def _load_products() -> list[dict]:
    """Load products from products.csv."""
    with open(PRODUCTS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _products_missing_targets(existing: set[tuple[str, str]]) -> list[dict]:
    """Return products that don't have targets for all 4 stores."""
    products = _load_products()
    missing = []
    for p in products:
        store_slugs_with_target = {s for (ps, s) in existing if ps == p["slug"]}
        all_store_slugs = {info["slug"] for info in STORES.values()}
        if not all_store_slugs.issubset(store_slugs_with_target):
            missing.append(p)
    return missing


async def search_product(browser, store_key: str, search_term: str) -> list[str]:
    """Search a store for a product, return list of matching product URLs."""
    info = STORES[store_key]
    search_url = info["search_url"].format(query=quote(search_term))
    pattern = info["product_pattern"]

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="fr-CA",
        viewport={"width": 1920, "height": 1080},
    )
    page = await context.new_page()
    await page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
    )
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(5000)

        links = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )

        seen = set()
        matches = []
        for url in links:
            cleaned = _clean_url(url)
            if pattern.match(cleaned) and cleaned not in seen:
                seen.add(cleaned)
                matches.append(cleaned)
        return matches
    except Exception as e:
        print(f"  [{store_key}] ERROR: {e}", file=sys.stderr)
        return []
    finally:
        await page.close()
        await context.close()


async def find_single(search_term: str) -> None:
    """Search all stores for a single product and print CSV candidates."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        print(f"# Searching for: {search_term}", file=sys.stderr)
        print("product_slug,store_slug,url,use_playwright,parser")

        for store_key, info in STORES.items():
            matches = await search_product(browser, store_key, search_term)
            if matches:
                # Print first match as the main candidate
                print(f"SLUG,{info['slug']},{matches[0]},{info['use_playwright']},{store_key}")
                for alt in matches[1:3]:
                    print(f"# alt: {alt}", file=sys.stderr)
            else:
                print(f"# [{store_key}] NOT FOUND", file=sys.stderr)
            await asyncio.sleep(2)

        await browser.close()


async def find_batch(limit: int | None = None) -> None:
    """Find URLs for all products missing targets. Append to targets.csv."""
    existing = _load_existing_targets()
    missing = _products_missing_targets(existing)

    if not missing:
        print("All products already have targets for all 4 stores.", file=sys.stderr)
        return

    if limit:
        missing = missing[:limit]

    print(f"Finding URLs for {len(missing)} products...", file=sys.stderr)

    new_rows: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for i, product in enumerate(missing):
            slug = product["slug"]
            name = product["name"]
            print(f"\n[{i+1}/{len(missing)}] {slug} (search: '{name}')", file=sys.stderr)

            for store_key, info in STORES.items():
                if (slug, info["slug"]) in existing:
                    print(f"  [{store_key}] already has target, skipping", file=sys.stderr)
                    continue

                matches = await search_product(browser, store_key, name)
                if matches:
                    url = matches[0]
                    new_rows.append({
                        "product_slug": slug,
                        "store_slug": info["slug"],
                        "url": url,
                        "use_playwright": info["use_playwright"],
                        "parser": store_key,
                    })
                    print(f"  [{store_key}] {url}", file=sys.stderr)
                    if len(matches) > 1:
                        for alt in matches[1:3]:
                            print(f"    alt: {alt}", file=sys.stderr)
                else:
                    print(f"  [{store_key}] NOT FOUND", file=sys.stderr)

                await asyncio.sleep(2)  # Rate limiting

        await browser.close()

    if not new_rows:
        print("\nNo new URLs found.", file=sys.stderr)
        return

    # Append to targets.csv
    file_exists = TARGETS_CSV.exists()
    with open(TARGETS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["product_slug", "store_slug", "url",
                                                "use_playwright", "parser"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    print(f"\nAppended {len(new_rows)} new rows to {TARGETS_CSV}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Find product URLs on grocery store sites")
    parser.add_argument("search_term", nargs="?", help="Product name to search for")
    parser.add_argument("--batch", action="store_true",
                        help="Find URLs for all products missing targets")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max products to process in batch mode")
    args = parser.parse_args()

    if args.batch:
        asyncio.run(find_batch(args.limit))
    elif args.search_term:
        asyncio.run(find_single(args.search_term))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
