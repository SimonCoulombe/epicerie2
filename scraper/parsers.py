"""Price extraction parsers — ported from scrape_fraises.R.

Each parser returns a PriceResult: (price, unit, price_per_kg).
- price: the displayed/primary price on the page (float)
- unit: what that price is for — "each", "kg", "lb", "100g", or a weight like "450g"
- price_per_kg: normalized $/kg (float or None if not available)
"""

import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class PriceResult:
    price: float            # Display price (as shown on the page)
    unit: str               # "each", "kg", "lb", "100g", weight like "450g"
    price_per_kg: float | None  # Normalized $/kg for comparison
    title: str = ""         # Exact product name from the store page


# ─── Helpers ──────────────────────────────────────────────────────────

_KG_PRICE_RE = re.compile(r"(\d+)[,.](\d{2})\s*\$?\s*/\s*(?:1\s*)?kg", re.IGNORECASE)
_LB_PRICE_RE = re.compile(r"(\d+)[,.](\d{2})\s*\$?\s*/\s*(?:1\s*)?lb", re.IGNORECASE)
_100G_PRICE_RE = re.compile(r"(\d+)[,.](\d{2})\s*\$?\s*/\s*100\s*(?:g|ml)", re.IGNORECASE)
LB_TO_KG = 2.20462


