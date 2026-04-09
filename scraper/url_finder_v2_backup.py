"""URL finder — discovers product page URLs for all 4 store chains.

Three-phase approach for each product:
  1. SEARCH: Search all 4 stores, collect top ~8 product URLs each
  2. EXTRACT: Visit each candidate page, extract brand + title from JSON-LD
  3. MATCH: Send all candidates to an LLM which picks the best comparable
     set across stores (same quality, same brand/model when possible)

Usage:
    python -m scraper.url_finder "Bœuf haché mi-maigre"
    python -m scraper.url_finder --batch --limit 5
    python -m scraper.url_finder --batch --no-llm   # first-match fallback

It's OK for this to be slow — it should only run once per product.
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import quote

import httpx
from playwright.async_api import async_playwright

from scraper.parsers import _parse_jsonld_product

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

_STRIP_PARAMS = re.compile(r"\?.*$")

# ── OpenRouter config ─────────────────────────────────────────────────
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_LLM_MODEL = "google/gemini-2.5-flash"

_MAX_SEARCH_RESULTS = 8   # URLs to collect per store from search
_MAX_DETAIL_PAGES = 5     # pages to visit per store for brand/title extraction


# ── Helpers ───────────────────────────────────────────────────────────

def _load_openrouter_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key:
        return key
    env_path = Path.home() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _clean_url(url: str) -> str:
    return _STRIP_PARAMS.sub("", url)


def _simplify_search_term(name: str) -> str:
    """Strip accents and quantity suffixes for more robust store search.

    Super C in particular can't handle accented characters in its search."""
    # Handle ligatures that NFKD doesn't decompose
    name = name.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae").replace("Æ", "AE")
    # Strip accents: é→e, è→e, etc.
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remove trailing quantity like "454g", "2L", "900g", "675g"
    ascii_name = re.sub(r"\s+\d+\s*(g|kg|l|lb|ml|u)\b", "", ascii_name, flags=re.IGNORECASE)
    # Remove percent signs (e.g. "2%")
    ascii_name = ascii_name.replace("%", "")
    return ascii_name.strip()


def _load_existing_targets() -> set[tuple[str, str]]:
    existing = set()
    if TARGETS_CSV.exists():
        with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((row["product_slug"], row["store_slug"]))
    return existing


def _load_products() -> list[dict]:
    with open(PRODUCTS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _products_missing_targets(existing: set[tuple[str, str]]) -> list[dict]:
    products = _load_products()
    missing = []
    for p in products:
        store_slugs_with_target = {s for (ps, s) in existing if ps == p["slug"]}
        all_store_slugs = {info["slug"] for info in STORES.values()}
        if not all_store_slugs.issubset(store_slugs_with_target):
            missing.append(p)
    return missing


# ── Browser helpers ───────────────────────────────────────────────────

async def _new_stealth_page(browser):
    """Create a browser context + page with stealth settings and pre-set
    cookie consent to avoid popup interactions that break search pages."""
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="fr-CA",
        viewport={"width": 1920, "height": 1080},
    )
    # Pre-set OneTrust cookie consent for all store domains so the banner
    # never appears (clicking it at runtime breaks Super C search results).
    consent_cookies = []
    for domain in [".superc.ca", ".maxi.ca", ".metro.ca", ".iga.ca"]:
        consent_cookies.append({
            "name": "OptanonAlertBoxClosed",
            "value": "2024-01-01T00:00:00.000Z",
            "domain": domain,
            "path": "/",
        })
    await context.add_cookies(consent_cookies)

    page = await context.new_page()
    await page.add_init_script(
        'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
    )
    # Auto-dismiss any JS dialog (alert/confirm/prompt)
    page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
    return context, page


async def _dismiss_popups(page):
    """Try to close common overlay popups (store pickers, cookie banners).
    Uses a single combined selector for speed."""
    combined = ", ".join([
        'button[aria-label="Fermer"]',
        'button[aria-label="Close"]',
        ".modal__close-btn",
        '[data-testid="close-button"]',
    ])
    try:
        for btn in await page.locator(combined).all():
            try:
                await btn.click()
                await page.wait_for_timeout(500)
            except Exception:
                pass
    except Exception:
        pass


# ── Phase 1: Search ──────────────────────────────────────────────────

