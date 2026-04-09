"""URL finder — discovers product page URLs for all 4 store chains.

Four-step approach for each product:
  1. SEARCH: Search all 4 stores in parallel, collect top ~8 product URLs each
  2. EXTRACT: Visit top 3 candidate pages per store, extract brand + title
  3. REFINE: LLM analyzes results, suggests better search keywords, identifies
     which stores already have a match vs which need re-searching
  4. PASS 2: Re-search missing stores with refined query (visit up to 8 pages),
     then LLM picks the best comparable set across all stores

Usage:
    python -m scraper.url_finder "Bœuf haché mi-maigre"
    python -m scraper.url_finder --batch --limit 5
    python -m scraper.url_finder --batch --no-llm   # first-match fallback

It's OK for this to be slow — it should only run once per product.
"""

import argparse
import asyncio
import csv
import hashlib
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
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"

_USE_CACHE = True  # toggled off by --no-cache

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
_MAX_DETAIL_PAGES = 5     # pages to visit per store for pass 1
_MAX_DETAIL_PAGES_PASS2 = 8  # pages to visit per store for pass 2 (refined)

SEARCH_HINTS_JSON = Path(__file__).resolve().parent.parent / "config" / "search_hints.json"

# Words to strip from search terms — these add noise and reduce search hits.
_STRIP_WORDS = re.compile(
    r"\b(frais|fraiches?|secs?|sechee?s?|surgele[es]?|entiers?|blancs?|bruns?)\b",
    re.IGNORECASE,
)


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



# ── Cache helpers ─────────────────────────────────────────────────────

def _search_cache_path(store_key: str, term: str) -> Path:
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', _simplify_search_term(term).lower())
    return CACHE_DIR / "search" / store_key / f"{safe}.json"


def _page_cache_path(url: str) -> Path:
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    return CACHE_DIR / "pages" / f"{h}.html"


def _read_search_cache(store_key: str, term: str) -> list[str] | None:
    if not _USE_CACHE:
        return None
    p = _search_cache_path(store_key, term)
    if p.exists():
        return json.loads(p.read_text())
    return None


def _write_search_cache(store_key: str, term: str, urls: list[str]) -> None:
    if not _USE_CACHE:
        return
    p = _search_cache_path(store_key, term)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(urls))


def _read_page_cache(url: str) -> str | None:
    if not _USE_CACHE:
        return None
    p = _page_cache_path(url)
    if p.exists():
        return p.read_text()
    return None


def _write_page_cache(url: str, html: str) -> None:
    if not _USE_CACHE:
        return
    p = _page_cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html)


def _extract_from_html(url: str, html: str) -> dict:
    """Parse product info from cached HTML without a browser."""
    from scraper.parsers import _parse_jsonld_product as _parse
    data = _parse(html)
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
        size = data.get("weight", "") or data.get("size", "")
    if not title:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        # Fallback 1: Maxi .product-name__item or h1
        pn = soup.select_one(".product-name__item")
        if pn:
            title = pn.get_text(strip=True)
        if not title:
            h1 = soup.find("h1")
            if h1:
                txt = h1.get_text(strip=True)
                if len(txt) < 120 and "\u00e9picerie" not in txt.lower():
                    title = txt
        # Fallback 2: og:title for Metro/SuperC
        if not title:
            og = soup.find("meta", property="og:title")
            if og and og.get("content"):
                title = re.split(r"\s*\|\s*", og["content"])[0].strip()
        if not brand:
            brand_el = soup.select_one(".pi--brand")
            if brand_el:
                brand = brand_el.get_text(strip=True)
    # Detect junk titles (Metro login page, generic headings)
    _JUNK = {"connectez-vous", "sign in", "connexion", "épicerie en ligne",
             "online grocery", ""}
    if title.lower().strip() in _JUNK:
        title = _title_from_url(url)
    # Last resort: extract product name from URL path
    if not title:
        title = _title_from_url(url)
    return {"url": url, "title": title, "brand": brand, "size": size}


def _title_from_url(url: str) -> str:
    """Extract a human-readable title from the URL path as last resort.
    e.g. https://www.maxi.ca/fr/fraises-1-lb/p/123 -> 'fraises 1 lb'"""
    from urllib.parse import urlparse, unquote
    path = unquote(urlparse(url).path)
    match = re.search(r"/([^/]+)/p/", path)
    if match:
        slug = match.group(1)
        return slug.replace("-", " ").strip()
    return ""


