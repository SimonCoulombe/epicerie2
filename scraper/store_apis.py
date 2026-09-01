"""Direct product-price clients for retailer endpoints that bypass bot-blocked pages."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

import httpx

from scraper.parsers import PriceResult


_USER_AGENT = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


async def fetch_metro_family_fragment(chain: str, url: str, store_id: str) -> str:
    """Fetch a Metro/Super C product tile through the site's server endpoint."""
    host = "api.metro.ca" if chain == "Metro" else "api1.superc.ca"
    product_id = url.rsplit("/p/", 1)[-1]
    base = f"https://{host}"
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "fr",
        "X-Requested-With": "XMLHttpRequest",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        store_response = await client.post(
            f"{base}/stores/my-store/{store_id}",
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            data={"userConfirmation": "true", "lang": "fr"},
        )
        store_response.raise_for_status()
        response = await client.post(
            f"{base}/epicerie-en-ligne/produit/skus",
            headers={**headers, "Content-Type": "application/json"},
            json={"productIds": [product_id]},
        )
        response.raise_for_status()
        if "data-main-price" not in response.text and chain == "Metro":
            fallback = await client.get(f"{base}{urlparse(url).path}", headers=headers)
            fallback.raise_for_status()
            return fallback.text
        return response.text


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.lower()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _normalise(value)))


def _product_code_from_url(url: str) -> str:
    return urlparse(url).path.rsplit("/p/", 1)[-1]


def _candidate_score(product: dict, target_name: str) -> float:
    target_tokens = _tokens(target_name)
    candidate_tokens = _tokens(product.get("name", ""))
    overlap = sum(1 for token in target_tokens if token in candidate_tokens)
    prefix_overlap = sum(
        0.5 for token in target_tokens
        if len(token) >= 5 and any(candidate.startswith(token[:5]) for candidate in candidate_tokens)
    )
    score = overlap + prefix_overlap
    target_weight = re.search(r"(\d+)\s*(g|kg|lb|ml|l)\b", _normalise(target_name))
    package = _normalise(product.get("packageSize") or "")
    if target_weight and target_weight.group(1) in package and target_weight.group(2) in package:
        score += 2
    modifiers = {"organic", "biologique", "biologiques", "imparfait", "imparfaite", "aromatise", "jus", "congele", "congelee"}
    unexpected = modifiers & candidate_tokens - target_tokens
    score -= 5 * len(unexpected)
    if product.get("stockStatus") == "OK":
        score += 0.25
    return score


def _price_result(product: dict, unit_hint: str = "") -> PriceResult | None:
    prices = product.get("prices") or {}
    price = prices.get("price") or {}
    value = price.get("value")
    if value is None:
        return None

    unit = unit_hint or (price.get("unit") or "ea").lower()
    pricing_units = product.get("pricingUnits") or {}
    if pricing_units.get("unit", "").lower() in {"kg", "lb"}:
        unit = pricing_units["unit"].lower()
    price_per_kg = float(value) if unit == "kg" else None
    if price_per_kg is None:
        for comparison in prices.get("comparisonPrices") or []:
            comparison_unit = (comparison.get("unit") or "").lower()
            quantity = comparison.get("quantity")
            comparison_value = comparison.get("value")
            if comparison_value is None:
                continue
            if comparison_unit == "g" and quantity == 100:
                price_per_kg = round(float(comparison_value) * 10, 2)
                break

    return PriceResult(
        price=float(value),
        unit="kg" if unit == "kg" else "each",
        price_per_kg=price_per_kg,
        title=product.get("name") or "",
    )


async def fetch_maxi_product(target_name: str, url: str, store_id: str) -> PriceResult | None:
    """Find a current Maxi product and price via the PC Express search API."""
    base = "https://api.pcexpress.ca/pcx-bff/api/v1/products/search"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr",
        "Business-User-Agent": "PCXWEB",
        "Content-Type": "application/json",
        "Origin": "https://www.maxi.ca",
        "Referer": "https://www.maxi.ca/",
        "Site-Banner": "maxi",
        "x-apikey": "C1xujSegT5j3ap3yexJjqhOfELwGKYvz",
        "x-application-type": "Web",
        "x-loblaw-tenant-id": "ONLINE_GROCERIES",
        "baseSiteId": "maxi",
        "is-helios-account": "true",
    }
    terms = [_product_code_from_url(url).rsplit("_", 1)[0], target_name]
    async with httpx.AsyncClient(timeout=30) as client:
        for term in terms:
            response = await client.post(
                base,
                headers=headers,
                json={
                    "lang": "fr",
                    "term": term,
                    "storeId": str(store_id),
                    "banner": "maxi",
                    "pagination": {"from": 0, "size": 48},
                },
            )
            response.raise_for_status()
            products = response.json().get("results") or []
            if not products:
                continue
            wanted_code = _product_code_from_url(url)
            exact = next((p for p in products if p.get("code") == wanted_code), None)
            if exact:
                return _price_result(exact, unit_hint="kg" if url.upper().endswith("_KG") else "")
            ranked = sorted(
                products,
                key=lambda product: _candidate_score(product, target_name) + (
                    1 if url.upper().endswith("_KG") and product.get("code", "").upper().endswith("_KG") else 0
                ),
                reverse=True,
            )
            for product in ranked:
                result = _price_result(product, unit_hint="kg" if url.upper().endswith("_KG") else "")
                if result and _candidate_score(product, target_name) > 0:
                    return result
    return None
