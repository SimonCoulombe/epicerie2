"""Price extraction parsers — ported from scrape_fraises.R."""

import json
import re

from bs4 import BeautifulSoup


def price_from_jsonld(html: str) -> float | None:
    """Extract price from JSON-LD <script> blocks.

    Handles both:
      { "offers": { "price": "4.99" } }
      { "offers": [{ "price": "4.99" }, ...] }
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue

        offers = data.get("offers")
        if offers is None:
            continue

        # offers can be a dict or a list of dicts
        if isinstance(offers, list):
            price_str = offers[0].get("price") if offers else None
        else:
            price_str = offers.get("price")

        if price_str is not None:
            try:
                return float(price_str)
            except (ValueError, TypeError):
                continue
    return None


def price_from_css(html: str, selector: str = ".pi--prices") -> float | None:
    """Extract price from a CSS selector. Handles French '3,99 $' and English '$3.99'."""
    soup = BeautifulSoup(html, "lxml")
    node = soup.select_one(selector)
    if node is None:
        return None

    txt = node.get_text()

    # French: 3,99 $
    m = re.search(r"(\d+),(\d{2})\s*\$", txt)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")

    # English: $3.99
    m = re.search(r"\$(\d+)\.(\d{2})", txt)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")

    return None


def parse_superc(html: str) -> float | None:
    price = price_from_jsonld(html)
    if price is None:
        price = price_from_css(html, ".pi--prices")
    return price


def parse_maxi(html: str) -> float | None:
    return price_from_jsonld(html)


def parse_metro(html: str) -> float | None:
    price = price_from_jsonld(html)
    if price is None:
        price = price_from_css(html, ".pi--prices")
    return price


def parse_iga(html: str) -> float | None:
    return price_from_jsonld(html)


PARSERS = {
    "superc": parse_superc,
    "maxi": parse_maxi,
    "metro": parse_metro,
    "iga": parse_iga,
}