def _simplify_search_term(name: str) -> str:
    """Strip accents, quantities, and filler words for more robust store search."""
    # Handle ligatures that NFKD doesn't decompose
    name = name.replace("œ", "oe").replace("Œ", "OE").replace("æ", "ae").replace("Æ", "AE")
    # Strip accents: é→e, è→e, etc.
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Remove trailing quantity like "454g", "2L", "900g", "675g"
    ascii_name = re.sub(r"\s+\d+\s*(g|kg|l|lb|ml|u)\b", "", ascii_name, flags=re.IGNORECASE)
    # Remove percent signs (e.g. "2%")
    ascii_name = ascii_name.replace("%", "")
    # Strip filler adjectives that hurt search recall
    ascii_name = _STRIP_WORDS.sub("", ascii_name)
    # Collapse whitespace
    return re.sub(r"\s+", " ", ascii_name).strip()


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
    page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
    return context, page


async def _dismiss_popups(page):
    """Try to close common overlay popups (store pickers)."""
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
                await page.wait_for_timeout(300)
            except Exception:
                pass
    except Exception:
        pass


# ── Phase 1: Search ──────────────────────────────────────────────────

async def search_product(browser, store_key: str, search_term: str) -> list[str]:
    """Search a store, return up to _MAX_SEARCH_RESULTS product URLs."""
    cached = _read_search_cache(store_key, search_term)
    if cached is not None:
        return cached

    info = STORES[store_key]
    clean_term = _simplify_search_term(search_term)
    search_url = info["search_url"].format(query=quote(clean_term))
    pattern = info["product_pattern"]

    context, page = await _new_stealth_page(browser)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await _dismiss_popups(page)
        await page.wait_for_timeout(1500)
        # Scroll to trigger lazy-loaded product tiles
        for pct in (0.3, 0.6):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pct})")
            await page.wait_for_timeout(1000)
        await _dismiss_popups(page)
        await page.wait_for_timeout(500)

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
        _write_search_cache(store_key, search_term, matches)
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
    cached_html = _read_page_cache(url)
    if cached_html is not None:
        return _extract_from_html(url, cached_html)

    context, page = await _new_stealth_page(browser)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Wait for product content — Maxi SPA needs ~12s
        _CONTENT_SELECTORS = ", ".join([
            'script[type="application/ld+json"]',
            ".product-name__item",
            "h1",
            ".pi--prices",
            "[data-testid='product-price']",
        ])
        try:
            await page.wait_for_selector(_CONTENT_SELECTORS, timeout=15000)
        except Exception:
            pass  # Proceed with whatever rendered
        await _dismiss_popups(page)
        html = await page.content()
        _write_page_cache(url, html)
        return _extract_from_html(url, html)
    except Exception as e:
        return {"url": url, "title": "", "brand": "", "size": "", "error": str(e)}
    finally:
        await page.close()
        await context.close()
async def _get_candidates_with_info(
    browser, store_key: str, urls: list[str], max_pages: int | None = None
) -> list[dict]:
    """Visit top N candidate pages for one store, return enriched dicts."""
    candidates = []
    limit = max_pages or _MAX_DETAIL_PAGES
    for j, url in enumerate(urls[:limit]):
        print(f"    [{store_key}] visiting {j+1}/{min(len(urls), limit)}...", file=sys.stderr)
        info = await _extract_product_info(browser, url)
        info["store"] = store_key
        candidates.append(info)
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

1. **Même type de produit**: Le produit doit être du même type que ce qui est recherché.
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

7. **IMPORTANT: Préfère un produit imparfait à NONE.** N'utilise NONE que si le magasin n'a AUCUN produit du même type (ex: pas de pain du tout). Un pain "tranché épais" reste un pain blanc tranché. Un "D'Italiano" reste du pain blanc même si les autres ont du POM. Choisis toujours le produit le plus proche disponible.

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
        json_match = re.search(r"\{[^{}]*\}", content, re.DOTALL)
        if not json_match:
            print(f"  [LLM] Could not parse JSON from response:\n{content[:300]}",
                  file=sys.stderr)
            return None

        result = json.loads(json_match.group())
        # Normalize keys to lowercase
        result = {k.lower(): v for k, v in result.items()}
        reasoning = result.pop("reasoning", "")
        if reasoning:
            print(f"  [LLM] {reasoning}", file=sys.stderr)

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
                for candidate_url in store_urls:
                    if candidate_url.rstrip("/") == url.rstrip("/"):
                        valid[store_key] = candidate_url
                        break
                else:
                    print(f"  [LLM] WARNING: {store_key} URL not in candidates: {url}",
                          file=sys.stderr)
        # Fallback: if LLM said NONE for a store but we have candidates, pick first
        for store_key in STORES:
            if store_key not in valid and store_key in all_candidates:
                candidates = all_candidates[store_key]
                if candidates:
                    valid[store_key] = candidates[0]["url"]
                    print(f"  [LLM] {store_key}: LLM said NONE, falling back to first result",
                          file=sys.stderr)

        return valid if valid else None

    except Exception as e:
        print(f"  [LLM] Error: {e}", file=sys.stderr)
        return None