async def search_product(browser, store_key: str, search_term: str) -> list[str]:
    """Search a store, return up to _MAX_SEARCH_RESULTS product URLs."""
    info = STORES[store_key]
    clean_term = _simplify_search_term(search_term)
    search_url = info["search_url"].format(query=quote(clean_term))
    pattern = info["product_pattern"]

    context, page = await _new_stealth_page(browser)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        # Dismiss cookie/store popups early — some sites won't render products
        # until the cookie consent is accepted.
        await _dismiss_popups(page)
        await page.wait_for_timeout(3000)
        # Scroll progressively to trigger lazy-loaded product tiles
        for pct in (0.3, 0.6):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
            await page.wait_for_timeout(2500)
        # Try dismissing again (some popups appear after scroll)
        await _dismiss_popups(page)
        await page.wait_for_timeout(2000)

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
                if len(matches) >= _MAX_SEARCH_RESULTS:
                    break
        return matches
    except Exception as e:
        print(f"  [{store_key}] search ERROR: {e}", file=sys.stderr)
        return []
    finally:
        await page.close()
        await context.close()


# ── Phase 2: Extract product info from individual pages ──────────────

async def _extract_product_info(browser, url: str) -> dict:
    """Visit a product page, return {url, title, brand, size}."""
    context, page = await _new_stealth_page(browser)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        await page.wait_for_timeout(3000)
        await _dismiss_popups(page)
        html = await page.content()

        data = _parse_jsonld_product(html)
        title = ""
        brand = ""
        size = ""
        if data:
            title = data.get("name", "")
            brand_obj = data.get("brand")
            if isinstance(brand_obj, dict):
                brand = brand_obj.get("name", "")
            elif isinstance(brand_obj, str):
                brand = brand_obj
            # Try to get size/weight from JSON-LD
            size = data.get("weight", "") or data.get("size", "")

        # Fallback: og:title for Metro/SuperC
        if not title:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = re.split(r"\s*\|\s*", og["content"])[0].strip()
            if not brand:
                brand_el = soup.select_one(".pi--brand")
                if brand_el:
                    brand = brand_el.get_text(strip=True)

        return {"url": url, "title": title, "brand": brand, "size": size}
    except Exception as e:
        return {"url": url, "title": "", "brand": "", "size": "", "error": str(e)}
    finally:
        await page.close()
        await context.close()


async def _get_candidates_with_info(
    browser, store_key: str, urls: list[str]
) -> list[dict]:
    """Visit top N candidate pages for one store, return enriched dicts."""
    candidates = []
    for url in urls[:_MAX_DETAIL_PAGES]:
        info = await _extract_product_info(browser, url)
        info["store"] = store_key
        candidates.append(info)
        await asyncio.sleep(1.5)  # Rate limit
    return candidates


# ── Phase 3: LLM matching ────────────────────────────────────────────

