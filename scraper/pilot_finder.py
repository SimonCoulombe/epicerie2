"""Quick pilot URL finder — searches 4 stores for 9 pilot products."""

import asyncio
import re
from urllib.parse import quote

from playwright.async_api import async_playwright

SEARCH_URLS = {
    "superc": "https://www.superc.ca/recherche?filter={query}",
    "maxi": "https://www.maxi.ca/fr/search?search-bar={query}",
    "metro": "https://www.metro.ca/epicerie-en-ligne/recherche?filter={query}",
    "iga": "https://www.iga.ca/fr?query={query}&tab=products",
}

PRODUCT_URL_PATTERNS = {
    "superc": re.compile(r"https://www\.superc\.ca/allees/.+/p/\d+"),
    "maxi": re.compile(r"https://www\.maxi\.ca/fr/.+/p/.+"),
    "metro": re.compile(r"https://www\.metro\.ca/epicerie-en-ligne/allees/.+/p/\d+"),
    "iga": re.compile(r"https://www\.iga\.ca/fr/produits/.+"),
}

PILOT_SEARCHES = [
    ("bananes", "bananes"),
    ("brocoli", "brocoli"),
    ("boeuf-hache", "boeuf haché"),
    ("poitrine-poulet", "poitrine de poulet"),
    ("lait-2pct-2l", "lait 2%"),
    ("beurre-454g", "beurre"),
    ("oeufs-gros-12", "oeufs"),
    ("pain-blanc-675g", "pain blanc"),
    ("pates-seches-900g", "pâtes"),
]


async def find_urls():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        for product_slug, search_term in PILOT_SEARCHES:
            print(f"\n=== {product_slug} (search: '{search_term}') ===")
            for store, url_template in SEARCH_URLS.items():
                search_url = url_template.format(query=quote(search_term))
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
                    
                    # Extract all links from the page
                    links = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                    """)
                    
                    pattern = PRODUCT_URL_PATTERNS[store]
                    matches = list(dict.fromkeys(url for url in links if pattern.match(url)))
                    
                    if matches:
                        print(f"  [{store}] {matches[0]}")
                        if len(matches) > 1:
                            for m in matches[1:3]:
                                print(f"    alt: {m}")
                    else:
                        print(f"  [{store}] NOT FOUND")
                except Exception as e:
                    print(f"  [{store}] ERROR: {e}")
                finally:
                    await page.close()
                    await context.close()
                    await asyncio.sleep(2)  # Rate limiting

        await browser.close()


if __name__ == "__main__":
    asyncio.run(find_urls())