# ── LLM Step 1: Refine search query ───────────────────────────────────────

async def _llm_refine_query(
    product_name: str,
    product_unit: str,
    all_candidates: dict[str, list[dict]],
) -> dict | None:
    """Ask LLM to analyze pass 1 results and suggest refined search.

    Returns {
        "refined_query": str,
        "found": {store: url, ...},
        "missing": [store, ...],
    } or None on failure.
    """
    api_key = _load_openrouter_key()
    if not api_key:
        return None

    lines = []
    for store_key, candidates in all_candidates.items():
        lines.append(f"\n## {store_key.upper()}")
        for i, cd in enumerate(candidates, 1):
            parts = []
            if cd.get("brand"):
                parts.append(f"Marque: {cd['brand']}")
            parts.append(f"Nom: {cd['title']}")
            if cd.get("size"):
                parts.append(f"Format: {cd['size']}")
            lines.append(f"  {i}. {' | '.join(parts)}")
            lines.append(f"     URL: {cd['url']}")
    candidate_text = "\n".join(lines)

    prompt = f"""Tu es un expert en recherche de produits d’épicerie au Québec.

PRODUIT RECHERCHÉ: **{product_name}** (format cible: {product_unit})

Voici les premiers résultats de recherche de chaque magasin pour le terme "{product_name}".

{candidate_text}

ANALYSE DEMANDÉE:

1. **Terme de recherche optimal**: Quel terme spécifique donnerait de meilleurs résultats?
   - Si le produit est générique (ex: "pâtes sèches"), suggère un terme plus précis (ex: "spaghetti").
   - Si le terme est déjà bon, répète-le.
   - Le terme doit correspondre à ce que les épiceries utilisent dans leurs noms de produits.
   - Inclus les détails importants: "bœuf haché mi-maigre" (pas juste "bœuf haché"),
     "poitrine de poulet désossée" (pas juste "poulet").
   - Si le format demandé n'existe pas mais un format voisin est commun
     (ex: "pommes 3 lb" n'existe pas mais "pommes 4 lb" est commun), utilise le format commun.

2. **Pour chaque magasin**: As-tu déjà trouvé un bon produit correspondant parmi les résultats ci-dessus?
   - Si OUI: indique l'URL du meilleur produit.
   - Si NON: indique qu'il faut chercher à nouveau avec le terme optimisé.

Réponds en JSON brut (sans ```) avec cette structure:
{{"refined_query": "terme optimisé", "found": {{"store": "URL", ...}}, "missing": ["store1", ...], "reasoning": "explication courte"}}

Rappel: les 4 magasins sont: superc, maxi, metro, iga"""

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
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        if not json_match:
            print(f"  [REFINE] Could not parse JSON:\n{content[:300]}",
                  file=sys.stderr)
            return None

        result = json.loads(json_match.group())
        reasoning = result.get("reasoning", "")
        if reasoning:
            print(f"  [REFINE] {reasoning}", file=sys.stderr)

        refined = result.get("refined_query", product_name)
        found = result.get("found", {})
        missing = result.get("missing", [])

        # Normalize store keys to lowercase (LLM may return "IGA" instead of "iga")
        found = {k.lower(): v for k, v in found.items()}
        missing = [k.lower() for k in missing]

        # Validate found URLs against candidates
        valid_found = {}
        for store_key, url in found.items():
            if store_key not in STORES:
                continue
            store_urls = {cd["url"] for cd in all_candidates.get(store_key, [])}
            if url in store_urls:
                valid_found[store_key] = url
            else:
                for cu in store_urls:
                    if cu.rstrip("/") == url.rstrip("/"):
                        valid_found[store_key] = cu
                        break
                else:
                    if store_key not in missing:
                        missing.append(store_key)

        for sk in STORES:
            if sk not in valid_found and sk not in missing:
                if sk in all_candidates:
                    missing.append(sk)

        print(f"  [REFINE] refined_query=\'{refined}\' found={list(valid_found)} "
              f"missing={missing}", file=sys.stderr)

        return {
            "refined_query": refined,
            "found": valid_found,
            "missing": missing,
        }

    except Exception as e:
        print(f"  [REFINE] Error: {e}", file=sys.stderr)
        return None


