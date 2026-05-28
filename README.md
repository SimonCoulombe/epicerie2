# Épicerie — Grocery Price Tracker

Daily scraper that tracks grocery prices across 4 Quebec store chains (Super C, Maxi, Metro, IGA),
pinned to a specific Lévis location (postal code G6W2X5).
Data is stored in DuckDB, served via a FastAPI JSON API, and visualized with a Plotly.js dashboard.

**Live:** https://epicerie.proutgpt.com

## Current Status

- **15 products** tracked across **4 stores** = **54 active targets** on Oracle VM + **18 IGA targets** scraped from Raspberry Pi (residential IP required — see [IGA Pi Bridge](#iga-pi-bridge))
- **194 products defined** in `config/products.csv` (full StatCan CPI basket), ~179 awaiting URL discovery
- **Next step:** Use `url_finder.py` to discover URLs for the remaining ~179 products → see [plan-expansion-ipc.md](plan-expansion-ipc.md)

### Currently tracked products

Ananas, Avocats, Bananes, Beurre 454g, Bleuets frais, Bœuf haché, Brocoli, Cantaloup,
Fraises 454g, Lait 2% 2L, Oranges, Poires fraîches, Poitrine de poulet, Pommes, Raisins frais
v
### Stores tracked (all in Lévis, QC)

| Chain | Store | Address | Chain Store ID |
|-------|-------|---------|---------------|
| Super C | Super C – Lévis | 1400 Boul. Alphonse-Desjardins, G6V 9X7 | 633 |
| Maxi | Maxi Lévis President Kennedy | 50, rte. du President Kennedy, G6V 6W8 | 8939 |
| Metro | Alimentation Maxime Faucher | 8032 Avenue Des Églises, G6X 1X7 | 82 |
| IGA | IGA Lévis (Sobeys Capital inc.) | 3950 blv. Guillaume-Couture, G6W 1H7 | 8490 |

## Architecture

```
Browser → nginx (HTTPS + rate limiting)
               ├── /       → static frontend (Plotly.js charts)
               └── /api/*  → FastAPI (uvicorn, port 8000) → DuckDB

Oracle VM cron (8 AM ET) → scraper.main → DuckDB
  ├── 1 async worker per chain (Metro, SuperC, Maxi) — parallel
  │   └── Each worker scrapes its targets sequentially (rate-limited)
  ├── Playwright + 2-context trick (Metro/SuperC) → HTML → JSON-LD parser
  └── Playwright + store cookie (Maxi)             → HTML → JSON-LD parser

Raspberry Pi cron (7:30 AM ET) → scrape_iga.py (residential IP)
  └── stdlib urllib → JSON-LD parser → JSON stdout
      └── SSH pipe → import_iga.py on Oracle VM → DuckDB
```

## Project Structure

```
epicerie2/
├── scraper/                    # Python scraper (python -m scraper.main)
│   ├── main.py                 #   CLI entry point + orchestration
│   ├── browser.py              #   Playwright store-aware HTML fetcher
│   ├── parsers.py              #   Per-store price extraction (JSON-LD + CSS fallback)
│   ├── db.py                   #   DuckDB schema, migrations, CRUD
│   └── url_finder.py           #   LLM-assisted URL discovery (one-shot setup tool)
├── api/
│   └── main.py                 # FastAPI app — JSON endpoints for dashboard
├── frontend/
│   ├── index.html              # Single-page app shell (French UI)
│   ├── app.js                  # Plotly.js charts + fetch + filters
│   └── style.css               # Responsive layout
├── config/
│   ├── products.csv            # Master product list (194 StatCan CPI items)
│   ├── targets.csv             # Active scrape targets (product × store × URL)  ← PRIMARY
│   ├── targets.yaml            # Store metadata + legacy product/target defs
│   └── search_hints.json       # Per-product LLM hints for url_finder.py
├── cache/                      # url_finder.py LLM + scrape cache (gitignored)
├── data/                       # DuckDB database file (gitignored)
├── logs/                       # Cron + API logs (gitignored)
├── requirements.txt            # Pinned Python dependencies
├── plan-expansion-ipc.md       # Expansion plan: 194 CPI products
├── epicerie-api.service        # systemd unit file
├── epicerie-nginx.conf         # nginx site config
└── prix_fraises.csv            # Original strawberry CSV data (migrated to DuckDB on first run)
```

## Tech Stack

- **Python 3.12** — venv at `/home/ubuntu/epicerie2/venv`
- **Playwright 1.58.0** — headless Chromium (ARM64), stealth anti-bot
- **httpx 0.28.1** — plain HTTP fetcher (Metro/SuperC/Maxi store setup calls)
- **DuckDB 1.5.1** — embedded DB at `data/epicerie.duckdb`
- **FastAPI 0.135.3** — JSON API, uvicorn on port 8000
- **Plotly.js 2.35.2** — frontend charts (CDN)
- **OpenRouter API** — `google/gemini-2.5-flash` for url_finder.py (key in `~/.env` as `OPENROUTER_API_KEY`)
- **nginx** — SSL (Let's Encrypt), rate limiting, static serving
- **Ubuntu 22.04** — ARM64 VM, America/Toronto timezone
- **Raspberry Pi 2** (`simon@192.168.2.14`) — IGA scraper only; pure Python stdlib (no extra deps); residential IP bypasses Akamai block

---

## Scraper Deep Dive

### `scraper/main.py` — Orchestration

Entry point: `python -m scraper.main`

1. Loads `config/targets.yaml` (store metadata) and `config/targets.csv` (scrape targets)
2. Calls `sync_targets()` to upsert stores, products, and targets into DuckDB
3. Gets all active targets with `get_active_targets()` — returns dicts with `chain_store_id`
4. Groups targets by store chain
5. Spins up **1 async worker per chain** via `asyncio.gather()` — all chains run in parallel
6. Each worker scrapes its targets **sequentially** with rate-limiting delays:
   - IGA: 1.0 s between requests
   - all others: 2.0 s between requests
7. Each target: calls `browser.fetch_html()` or `_fetch_plain()` (for httpx targets), then the
   appropriate parser, then `save_price()` to DuckDB
8. **Retry logic:** `_MAX_RETRIES = 2`, `_RETRY_BASE_DELAY = 3.0 s` exponential backoff
9. **Circuit breaker:** 5 consecutive failures on a chain → abort that chain's worker

### `scraper/browser.py` — Store-Aware HTML Fetcher

Playwright wrapper that ensures every product page is served from the correct Lévis store.
Each chain uses a different store-selection mechanism.

#### Metro — 2-context approach (JSESSIONID pinning)

Metro uses server-side sessions. Navigating from the store-selection page directly to a product
page in the same browser context triggers Cloudflare bot detection. Solution: 2 separate contexts.

**Context A (store setup, discarded):**
1. Navigate to `https://www.metro.ca/en/find-a-grocery/result-page?...` for store #82
2. Read CSRF token from `<meta name="_csrf">` via `document.querySelector(...).content`
3. POST to `/epicerie-en-ligne/set-preferred-store` with JSON body + `X-CSRF-TOKEN` header
4. Capture the `JSESSIONID` cookie — this is the store-pinned session token
5. Close context A

**Context B (fresh, per product request):**
- Create a new clean context with only `JSESSIONID={captured value}` set
- Fetch the product page — server uses the session to serve Lévis store prices

`_obtain_metro_session(browser, chain, chain_store_id)` handles all of this. The JSESSIONID is
cached in `_chain_sessions` and reused for all Metro targets until it expires.

#### Super C — 2-context approach (no CSRF)

Same 2-context pattern as Metro, but Super C's store-selection API is simpler:
- POST to `/stores/my-store/{store_id}` with `Content-Type: application/x-www-form-urlencoded`
- Body: `userConfirmation=true&lang=fr`
- No CSRF token needed
- JSESSIONID from the response is reused for all Super C targets

#### Maxi — persistent context with cookie

No session API needed. Set `auto_store_selected=8939` cookie (domain `www.maxi.ca`) and Maxi
serves all prices for that store. Uses a single long-lived Playwright context for all Maxi requests.

Note: The header banner may still say "MAXI LAVAL LAURENTIDES" — this is a rendering artifact.
The actual prices ARE from the Lévis store (confirmed via cart API returning `storeId=8939`).

#### IGA — httpx with cookies (no Playwright)

IGA's product pages don't require JavaScript — plain HTTP works. Store selection via cookies:
- `storeId=8490_Quebec`
- `selected_store_region=Quebec`

These are passed via httpx. No Playwright context is created for IGA targets.

### `scraper/parsers.py` — Price Extraction

`enrich_price_per_kg(result)` runs after parsing. If `price_per_kg` is still `None` and the
product title contains a weight (e.g. `"454g"`, `"3lb"`), it computes `$/kg = price / weight_kg`.

Some products are sold by volume or in incomparable units across stores. For these, `price_per_kg`
is suppressed entirely (set to `NULL`) via `_NO_KG_SLUGS` in `main.py`:
- **bleuets-frais** — pint containers (170 mL), stores report mL not grams
- **lait-2pct-2l** — 2 L cartons; comparison would be $/L, not $/kg

All 4 stores embed structured data as JSON-LD in their product pages. Primary strategy for all:

```python
# Find <script type="application/ld+json"> with "@type": "Product"
# Extract offers.price (or offers[0].price)
```

**`PriceResult` dataclass:**
```python
@dataclass
class PriceResult:
    price: float
    unit: str              # "each", "kg", "lb", "100ml", "500g", etc.
    price_per_kg: float | None
    title: str             # Product title as shown on the store page
```

Parser dispatch is keyed by the `parser` field in `targets.csv` (or store slug prefix as fallback):

| Parser | Strategy | Notes |
|--------|----------|-------|
| `parse_superc` | JSON-LD first, `.pi--prices` CSS fallback | |
| `parse_metro` | Scopes `data-main-price` lookup to `.product-info` to skip carousel tiles | Metro added a promoted-product widget (May 2026) whose div also carries `data-main-price`; page-wide `find()` returned the widget price for every product |
| `parse_maxi` | JSON-LD; URL suffix `_KG` vs `_EA` determines unit | |
| `parse_iga` | JSON-LD only | IGA's structured data is reliable |

### `scraper/db.py` — DuckDB Schema & Migrations

`_migrate_schema()` runs on every startup to add new columns to existing databases
without breaking anything. Key tables:

```sql
products (
    id INTEGER PRIMARY KEY,
    name VARCHAR,
    slug VARCHAR UNIQUE,
    category VARCHAR,          -- e.g. "Viandes fraîches"
    cpi_name VARCHAR,          -- StatCan CPI basket name
    unit VARCHAR               -- canonical unit description
)

store_chains (id, name UNIQUE)
cities (id, name UNIQUE, slug UNIQUE)

stores (
    id INTEGER PRIMARY KEY,
    store_chain_id FK,
    city_id FK,
    chain_store_id VARCHAR,    -- chain's internal store ID (e.g. "8490")
    store_name VARCHAR,        -- human-readable name
    address VARCHAR,
    postal_code VARCHAR,
    slug VARCHAR UNIQUE
)

scrape_targets (
    id INTEGER PRIMARY KEY,
    product_id FK,
    store_id FK,
    url VARCHAR,
    use_playwright BOOLEAN,
    active BOOLEAN DEFAULT TRUE,
    parser VARCHAR,
    last_success DATE,
    fail_count INTEGER DEFAULT 0,
    product_title VARCHAR,      -- last scraped title (for URL validation)
    UNIQUE(product_id, store_id)
)

prices (
    id INTEGER PRIMARY KEY,
    scrape_target_id FK,
    date DATE,
    price DECIMAL(8,2),
    price_unit VARCHAR,         -- "each", "kg", etc.
    price_per_kg DECIMAL(8,2),  -- normalized $/kg comparison (NULL if not applicable)
    scraped_at TIMESTAMP,
    UNIQUE(scrape_target_id, date)
)
```

`sync_targets(config)` upserts from `targets.yaml` (stores) and `targets.csv` (targets).
The ON CONFLICT for stores does a full UPDATE so `chain_store_id` and `store_name` stay current.

---

## URL Finder (`scraper/url_finder.py`)

One-shot tool for discovering correct product page URLs. Only needs to run once per product.
It's slow (~2-5 min per product) but runs completely unattended.

```bash
# Find URLs for a single product
python -m scraper.url_finder "Bœuf haché mi-maigre"

# Batch mode — process all products missing URLs in targets.csv
python -m scraper.url_finder --batch

# Batch with limit (for testing)
python -m scraper.url_finder --batch --limit 5

# Batch without LLM (first-match fallback, faster but less accurate)
python -m scraper.url_finder --batch --no-llm
```

**4-step process per product:**
1. **SEARCH** — Search all 4 stores in parallel, collect top ~8 product URLs from each
2. **EXTRACT** — Visit top 3 candidate pages per store, extract brand + title via JSON-LD
3. **REFINE** — LLM (Gemini 2.5 Flash via OpenRouter) analyzes results, identifies which stores
   already have a match, suggests better search keywords for the rest
4. **PASS 2** — Re-search missing stores with refined query, LLM picks best comparable set

Results are written to `config/targets.csv`. LLM responses and scraped pages are cached in
`cache/` so re-runs are fast and cheap.

**Prerequisites:**
- `OPENROUTER_API_KEY` in `~/.env` (or environment)
- Playwright Chromium installed

**Adding hints for stubborn products** — edit `config/search_hints.json`:
```json
{
  "boeuf-hache": "mi-maigre 900g OR 1kg",
  "pain-blanc-675g": "pain tranché 675g Gadoua OR Wonder"
}
```

---

## How to Add New Products

### Option A: Use url_finder.py (recommended for batches)

```bash
source venv/bin/activate
python -m scraper.url_finder "Côtelettes de porc"
# Results appended to config/targets.csv automatically
```

### Option B: Add manually to targets.csv

`config/targets.csv` format:
```
product_slug,store_slug,url,use_playwright,parser
```

Example:
```
oeufs-gros-12,superc-default,https://www.superc.ca/allees/.../p/12345,true,superc
oeufs-gros-12,maxi-default,https://www.maxi.ca/fr/oeufs.../p/99999_EA,true,maxi
oeufs-gros-12,metro-default,https://www.metro.ca/epicerie-en-ligne/allees/.../p/12345,true,metro
oeufs-gros-12,iga-default,https://www.iga.ca/fr/produits/oeufs-gros,false,iga
```

- `store_slug` must match a slug defined in `config/targets.yaml` (stores section)
- `use_playwright`: `true` for Metro/SuperC/Maxi, `false` for IGA
- `parser`: `superc`, `metro`, `maxi`, or `iga`

After editing, just run `python -m scraper.main` — `sync_targets()` picks up new entries automatically.

### Adding a new store or city

Edit `config/targets.yaml`. The stores section drives both DuckDB population and the chain-specific
store-selection logic in `browser.py`. Key fields:
- `chain`: must exactly match a name in `store_chains` table ("Super C", "Maxi", "Metro", "IGA")
- `chain_store_id`: the chain's internal numeric store ID
- `slug`: unique identifier referenced in `targets.csv`

---

## Anti-bot / Cloudflare Notes

### IGA — Akamai IP block

IGA uses Akamai Bot Manager. Oracle Cloud VM IPs are blocked at the network level regardless
of browser fingerprinting or headers. Plain `httpx` from a residential IP works fine.
See [IGA Pi Bridge](#iga-pi-bridge) for the scraping architecture.


**The problem:** Metro and Super C serve product pages through Cloudflare. Visiting the
store-selection page and then a product page in the same browser context triggers bot detection.

**The fix:** 2-context approach — never navigate from a "setup" page to a product page in the
same context. Always use a fresh context for product fetches, pre-loaded with only the JSESSIONID
cookie from a discarded setup context.

**Stealth settings applied to all Playwright contexts:**
- `domcontentloaded` event + selector polling (never `networkidle`)
- Windows User-Agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...`
- `navigator.webdriver` set to `undefined` via `_WEBDRIVER_HIDE` script
- 1920×1080 viewport
- No extra HTTP headers or JS overrides beyond the above

---


## IGA Pi Bridge

IGA uses Akamai Bot Manager which blocks all Oracle Cloud IP ranges. Workaround: scrape IGA
from a Raspberry Pi on a home network (residential IP).

### Architecture

```
Pi (residential IP, 7:30 AM ET)
  scrape_iga.py
    → urllib.request (pure stdlib, no extra deps)
    → 18 IGA product pages → JSON-LD price parser
    → JSON to stdout
    → SSH pipe to Oracle VM
         import_iga.py
           → DuckDB upsert (same prices table)
```

### Files

| File | Location | Purpose |
|------|----------|---------|
| `scrape_iga.py` | Pi: `~/scrape_iga.py` | Fetches 18 IGA targets, outputs JSON |
| `import_iga.py` | Oracle VM: `~/epicerie2/import_iga.py` | Reads JSON from stdin, upserts to DuckDB |

### Pi cron (simon@192.168.2.14)

```
30 7 * * * python3 /home/simon/scrape_iga.py 2>>/home/simon/scrape_iga.log | ssh ubuntu@148.116.71.245 "cd ~/epicerie2 && venv/bin/python import_iga.py" >>/home/simon/scrape_iga.log 2>&1
```

### IGA targets in DuckDB

IGA `scrape_targets` are set to `active = FALSE` on the Oracle VM so `scraper.main` skips them.
The Pi's `import_iga.py` calls `update_target_status(success=True)` after each upsert.

### Pi SSH key

Pi uses `~/.ssh/id_ed25519` (ed25519, comment: `pi-iga-scraper`).
Public key is in Oracle VM's `~/.ssh/authorized_keys`.

### Checking Pi logs

```bash
ssh simon@192.168.2.14 "tail -40 ~/scrape_iga.log"
```

## Setup on a New VM

### Prerequisites

- Ubuntu 22.04+ (ARM64 or x86_64)
- Python 3.12+
- nginx
- certbot

### 1. Clone and create virtual environment

```bash
git clone <repo-url> /home/ubuntu/epicerie2
cd /home/ubuntu/epicerie2
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
```

### 2. Initialize database and run first scrape

```bash
# Sync targets.yaml to DuckDB + migrate CSV data + scrape all stores
python -m scraper.main
```

### 3. Install systemd service

```bash
sudo cp epicerie-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now epicerie-api
```

Verify: `curl http://127.0.0.1:8000/api/products`

### 4. Configure nginx

Add rate limiting zones to `/etc/nginx/nginx.conf` inside the `http {}` block:

```nginx
limit_req_zone $binary_remote_addr zone=epicerie_req:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=epicerie_conn:10m;
```

Install the site config:

```bash
sudo cp epicerie-nginx.conf /etc/nginx/sites-available/epicerie
sudo ln -s /etc/nginx/sites-available/epicerie /etc/nginx/sites-enabled/
```

Fix home directory permissions so nginx (www-data) can serve static files:

```bash
chmod o+x /home/ubuntu /home/ubuntu/epicerie2 /home/ubuntu/epicerie2/frontend
```

Test and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 5. DNS + SSL

1. Add a DNS A record: `epicerie.proutgpt.com` → VM public IP
2. Run certbot:

```bash
sudo certbot --nginx -d epicerie.proutgpt.com
```

### 6. Cron (daily scraping)

```bash
sudo timedatectl set-timezone America/Toronto
crontab -e
# Add:
0 8 * * * cd /home/ubuntu/epicerie2 && /home/ubuntu/epicerie2/venv/bin/python -m scraper.main >> /home/ubuntu/epicerie2/logs/scrape.log 2>&1
```

IGA is scraped from the Raspberry Pi at 7:30 AM (30 min before Oracle VM) — see [IGA Pi Bridge](#iga-pi-bridge).

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/products?category=&has_data=` | List products (filter by category; `has_data=true` for only scraped ones) |
| `GET /api/store-chains` | List all store chains |
| `GET /api/cities` | List all cities |
| `GET /api/stores` | List all stores with chain/city info |
| `GET /api/prices?product=&city=&chain=&from=&to=` | Price data — returns `store_name` field |

The API is mounted at port 8000 (uvicorn). `/` serves `frontend/index.html` as a static SPA.

---

## Expansion Plan

See [plan-expansion-ipc.md](plan-expansion-ipc.md) for the roadmap to expand from 15 products
to all ~194 StatCan CPI food items. Short version:

1. Run `url_finder.py --batch` in chunks to discover URLs for remaining ~179 products
2. Validate scraped titles look correct (url_finder caches titles in targets.csv)
3. The scraper handles all 4 chains generically — no code changes needed for new products
