#!/usr/bin/env python3
"""Scrape IGA product pages (must run on a residential IP) and push results
to Oracle VM via SSH. Outputs progress to stderr, JSON result to stdout."""

import json
import re
import sys
import time
import urllib.request
from datetime import date

STORE_ID = "8490"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5",
    "Cookie": (
        f"storeId={STORE_ID}_Quebec; selected_store_region=Quebec; "
        "OptanonAlertBoxClosed=2024-01-01T00:00:00.000Z"
    ),
}

# product_slug → IGA URL
TARGETS = [
    ("fraises-454g",     "https://www.iga.ca/fr/produits/fraises-454-g"),
    ("bananes",          "https://www.iga.ca/fr/produits/banane-1-groupe-(5---6)"),
    ("bleuets-frais",    "https://www.iga.ca/fr/produits/bleuets-chopine-1-un"),
    ("oranges",          "https://www.iga.ca/fr/produits/oranges-sac-1%252E36-kg"),
    ("raisins-frais",    "https://www.iga.ca/fr/produits/raisins-rouge-de-qualit%C3%A9-superieur-908-g"),
    ("ananas",           "https://www.iga.ca/fr/produits/ananas-dor%C3%A9-1-un"),
    ("cantaloup",        "https://www.iga.ca/fr/produits/cantaloup-gros-1-un"),
    ("avocats",          "https://www.iga.ca/fr/produits/avocat-hass-de-l-ouest-1-un"),
    ("brocoli",          "https://www.iga.ca/fr/produits/brocoli-1-un"),
    ("poires-fraiches",  "https://www.iga.ca/fr/produits/poire-bartlett-907-g"),
    ("boeuf-hache",      "https://www.iga.ca/fr/produits/mi-maigre-format-familial-boeuf-hach%C3%A9"),
    ("poitrine-poulet",  "https://www.iga.ca/fr/produits/d%C3%A9soss%C3%A9e-par%C3%A9e-format-familial-poitrine-de-poulet"),
    ("beurre-454g",      "https://www.iga.ca/fr/produits/compliments-beurre-sal%C3%A9-454-g"),
    ("pommes",           "https://www.iga.ca/fr/produits/compliments-pommes-mcintosh-1%252E81-kg"),
    ("lait-2pct-2l",     "https://www.iga.ca/fr/produits/qu%C3%A9bon-lait-2---de-mati%C3%A8res-grasses-contenant-plastique-2-l"),
    ("oeufs-gros-12",    "https://www.iga.ca/fr/produits/compliments-oeufs-blancs-gros-12-un"),
    ("pain-blanc-675g",  "https://www.iga.ca/fr/produits/pom-pain-blanc-ultra-moelleux-superclub-pour-sandwich-675-g"),
    ("pates-seches-900g","https://www.iga.ca/fr/produits/compliments-p%C3%A2tes-alimentaires-fusilli-900-g"),
]

# These products should never show $/kg
NO_KG_SLUGS = {
    "ananas", "avocats", "bleuets-frais", "brocoli",
    "cantaloup", "lait-2pct-2l", "oeufs-gros-12",
}

_LB_TO_KG = 2.20462


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def _price_per_kg_from_title(title: str, price: float) -> float | None:
    """Derive $/kg from the weight mentioned in the product title."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*kg\b", title, re.IGNORECASE)
    if m:
        kg = float(m.group(1).replace(",", "."))
        return round(price / kg, 2)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*g\b", title, re.IGNORECASE)
    if m:
        grams = float(m.group(1).replace(",", "."))
        if grams >= 50:
            return round(price / (grams / 1000), 2)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*lb\b", title, re.IGNORECASE)
    if m:
        lbs = float(m.group(1).replace(",", "."))
        return round(price / (lbs / _LB_TO_KG), 2)
    return None


def _parse(html: str, product_slug: str) -> dict | None:
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(m.group(1))
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("offers") is None:
            continue

        offers = data["offers"]
        price_str = (
            offers[0].get("price") if isinstance(offers, list) else offers.get("price")
        )
        try:
            price = float(price_str)
        except (TypeError, ValueError):
            continue

        uom_m = re.search(r'"uom":"([A-Z]+)"', html)
        if not uom_m:
            uom_m = re.search(r'\\"uom\\":\\"([A-Z]+)\\"', html)
        uom = uom_m.group(1) if uom_m else "EA"

        if uom == "KG":
            return {"price": price, "unit": "kg", "price_per_kg": price}

        # Sold by unit — try to compute $/kg from title weight
        title = data.get("name", "")
        price_per_kg = None
        if product_slug not in NO_KG_SLUGS:
            price_per_kg = _price_per_kg_from_title(title, price)

        return {"price": price, "unit": "each", "price_per_kg": price_per_kg}

    return None


def main():
    today = str(date.today())
    results = []

    for product_slug, url in TARGETS:
        try:
            html = _fetch(url)
            parsed = _parse(html, product_slug)
            if parsed:
                parsed["product_slug"] = product_slug
                parsed["store_slug"] = "iga-default"
                parsed["date"] = today
                results.append(parsed)
                kg_str = f", {parsed['price_per_kg']:.2f}$/kg" if parsed.get("price_per_kg") else ""
                print(f"[IGA] {product_slug}: {parsed['price']:.2f} $ ({parsed['unit']}{kg_str})", file=sys.stderr)
            else:
                print(f"[IGA] {product_slug}: prix introuvable", file=sys.stderr)
        except Exception as e:
            print(f"[IGA] {product_slug}: ERREUR {e}", file=sys.stderr)
        time.sleep(1)

    print(json.dumps(results))


if __name__ == "__main__":
    main()