# ── Search hints persistence ──────────────────────────────────────────────────

def _load_search_hints() -> dict:
    if SEARCH_HINTS_JSON.exists():
        return json.loads(SEARCH_HINTS_JSON.read_text())
    return {}


def _save_search_hint(product_name: str, refined_query: str) -> None:
    hints = _load_search_hints()
    hints[product_name] = refined_query
    SEARCH_HINTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_HINTS_JSON.write_text(json.dumps(hints, ensure_ascii=False, indent=2))


# ── Two-pass URL finder (shared by find_single and find_batch) ────────────

async def _find_product_urls(
    browser, product_name: str, product_unit: str = "",
    use_llm: bool = True, store_keys: list[str] | None = None,
) -> dict[str, str]:
    """Two-pass URL finder. Returns {store_key: url}.

    Pass 1: Search all stores with original term, extract top 3 per store.
    LLM refine: Analyze results, suggest better search keywords.
    Pass 2: Re-search missing stores with refined query (8 pages).
    LLM final: Pick best comparable set across all stores.
    """
    stores_to_search = store_keys or list(STORES.keys())
    has_llm = use_llm and bool(_load_openrouter_key())

    # === PASS 1: Broad search ===
    print(f"  --- Pass 1: searching \'{product_name}\' ---", file=sys.stderr)
    search_tasks = {sk: search_product(browser, sk, product_name)
                    for sk in stores_to_search}
    search_results = await asyncio.gather(*search_tasks.values())
    all_urls = dict(zip(search_tasks.keys(), search_results))
    for sk, urls in all_urls.items():
        status = f"{len(urls)} results" if urls else "NOT FOUND"
        print(f"  [{sk}] {status}", file=sys.stderr)

    if not has_llm:
        return {sk: urls[0] for sk, urls in all_urls.items() if urls}

    # Extract info from top 3 per store
    print("  Extracting product info (pass 1)...", file=sys.stderr)
    extract_tasks = {
        sk: _get_candidates_with_info(browser, sk, urls)
        for sk, urls in all_urls.items() if urls
    }
    extract_results = await asyncio.gather(*extract_tasks.values())
    all_candidates = dict(zip(extract_tasks.keys(), extract_results))

    for sk, candidates in all_candidates.items():
        for cd in candidates:
            brand = f" [{cd['brand']}]" if cd.get("brand") else ""
            print(f"    [{sk}] {cd['title']}{brand}", file=sys.stderr)

    # === LLM REFINE: Analyze and suggest better search terms ===
    print("  LLM analyzing results...", file=sys.stderr)
    refinement = await _llm_refine_query(product_name, product_unit, all_candidates)

    if refinement is None:
        print("  LLM refine failed, doing standard pick...", file=sys.stderr)
        picks = await _llm_pick_best(product_name, product_unit, all_candidates)
        return picks or {sk: urls[0] for sk, urls in all_urls.items() if urls}

    refined_query = refinement["refined_query"]
    found_stores = refinement["found"]
    missing_stores = refinement["missing"]

    # Only re-search stores we actually need targets for
    missing_stores = [sk for sk in missing_stores if sk in stores_to_search]

    _save_search_hint(product_name, refined_query)

    if not missing_stores:
        print("  All stores matched in pass 1, doing final pick...", file=sys.stderr)
        picks = await _llm_pick_best(product_name, product_unit, all_candidates)
        return picks or found_stores

    # === PASS 2: Refined search for missing stores ===
    print(f"  --- Pass 2: searching \'{refined_query}\' for {missing_stores} ---",
          file=sys.stderr)

    search_tasks_2 = {sk: search_product(browser, sk, refined_query)
                      for sk in missing_stores}
    search_results_2 = await asyncio.gather(*search_tasks_2.values())
    urls_pass2 = dict(zip(search_tasks_2.keys(), search_results_2))
    for sk, urls in urls_pass2.items():
        status = f"{len(urls)} results" if urls else "NOT FOUND"
        print(f"  [{sk}] pass 2: {status}", file=sys.stderr)

    print("  Extracting product info (pass 2, up to 8 pages)...", file=sys.stderr)
    extract_tasks_2 = {
        sk: _get_candidates_with_info(browser, sk, urls,
                                      max_pages=_MAX_DETAIL_PAGES_PASS2)
        for sk, urls in urls_pass2.items() if urls
    }
    extract_results_2 = await asyncio.gather(*extract_tasks_2.values())
    candidates_pass2 = dict(zip(extract_tasks_2.keys(), extract_results_2))

    for sk, candidates in candidates_pass2.items():
        for cd in candidates:
            brand = f" [{cd['brand']}]" if cd.get("brand") else ""
            print(f"    [{sk}] (pass 2) {cd['title']}{brand}", file=sys.stderr)

    # === MERGE candidates ===
    merged = {}
    for sk in stores_to_search:
        seen_urls = set()
        combined = []
        for cd in all_candidates.get(sk, []):
            if cd["url"] not in seen_urls:
                seen_urls.add(cd["url"])
                combined.append(cd)
        for cd in candidates_pass2.get(sk, []):
            if cd["url"] not in seen_urls:
                seen_urls.add(cd["url"])
                combined.append(cd)
        if combined:
            merged[sk] = combined

    # === FINAL LLM PICK ===
    print("  LLM final matching...", file=sys.stderr)
    picks = await _llm_pick_best(product_name, product_unit, merged)

    if picks:
        return picks

    result = dict(found_stores)
    for sk in missing_stores:
        urls = urls_pass2.get(sk, []) or all_urls.get(sk, [])
        if urls:
            result[sk] = urls[0]
    return result


