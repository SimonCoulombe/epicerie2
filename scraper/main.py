"""Scraper CLI entry point — reads config, scrapes prices, stores in DuckDB.

Supports parallel scraping: one async worker per store chain with rate limiting.
Retries failed fetches up to 2 times with exponential backoff.
"""

import asyncio
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx
import yaml

from scraper.browser import PlaywrightBrowser
from scraper.db import sync_targets, get_active_targets, upsert_price, update_target_status
from scraper.parsers import PARSERS, PriceResult

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "targets.yaml"

# Inter-request delay per chain (seconds)
_CHAIN_DELAY = {
    "IGA": 1.0,
}
_DEFAULT_DELAY = 2.0

# Retry config
_MAX_RETRIES = 2
_RETRY_BASE_DELAY = 3.0  # seconds, doubles each retry


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_parser_key(target: dict) -> str:
    """Determine parser key from DB parser field, falling back to store slug prefix."""
    parser = target.get("parser", "auto")
    if parser and parser != "auto":
        return parser
    return target["store_slug"].split("-")[0]


def _store_cookies(target: dict) -> dict[str, str]:
    """Build store-selection cookies for httpx based on chain."""
    chain = target.get("chain_name", "")
    store_id = target.get("chain_store_id", "")
    if chain == "IGA" and store_id:
        return {
            "storeId": f"{store_id}_Quebec",
            "selected_store_region": "Quebec",
        }
    return {}


async def _fetch_plain(url: str, target: dict | None = None) -> str:
    """Fetch HTML directly via httpx (no JS rendering)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5",
    }
    cookies = _store_cookies(target) if target else {}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers, cookies=cookies)
        resp.raise_for_status()
        return resp.text


async def _scrape_one(target: dict, browser: PlaywrightBrowser, today: date) -> bool:
    """Scrape a single target with retries. Returns True on success."""
    chain = target["chain_name"]
    parser_key = _get_parser_key(target)
    parser_fn = PARSERS.get(parser_key)

    if parser_fn is None:
        print(f"[SKIP] {chain}: no parser for '{parser_key}'")
        return False

    for attempt in range(_MAX_RETRIES + 1):
        try:
            if target["use_playwright"]:
                html = await browser.fetch_html(
                    target["url"],
                    chain=target["chain_name"],
                    chain_store_id=target.get("chain_store_id"),
                )
            else:
                html = await _fetch_plain(target["url"], target)
            # parse_maxi needs the URL to detect unit from suffix
            if parser_key == "maxi":
                result = parser_fn(html, url=target["url"])
            else:
                result = parser_fn(html)
        except Exception as e:
            result = None
            if attempt < _MAX_RETRIES:
                delay = _RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)
                continue
            else:
                print(f"[{chain}] {target['product_name']} ... ERREUR: {e}")

        if result is not None:
            unit_info = f" ({result.unit}" + (f", {result.price_per_kg:.2f}$/kg" if result.price_per_kg else "") + ")"
            print(f"[{chain}] {target['product_name']} ... {result.price:.2f} ${unit_info}")
            update_target_status(target["target_id"], success=True,
                                 product_title=result.title or None)
            upsert_price(target["target_id"], today, result.price,
                         price_unit=result.unit, price_per_kg=result.price_per_kg)
            return True

        if attempt < _MAX_RETRIES:
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            await asyncio.sleep(delay)
        else:
            print(f"[{chain}] {target['product_name']} ... prix introuvable")
            update_target_status(target["target_id"], success=False)
            upsert_price(target["target_id"], today, None)
            return False

    return False


async def _worker(chain_name: str, targets: list[dict],
                  browser: PlaywrightBrowser, today: date) -> tuple[int, int]:
    """Process all targets for one chain sequentially with rate limiting."""
    delay = _CHAIN_DELAY.get(chain_name, _DEFAULT_DELAY)
    successes = 0
    failures = 0
    consecutive_fails = 0

    for i, t in enumerate(targets):
        # Circuit breaker: stop after 5 consecutive failures
        if consecutive_fails >= 5:
            remaining = len(targets) - i
            print(f"[{chain_name}] Circuit breaker: {consecutive_fails} consecutive "
                  f"failures, skipping {remaining} remaining targets")
            failures += remaining
            break

        ok = await _scrape_one(t, browser, today)
        if ok:
            successes += 1
            consecutive_fails = 0
        else:
            failures += 1
            consecutive_fails += 1

        # Rate limit between requests (skip after last)
        if i < len(targets) - 1:
            await asyncio.sleep(delay)

    return successes, failures


async def run() -> None:
    config = _load_config()
    sync_targets(config)

    targets = get_active_targets()
    if not targets:
        print("No active scrape targets found.")
        return

    today = date.today()

    # Group targets by chain
    by_chain: dict[str, list[dict]] = defaultdict(list)
    for t in targets:
        by_chain[t["chain_name"]].append(t)

    print(f"Scraping {len(targets)} targets across {len(by_chain)} chains "
          f"for {today}...")

    # Start browser (shared across all workers)
    browser = PlaywrightBrowser()
    needs_browser = any(t["use_playwright"] for t in targets)
    if needs_browser:
        await browser.start()

        # Set up store selection for each chain before scraping
        for chain_name, chain_targets in by_chain.items():
            chain_store_id = chain_targets[0].get("chain_store_id")
            if chain_store_id:
                await browser.setup_chain(chain_name, chain_store_id)

    try:
        # Launch one worker per chain in parallel
        tasks = [
            _worker(chain, chain_targets, browser, today)
            for chain, chain_targets in by_chain.items()
        ]
        results = await asyncio.gather(*tasks)
    finally:
        if needs_browser:
            await browser.close()

    total_success = sum(r[0] for r in results)
    total_fail = sum(r[1] for r in results)

    print(f"\nDone. {len(targets)} targets scraped for {today}. "
          f"Success: {total_success}, Failed: {total_fail}.")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