async def _llm_pick_best(
    product_name: str,
    product_unit: str,
    all_candidates: dict[str, list[dict]],
) -> dict[str, str] | None:
    """Ask LLM to pick the best comparable URL per store.

    Returns dict {store_key: url} or None on failure.
    """
    api_key = _load_openrouter_key()
    if not api_key:
        print("  [LLM] No OPENROUTER_API_KEY, skipping", file=sys.stderr)
        return None

    # Build candidate text
    lines = []
    for store_key, candidates in all_candidates.items():
        lines.append(f"\n## {store_key.upper()}")
        for i, c in enumerate(candidates, 1):
            parts = []
            if c.get("brand"):
                parts.append(f"Marque: {c['brand']}")
            parts.append(f"Nom: {c['title']}")
            if c.get("size"):
                parts.append(f"Format: {c['size']}")
            lines.append(f"  {i}. {' | '.join(parts)}")
            lines.append(f"     URL: {c['url']}")
    candidate_text = "\n".join(lines)

    prompt = f"""Tu es un assistant expert en comparaison de prix d'épicerie au Québec.

PRODUIT RECHERCHÉ: **{product_name}** (format cible: {product_unit})

Voici les résultats de recherche venant de 4 bannières d'épicerie québécoises (Super C, Maxi, Metro, IGA). Pour chaque bannière, choisis LE produit qui est le plus comparable aux choix des autres bannières.

RÈGLES DE SÉLECTION (par ordre de priorité):

1. **Même type de produit**: Le produit DOIT être du même type que ce qui est recherché.
   Exemple: pour "bœuf haché mi-maigre", ne prends PAS du bœuf haché maigre ou extra-maigre.

2. **Même qualité/grade**: "mi-maigre" ≠ "maigre" ≠ "extra-maigre". "Blé entier" ≠ "blanc".
   Les niveaux de qualité différents donnent des prix différents et ne sont pas comparables.

3. **Même marque si possible**: Si une marque premium (ex: POM, Natrel, Lactantia, Villaggio, St-Méthode, Catelli, Barilla) est disponible dans les 4 magasins, choisis-la partout.
   - Si une marque premium n'est pas disponible partout, bascule vers une marque maison PARTOUT.

4. **Marques maison interchangeables**: Les marques suivantes sont des marques maison ("store brands") équivalentes entre elles:
   - "Sélection" / "Selection" (Metro, Super C)
   - "Sans Nom" / "No Name" (Maxi)
   - "Nos Compliments" / "Compliments" (IGA)
   - "Le Choix du Président" / "President's Choice" (Maxi, Loblaw)
   Il est acceptable de comparer "Selection" vs "Sans Nom" vs "Compliments" car ce sont toutes des marques maison.

5. **Même modèle/variante pour marque premium**: Si on choisit POM, il faut le MÊME type de pain POM dans les 4 magasins (ex: tous "blanc ultramoelleux", pas "raisin" dans un et "blé" dans un autre).

6. **Format/poids le plus proche**: Préfère le format qui correspond à "{product_unit}".

7. Si aucun produit ne correspond dans un magasin, indique "NONE".

{candidate_text}

Réponds avec un bloc JSON valide (sans markdown ```, juste le JSON brut) ayant cette structure exacte:
{{"superc": "URL_ou_NONE", "maxi": "URL_ou_NONE", "metro": "URL_ou_NONE", "iga": "URL_ou_NONE", "reasoning": "explication courte en français"}}"""

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                _OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            body = resp.json()

        content = body["choices"][0]["message"]["content"]
        # Extract JSON from response
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if not json_match:
            print(f"  [LLM] Could not parse JSON from response:\n{content[:300]}",
                  file=sys.stderr)
            return None

        result = json.loads(json_match.group())
        reasoning = result.pop("reasoning", "")
        if reasoning:
            print(f"  [LLM] Raisonnement: {reasoning}", file=sys.stderr)

        # Validate: every URL must be in our candidate list
        valid = {}
        for store_key in STORES:
            url = result.get(store_key, "NONE")
            if url == "NONE" or not url:
                continue
            store_urls = {c["url"] for c in all_candidates.get(store_key, [])}
            if url in store_urls:
                valid[store_key] = url
            else:
                # Try fuzzy match (LLM may reformat URL slightly)
                for candidate_url in store_urls:
                    if candidate_url.rstrip("/") == url.rstrip("/"):
                        valid[store_key] = candidate_url
                        break
                else:
                    print(f"  [LLM] WARNING: {store_key} URL not in candidates: {url}",
                          file=sys.stderr)
        return valid if valid else None

    except Exception as e:
        print(f"  [LLM] Error: {e}", file=sys.stderr)
        return None


# ── Orchestration ─────────────────────────────────────────────────────