def _parse_french_price(text: str) -> float | None:
    """Parse '3,99 $' or '$3.99' from text."""
    m = re.search(r"(\d+),(\d{2})\s*\$", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    m = re.search(r"\$(\d+)\.(\d{2})", text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _extract_kg_price(text: str) -> float | None:
    """Extract $/kg from text like '1,74 $ /kg' or '1.74$/kg'."""
    m = _KG_PRICE_RE.search(text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _extract_lb_price(text: str) -> float | None:
    """Extract $/lb from text like '0,79 $ /lb'."""
    m = _LB_PRICE_RE.search(text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _extract_100g_price(text: str) -> float | None:
    """Extract $/100g or $/100ml from text like '0,37 $ /100g'."""
    m = _100G_PRICE_RE.search(text)
    if m:
        return float(f"{m.group(1)}.{m.group(2)}")
    return None


def _lb_to_kg(price_per_lb: float) -> float:
    return round(price_per_lb * LB_TO_KG, 2)


def _detect_unit_from_sale_text(text: str) -> str:
    """Detect unit from sale price text like 'ch.', '/ 450g', '/kg'."""
    text_lower = text.lower()
    if "/kg" in text_lower or "/ kg" in text_lower:
        return "kg"
    if "/lb" in text_lower or "/ lb" in text_lower:
        return "lb"
    m = re.search(r"/\s*(\d+)\s*g\b", text_lower)
    if m:
        return f"{m.group(1)}g"
    if re.search(r"/\s*100\s*g", text_lower):
        return "100g"
    # "ch." or "env.ch." or "chacun" = each
    return "each"


def _parse_jsonld_product(html: str) -> dict | None:
    """Extract the first JSON-LD Product object from <script> blocks."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("offers") is not None:
            return data
    return None


def price_from_jsonld(html: str) -> float | None:
    """Extract price from JSON-LD <script> blocks."""
    data = _parse_jsonld_product(html)
    if data is None:
        return None
    offers = data.get("offers")
    if isinstance(offers, list):
        price_str = offers[0].get("price") if offers else None
    else:
        price_str = offers.get("price")
    if price_str is not None:
        try:
            return float(price_str)
        except (ValueError, TypeError):
            pass
    return None


def _title_from_jsonld(html: str) -> str:
    """Extract product name from JSON-LD."""
    data = _parse_jsonld_product(html)
    if data:
        return data.get("name", "")
    return ""


def _brand_from_jsonld(html: str) -> str:
    """Extract brand name from JSON-LD brand.name."""
    data = _parse_jsonld_product(html)
    if data:
        brand = data.get("brand")
        if isinstance(brand, dict):
            return brand.get("name", "")
        if isinstance(brand, str):
            return brand
    return ""


def _prepend_brand(title: str, brand: str) -> str:
    """Prepend brand to title if not already present."""
    if not brand or not title:
        return title
    if brand.lower() in title.lower():
        return title
    return f"{brand} {title}"


# ─── Loblaw-family parsers (Super C, Metro) ──────────────────────────
# These stores have:
#   - data-main-price attribute with the display price
#   - .pricing__sale-price text telling unit (ch., /450g, etc.)
#   - .pricing__secondary-price with $/kg and $/lb

def _parse_loblaw_html(html: str) -> PriceResult | None:
    """Parse price + unit from Super C / Metro HTML."""
    soup = BeautifulSoup(html, "lxml")

    # Extract title from <h1>, fall back to og:title / JSON-LD
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    # Metro h1 may say "Connectez-vous"; use og:title instead
    if not title or "connectez" in title.lower():
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            # Strip store suffix like " | Metro" or " | Super C"
            title = re.split(r"\s*\|\s*", og["content"])[0].strip()
    if not title or "connectez" in title.lower():
        title = _title_from_jsonld(html)

    # Prepend brand (from .pi--brand element or JSON-LD)
    brand_el = soup.select_one(".pi--brand")
    brand = brand_el.get_text(strip=True) if brand_el else _brand_from_jsonld(html)
    title = _prepend_brand(title, brand)

    # Scope to .product-info to skip carousel/promoted tiles that appear
    # above the product and also carry data-main-price (Metro layout, May 2026+)
    product_info = soup.select_one('.product-info')
    price_div = (product_info or soup).find(attrs={'data-main-price': True})
    if price_div is None:
        # Fallback: try JSON-LD
        price = price_from_jsonld(html)
        if price is not None:
            return PriceResult(price=price, unit="each", price_per_kg=None, title=title)
        return None

    price = float(price_div["data-main-price"])

    # Determine unit from sale price text
    sale_el = price_div.select_one(".pricing__sale-price")
    sale_text = sale_el.get_text(strip=True) if sale_el else ""
    unit = _detect_unit_from_sale_text(sale_text)

    # Extract $/kg from secondary price
    sec_el = price_div.select_one(".pricing__secondary-price")
    sec_text = sec_el.get_text(strip=True) if sec_el else ""

    price_per_kg = _extract_kg_price(sec_text)
    if price_per_kg is None:
        price_per_lb = _extract_lb_price(sec_text)
        if price_per_lb is not None:
            price_per_kg = _lb_to_kg(price_per_lb)
    if price_per_kg is None:
        price_per_100g = _extract_100g_price(sec_text)
        if price_per_100g is not None:
            price_per_kg = round(price_per_100g * 10, 2)

    # If unit is "kg", the display price IS the per-kg price
    if unit == "kg" and price_per_kg is None:
        price_per_kg = price

    return PriceResult(price=price, unit=unit, price_per_kg=price_per_kg, title=title)


def parse_superc(html: str) -> PriceResult | None:
    return _parse_loblaw_html(html)


def parse_metro(html: str) -> PriceResult | None:
    return _parse_loblaw_html(html)


# ─── Maxi parser ─────────────────────────────────────────────────────
# Maxi uses JSON-LD for the display price.
# URL suffix _KG or _EA tells us the unit.
# The page has .comparison-price-list with $/kg and $/lb.

def parse_maxi(html: str, url: str = "") -> PriceResult | None:
    price = price_from_jsonld(html)
    if price is None:
        return None

    title = _title_from_jsonld(html)
    brand = _brand_from_jsonld(html)
    title = _prepend_brand(title, brand)
    soup = BeautifulSoup(html, "lxml")

    # Detect unit — prefer the on-page selling-price text (most reliable),
    # fall back to URL suffix.
    url_hint = "kg" if "_KG" in url.upper() else "each"
    unit = url_hint

    sell_el = soup.select_one(".selling-price-list__item__price--now-price")
    if sell_el:
        sell_text = sell_el.get_text(strip=True)
        unit = _detect_unit_from_sale_text(sell_text)

    # Extract $/kg from the product-details-page comparison price only.
    # Scoping to --product-details-page avoids picking up the bulk/loose
    # comparison prices shown on product-tile cards in the carousel.
    price_per_kg = None
    pdp_sel = (
        ".comparison-price-list--product-details-page "
        ".comparison-price-list__item__price"
    )
    for el in soup.select(pdp_sel):
        text = el.get_text(strip=True)
        kg_p = _extract_kg_price(text)
        if kg_p is not None:
            price_per_kg = kg_p
            break
        lb_p = _extract_lb_price(text)
        if lb_p is not None:
            price_per_kg = _lb_to_kg(lb_p)
            break
        p100g = _extract_100g_price(text)
        if p100g is not None:
            price_per_kg = round(p100g * 10, 2)
            break

    if price_per_kg is None and unit == "kg":
        price_per_kg = price

    return PriceResult(price=price, unit=unit, price_per_kg=price_per_kg, title=title)


# ─── IGA parser ──────────────────────────────────────────────────────
# IGA uses JSON-LD for price. The page's embedded JS has "uom":"KG" or "uom":"EA".
# The JSON-LD price is in the unit indicated by uom.

def parse_iga(html: str) -> PriceResult | None:
    price = price_from_jsonld(html)
    if price is None:
        return None

    title = _title_from_jsonld(html)
    brand = _brand_from_jsonld(html)
    title = _prepend_brand(title, brand)

    # Extract uom from embedded JS data (double-escaped JSON)
    uom_match = re.search(r'\\"uom\\":\\"([A-Z]+)\\"', html)
    if uom_match is None:
        # Try unescaped (Playwright-rendered)
        uom_match = re.search(r'"uom":"([A-Z]+)"', html)

    uom = uom_match.group(1) if uom_match else "EA"

    if uom == "KG":
        return PriceResult(price=price, unit="kg", price_per_kg=price, title=title)
    else:
        return PriceResult(price=price, unit="each", price_per_kg=None, title=title)


PARSERS = {
    "superc": parse_superc,
    "maxi": parse_maxi,
    "metro": parse_metro,
    "iga": parse_iga,
}


# ─── Post-processing: compute $/kg from title weight ─────────────────

_WEIGHT_KG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\b", re.IGNORECASE)
_WEIGHT_G_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*g\b", re.IGNORECASE)
_WEIGHT_LB_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*lb\b", re.IGNORECASE)


def _weight_kg_from_title(title: str) -> float | None:
    """Extract weight in kg from product title. Returns None if not found."""
    m = _WEIGHT_KG_RE.search(title)
    if m:
        return float(m.group(1).replace(",", "."))
    m = _WEIGHT_G_RE.search(title)
    if m:
        grams = float(m.group(1).replace(",", "."))
        if grams >= 50:  # ignore tiny numbers that aren't weights
            return grams / 1000.0
    m = _WEIGHT_LB_RE.search(title)
    if m:
        lbs = float(m.group(1).replace(",", "."))
        return lbs / LB_TO_KG
    return None


def enrich_price_per_kg(result: PriceResult) -> PriceResult:
    """If price_per_kg is missing but title contains weight, compute it."""
    if result.price_per_kg is not None:
        return result
    if result.unit != "each":
        return result
    weight_kg = _weight_kg_from_title(result.title)
    if weight_kg and weight_kg > 0:
        result.price_per_kg = round(result.price / weight_kg, 2)
    return result
