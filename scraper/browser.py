"""Playwright browser helper — launch headless Chromium, fetch page HTML.

Maintains one browser context per store chain so that store-selection
cookies/sessions persist across all requests for that chain.

Metro / Super C use a two-context approach to avoid Cloudflare:
  1. Temporary context visits the store detail page, POSTs to set the store,
     captures the JSESSIONID that now carries the store selection server-side.
  2. A fresh scraping context is created with just that JSESSIONID cookie.
     Product pages load without Cloudflare challenges in this clean context.

IGA and Maxi use simple cookie-based store selection.
"""

from __future__ import annotations

from playwright.async_api import async_playwright, Browser, BrowserContext

# Selectors that indicate price content has loaded
_PRICE_SELECTORS = (
    'script[type="application/ld+json"]',
    ".pi--prices",
    "[data-testid='product-price']",
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_WEBDRIVER_HIDE = (
    'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
)


# ── Chain-specific store-selection helpers ────────────────────────────

async def _obtain_metro_session(browser: Browser, chain: str,
                                chain_store_id: str) -> str:
    """Set store for Metro / Super C and return the JSESSIONID.

    Opens a temporary context, visits the store detail page (which is
    not blocked by Cloudflare), POSTs to select the store, captures the
    JSESSIONID cookie, and closes the temporary context.
    """
    domain = "metro.ca" if chain == "Metro" else "superc.ca"
    store_url = f"https://www.{domain}/trouver-une-epicerie/{chain_store_id}"

    ctx = await browser.new_context(user_agent=_UA, locale="fr-CA",
                                    viewport={"width": 1920, "height": 1080})
    await ctx.add_cookies([{
        "name": "OptanonAlertBoxClosed",
        "value": "2024-01-01T00:00:00.000Z",
        "domain": f".{domain}", "path": "/",
    }])
    page = await ctx.new_page()
    await page.add_init_script(_WEBDRIVER_HIDE)
    try:
        await page.goto(store_url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)

        if chain == "Metro":
            # Metro requires CSRF token from a <meta> tag
            result = await page.evaluate("""async (storeId) => {
                const csrf = document.querySelector('meta[name="_csrf"]')?.content;
                if (!csrf) return {ok: false, reason: 'no_csrf'};
                const resp = await fetch('/stores/my-store/' + storeId, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-TOKEN': csrf,
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: JSON.stringify({userConfirmation: true, lang: 'fr'})
                });
                return {ok: resp.ok, status: resp.status};
            }""", chain_store_id)
        else:
            # Super C uses form-urlencoded, no CSRF needed
            result = await page.evaluate("""async (storeId) => {
                const resp = await fetch('/stores/my-store/' + storeId, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: 'userConfirmation=true&lang=fr'
                });
                return {ok: resp.ok, status: resp.status};
            }""", chain_store_id)

        if result.get("ok"):
            print(f"  [{chain}] Store set to #{chain_store_id}")
        else:
            print(f"  [{chain}] WARNING: store selection returned {result}")

        cookies = await ctx.cookies()
        jsession = next((c["value"] for c in cookies if c["name"] == "JSESSIONID"), "")
        return jsession
    finally:
        await page.close()
        await ctx.close()



async def _create_iga_context(browser: Browser,
                              chain_store_id: str) -> BrowserContext:
    """Create a scraping context for IGA with store cookie."""
    ctx = await browser.new_context(user_agent=_UA, locale="fr-CA",
                                    viewport={"width": 1920, "height": 1080})
    await ctx.add_cookies([
        {"name": "storeId", "value": f"{chain_store_id}_Quebec",
         "domain": ".iga.ca", "path": "/"},
        {"name": "selected_store_region", "value": "Quebec",
         "domain": ".iga.ca", "path": "/"},
        {"name": "OptanonAlertBoxClosed",
         "value": "2024-01-01T00:00:00.000Z",
         "domain": ".iga.ca", "path": "/"},
    ])
    print(f"  [IGA] Store cookie set to #{chain_store_id}")
    return ctx


async def _create_maxi_context(browser: Browser,
                               chain_store_id: str) -> BrowserContext:
    """Create a scraping context for Maxi with store cookie."""
    ctx = await browser.new_context(user_agent=_UA, locale="fr-CA",
                                    viewport={"width": 1920, "height": 1080})
    await ctx.add_cookies([
        {"name": "auto_store_selected", "value": chain_store_id,
         "domain": "www.maxi.ca", "path": "/"},
        {"name": "OptanonAlertBoxClosed",
         "value": "2024-01-01T00:00:00.000Z",
         "domain": ".maxi.ca", "path": "/"},
    ])
    print(f"  [Maxi] Store cookie set to #{chain_store_id}")
    return ctx



# ─────────────────────────────────────────────────────────────────────


class PlaywrightBrowser:
    """Manages a single browser instance with per-chain contexts for store selection.

    Metro / Super C: Each product page request gets a FRESH context pre-loaded
    with the chain's JSESSIONID (obtained once at setup time). This prevents
    Cloudflare from flagging the session after multiple page navigations.

    IGA / Maxi: A single persistent context with store cookies is reused.
    """

    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None
        # For IGA / Maxi — persistent contexts
        self._chain_contexts: dict[str, BrowserContext] = {}
        # For Metro / Super C — stored JSESSIONID + domain for per-request contexts
        self._chain_sessions: dict[str, dict] = {}

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def setup_chain(self, chain: str, chain_store_id: str) -> None:
        """Pre-configure a chain's store selection (called once before scraping)."""
        if chain in self._chain_contexts or chain in self._chain_sessions:
            return
        if chain in ("Metro", "Super C"):
            domain = "metro.ca" if chain == "Metro" else "superc.ca"
            jsession = await _obtain_metro_session(
                self._browser, chain, chain_store_id)
            self._chain_sessions[chain] = {
                "jsession": jsession,
                "domain": domain,
            }
        elif chain == "IGA":
            ctx = await _create_iga_context(self._browser, chain_store_id)
            self._chain_contexts[chain] = ctx
        elif chain == "Maxi":
            ctx = await _create_maxi_context(self._browser, chain_store_id)
            self._chain_contexts[chain] = ctx
        else:
            ctx = await self._browser.new_context(
                user_agent=_UA, locale="fr-CA",
                viewport={"width": 1920, "height": 1080})
            self._chain_contexts[chain] = ctx

    async def fetch_html(self, url: str, timeout_ms: int = 45_000,
                         chain: str | None = None,
                         chain_store_id: str | None = None) -> str:
        """Navigate to URL, wait for price content, return HTML.

        For Metro/SuperC: creates a fresh context per request using the stored
        JSESSIONID so each product page appears as a clean browser visit.
        For IGA/Maxi: reuses the persistent context with store cookies.
        """
        if chain and chain not in self._chain_contexts and chain not in self._chain_sessions and chain_store_id:
            await self.setup_chain(chain, chain_store_id)

        if chain and chain in self._chain_sessions:
            # Metro / Super C: fresh context per request
            session = self._chain_sessions[chain]
            domain = session["domain"]
            jsession = session["jsession"]
            context = await self._browser.new_context(
                user_agent=_UA, locale="fr-CA",
                viewport={"width": 1920, "height": 1080})
            cookies = [{
                "name": "OptanonAlertBoxClosed",
                "value": "2024-01-01T00:00:00.000Z",
                "domain": f".{domain}", "path": "/",
            }]
            if jsession:
                cookies.append({
                    "name": "JSESSIONID",
                    "value": jsession,
                    "domain": f".{domain}", "path": "/",
                })
            await context.add_cookies(cookies)
            close_ctx = True
        elif chain and chain in self._chain_contexts:
            context = self._chain_contexts[chain]
            close_ctx = False
        else:
            context = await self._browser.new_context(
                user_agent=_UA, locale="fr-CA",
                viewport={"width": 1920, "height": 1080})
            close_ctx = True

        page = await context.new_page()
        await page.add_init_script(_WEBDRIVER_HIDE)
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=timeout_ms)
            selector = ", ".join(_PRICE_SELECTORS)
            try:
                await page.wait_for_selector(selector, timeout=15_000)
            except Exception:
                pass
            await page.wait_for_timeout(2_000)
            return await page.content()
        finally:
            await page.close()
            if close_ctx:
                await context.close()

    async def close(self) -> None:
        for ctx in self._chain_contexts.values():
            await ctx.close()
        self._chain_contexts.clear()
        self._chain_sessions.clear()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
