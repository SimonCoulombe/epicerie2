"""Playwright browser helper — launch headless Chromium, fetch page HTML."""

from playwright.async_api import async_playwright, Browser, BrowserContext

# Selectors that indicate price content has loaded
_PRICE_SELECTORS = (
    'script[type="application/ld+json"]',
    ".pi--prices",
    "[data-testid='product-price']",
)


class PlaywrightBrowser:
    """Manages a single browser instance for reuse across multiple fetches."""

    def __init__(self):
        self._pw = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)

    async def fetch_html(self, url: str, timeout_ms: int = 45_000) -> str:
        """Navigate to URL, wait for price content to render, return HTML.

        Uses domcontentloaded + selector waiting instead of networkidle
        to avoid timeouts on Cloudflare-protected sites (Super C, Metro).
        """
        context: BrowserContext = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="fr-CA",
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        # Hide webdriver flag from bot detection
        await page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
        )
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Wait for any price-related selector to appear
            selector = ", ".join(_PRICE_SELECTORS)
            try:
                await page.wait_for_selector(selector, timeout=15_000)
            except Exception:
                pass  # proceed anyway — some pages may use different selectors
            await page.wait_for_timeout(2_000)
            return await page.content()
        finally:
            await page.close()
            await context.close()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
