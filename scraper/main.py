"""Scraper CLI entry point — reads config, scrapes prices, stores in DuckDB."""

import asyncio
import sys
from datetime import date
from pathlib import Path

import httpx
import yaml

from scraper.browser import PlaywrightBrowser
from scraper.db import sync_targets, get_active_targets, upsert_price
from scraper.parsers import PARSERS

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "targets.yaml"

# Map store slugs to parser keys
_STORE_PARSER_MAP: dict[str, str] = {}


def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_parser_map(config: dict) -> None:
    """Build mapping from store slug to parser name from config targets."""
    for t in config.get("targets", []):
        _STORE_PARSER_MAP[t["store"]] = t["parser"]


async def _fetch_plain(url: str) -> str:
    """Fetch HTML directly via httpx (no JS rendering)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5",
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text


async def run() -> None:
    config = _load_config()
    _build_parser_map(config)

    # Sync YAML targets to DuckDB
    sync_targets(config)

    targets = get_active_targets()
    if not targets:
        print("No active scrape targets found.")
        return

    today = date.today()
    browser = PlaywrightBrowser()
    needs_browser = any(t["use_playwright"] for t in targets)

    if needs_browser:
        await browser.start()

    try:
        for t in targets:
            chain = t["chain_name"]
            parser_key = _STORE_PARSER_MAP.get(t["store_slug"], "")
            parser_fn = PARSERS.get(parser_key)

            if parser_fn is None:
                print(f"[SKIP] {chain}: no parser for '{parser_key}'")
                continue

            print(f"[{chain}] Scraping {t['product_name']} ... ", end="", flush=True)

            try:
                if t["use_playwright"]:
                    html = await browser.fetch_html(t["url"])
                else:
                    html = await _fetch_plain(t["url"])

                price = parser_fn(html)
            except Exception as e:
                print(f"ERREUR: {e}")
                price = None

            if price is not None:
                print(f"{price:.2f} $")
            else:
                print("prix introuvable")

            upsert_price(t["target_id"], today, price)
    finally:
        if needs_browser:
            await browser.close()

    print(f"\nDone. {len(targets)} targets scraped for {today}.")


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