async def find_single(search_term: str, use_llm: bool = True) -> None:
    """Search all 4 stores for one product and print CSV rows."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        print(f"# Searching for: {search_term}", file=sys.stderr)

        # Phase 1: Search
        all_urls: dict[str, list[str]] = {}
        for store_key in STORES:
            matches = await search_product(browser, store_key, search_term)
            all_urls[store_key] = matches
            status = f"{len(matches)} results" if matches else "NOT FOUND"
            print(f"  [{store_key}] {status}", file=sys.stderr)
            await asyncio.sleep(2)

        if use_llm and _load_openrouter_key():
            # Phase 2: Extract info
            print("  Extracting product info from pages...", file=sys.stderr)
            all_candidates: dict[str, list[dict]] = {}
            for store_key, urls in all_urls.items():
                if urls:
                    candidates = await _get_candidates_with_info(
                        browser, store_key, urls
                    )
                    all_candidates[store_key] = candidates
                    for c in candidates:
                        brand = f" [{c['brand']}]" if c.get("brand") else ""
                        print(f"    [{store_key}] {c['title']}{brand}",
                              file=sys.stderr)

            # Phase 3: LLM
            print("  Asking LLM to match products...", file=sys.stderr)
            picks = await _llm_pick_best(search_term, "", all_candidates)

            if picks:
                print("product_slug,store_slug,url,use_playwright,parser")
                for store_key, url in picks.items():
                    info = STORES[store_key]
                    print(f"SLUG,{info['slug']},{url},{info['use_playwright']},"
                          f"{store_key}")
                await browser.close()
                return

        # Fallback: first match per store
        print("product_slug,store_slug,url,use_playwright,parser")
        for store_key, info in STORES.items():
            urls = all_urls.get(store_key, [])
            if urls:
                print(f"SLUG,{info['slug']},{urls[0]},{info['use_playwright']},"
                      f"{store_key}")
            else:
                print(f"# [{store_key}] NOT FOUND", file=sys.stderr)
        await browser.close()


async def find_batch(limit: int | None = None, use_llm: bool = True) -> None:
    """Find URLs for all products missing targets. Append to targets.csv."""
    existing = _load_existing_targets()
    missing = _products_missing_targets(existing)

    if not missing:
        print("All products already have targets.", file=sys.stderr)
        return

    if limit:
        missing = missing[:limit]

    has_llm = use_llm and bool(_load_openrouter_key())
    mode = "LLM-assisted" if has_llm else "first-match"
    print(f"Finding URLs for {len(missing)} products ({mode})...", file=sys.stderr)

    new_rows: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for i, product in enumerate(missing):
            slug = product["slug"]
            name = product["name"]
            unit = product.get("unit", "")
            print(f"\n[{i+1}/{len(missing)}] {slug} — '{name}'", file=sys.stderr)

            # Which stores still need targets?
            needed = {
                k: v for k, v in STORES.items()
                if (slug, v["slug"]) not in existing
            }

            # Phase 1: Search all needed stores
            all_urls: dict[str, list[str]] = {}
            for store_key in needed:
                matches = await search_product(browser, store_key, name)
                all_urls[store_key] = matches
                status = f"{len(matches)} results" if matches else "NOT FOUND"
                print(f"  [{store_key}] {status}", file=sys.stderr)
                await asyncio.sleep(2)

            picks = None
            if has_llm and any(all_urls.values()):
                # Phase 2: Extract info
                all_candidates: dict[str, list[dict]] = {}
                for store_key, urls in all_urls.items():
                    if urls:
                        candidates = await _get_candidates_with_info(
                            browser, store_key, urls
                        )
                        all_candidates[store_key] = candidates
                        for c in candidates:
                            brand = f" [{c['brand']}]" if c.get("brand") else ""
                            print(f"    [{store_key}] {c['title']}{brand}",
                                  file=sys.stderr)

                # Phase 3: LLM
                print(f"  LLM matching...", file=sys.stderr)
                picks = await _llm_pick_best(name, unit, all_candidates)

            # Record results
            for store_key in needed:
                info = STORES[store_key]
                url = None
                if picks and store_key in picks:
                    url = picks[store_key]
                elif all_urls.get(store_key):
                    url = all_urls[store_key][0]

                if url:
                    new_rows.append({
                        "product_slug": slug,
                        "store_slug": info["slug"],
                        "url": url,
                        "use_playwright": info["use_playwright"],
                        "parser": store_key,
                    })
                    src = "LLM" if (picks and store_key in picks) else "1st"
                    print(f"  [{store_key}] ✓ ({src}) {url}", file=sys.stderr)

        await browser.close()

    if not new_rows:
        print("\nNo new URLs found.", file=sys.stderr)
        return

    file_exists = TARGETS_CSV.exists()
    with open(TARGETS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["product_slug", "store_slug", "url",
                         "use_playwright", "parser"],
        )
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    print(f"\nAppended {len(new_rows)} rows to {TARGETS_CSV}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Find product URLs on grocery store sites"
    )
    parser.add_argument("search_term", nargs="?",
                        help="Product name to search for")
    parser.add_argument("--batch", action="store_true",
                        help="Find URLs for all products missing targets")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max products in batch mode")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM, just pick first search result")
    args = parser.parse_args()

    if args.batch:
        asyncio.run(find_batch(args.limit, use_llm=not args.no_llm))
    elif args.search_term:
        asyncio.run(find_single(args.search_term, use_llm=not args.no_llm))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