# ── Orchestration ─────────────────────────────────────────────────────

async def find_single(search_term: str, use_llm: bool = True) -> None:
    """Search all 4 stores for one product using 2-pass approach."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        print(f"# Searching for: {search_term}", file=sys.stderr)

        picks = await _find_product_urls(browser, search_term, use_llm=use_llm)

        print("product_slug,store_slug,url,use_playwright,parser")
        for store_key, url in picks.items():
            info = STORES[store_key]
            print(f"SLUG,{info['slug']},{url},{info['use_playwright']},{store_key}")

        for sk in STORES:
            if sk not in picks:
                print(f"# [{sk}] NOT FOUND", file=sys.stderr)

        await browser.close()
async def find_batch(limit: int | None = None, use_llm: bool = True) -> None:
    """Find URLs for all products missing targets using 2-pass approach."""
    existing = _load_existing_targets()
    missing = _products_missing_targets(existing)

    if not missing:
        print("All products already have targets.", file=sys.stderr)
        return

    if limit:
        missing = missing[:limit]

    has_llm = use_llm and bool(_load_openrouter_key())
    mode = "2-pass LLM" if has_llm else "first-match"
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
            needed_keys = [
                k for k in STORES
                if (slug, STORES[k]["slug"]) not in existing
            ]

            picks = await _find_product_urls(
                browser, name, unit, use_llm=has_llm, store_keys=needed_keys
            )

            # Record results — write incrementally
            product_rows = []
            for store_key in needed_keys:
                info = STORES[store_key]
                url = picks.get(store_key)
                if url:
                    row = {
                        "product_slug": slug,
                        "store_slug": info["slug"],
                        "url": url,
                        "use_playwright": info["use_playwright"],
                        "parser": store_key,
                    }
                    product_rows.append(row)
                    print(f"  [{store_key}] ✓ {url}", file=sys.stderr)

            if product_rows:
                file_exists = TARGETS_CSV.exists()
                with open(TARGETS_CSV, "a", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(
                        f,
                        fieldnames=["product_slug", "store_slug", "url",
                                     "use_playwright", "parser"],
                    )
                    if not file_exists:
                        writer.writeheader()
                    writer.writerows(product_rows)
                new_rows.extend(product_rows)
                existing.update(
                    (r["product_slug"], r["store_slug"]) for r in product_rows
                )

        await browser.close()

    print(f"\nDone. Appended {len(new_rows)} rows to {TARGETS_CSV}",
          file=sys.stderr)


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
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached pages and search results")
    args = parser.parse_args()

    if args.no_cache:
        global _USE_CACHE
        _USE_CACHE = False

    if args.batch:
        asyncio.run(find_batch(args.limit, use_llm=not args.no_llm))
    elif args.search_term:
        asyncio.run(find_single(args.search_term, use_llm=not args.no_llm))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
