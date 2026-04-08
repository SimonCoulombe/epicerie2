# Épicerie — Grocery Price Tracker

Daily scraper that tracks grocery prices across Quebec stores (Super C, Maxi, Metro, IGA).
Data is stored in DuckDB, served via a FastAPI JSON API, and visualized with a Plotly.js dashboard.

**Live:** https://epicerie.proutgpt.com

## Current Status

- **1 product** tracked: Fraises 454g across 4 stores
- **Next step:** Expand to ~175 CPI food products → see [plan-expansion-ipc.md](plan-expansion-ipc.md)

## Architecture

```
Browser → nginx (HTTPS + rate limiting)
               ├── /       → static frontend (Plotly.js charts)
               └── /api/*  → FastAPI (uvicorn, port 8000) → DuckDB
Cron (8 AM ET) → scraper (Playwright + httpx) → DuckDB
```

## Project Structure

```
epicerie2/
├── scraper/                # Python scraper
│   ├── main.py             #   CLI entry point (python -m scraper.main)
│   ├── browser.py          #   Playwright browser helper (stealth, anti-bot)
│   ├── parsers.py          #   Per-store price extraction (JSON-LD + CSS)
│   └── db.py               #   DuckDB operations (schema, migrations, CRUD)
├── api/
│   └── main.py             # FastAPI app — JSON endpoints for dashboard
├── frontend/
│   ├── index.html          # Single-page app shell (French UI)
│   ├── app.js              # Plotly.js charts + fetch logic
│   └── style.css           # Responsive layout
├── config/
│   └── targets.yaml        # Product × store × URL definitions
├── data/                   # DuckDB database file (gitignored)
├── logs/                   # Cron + API logs (gitignored)
├── requirements.txt        # Pinned Python dependencies
├── plan-expansion-ipc.md   # Expansion plan: 175 CPI products
├── epicerie-api.service    # systemd unit file
├── epicerie-nginx.conf     # nginx site config
├── scrape_fraises.R        # Original R scraper (reference only, unused)
└── prix_fraises.csv        # Original CSV data (migrated to DuckDB on first run)
```

## Tech Stack

- **Python 3.12** — venv at `/home/ubuntu/epicerie2/venv`
- **Playwright 1.58.0** — headless Chromium (ARM64), stealth anti-bot
- **DuckDB 1.5.1** — embedded DB at `data/epicerie.duckdb`
- **FastAPI 0.135.3** — JSON API, uvicorn on port 8000
- **Plotly.js 2.35.2** — frontend charts (CDN)
- **nginx** — SSL (Let's Encrypt), rate limiting, static serving
- **Ubuntu 22.04** — ARM64 VM, America/Toronto timezone

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

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/products` | List all tracked products |
| `GET /api/store-chains` | List all store chains |
| `GET /api/cities` | List all cities |
| `GET /api/stores` | List all stores with chain/city info |
| `GET /api/prices?product=&city=&chain=&from=&to=` | Price data (all filters optional) |

## How Prices Are Extracted

| Store | Method | Bot Protection |
|-------|--------|----------------|
| Super C | Playwright → JSON-LD `offers.price`, CSS `.pi--prices` fallback | Cloudflare (bypassed via stealth) |
| Maxi | Playwright → JSON-LD `offers.price` | Cloudflare (bypassed via stealth) |
| Metro | Playwright → JSON-LD `offers.price`, CSS `.pi--prices` fallback | Cloudflare (bypassed via stealth) |
| IGA | httpx (plain HTTP) → JSON-LD `offers.price` | None |

The stealth strategy: `domcontentloaded` wait + selector polling (not `networkidle`), Windows User-Agent, `navigator.webdriver` removal, 1920×1080 viewport.

## DuckDB Schema

```sql
products     (id, name, slug UNIQUE)
store_chains (id, name UNIQUE)
cities       (id, name UNIQUE, slug UNIQUE)
stores       (id, store_chain_id FK, city_id FK, address, postal_code, slug UNIQUE)
scrape_targets (id, product_id FK, store_id FK, url, use_playwright, active, UNIQUE(product_id, store_id))
prices       (id, scrape_target_id FK, date, price DECIMAL(8,2), scraped_at, UNIQUE(scrape_target_id, date))
```

## Adding New Products

Edit `config/targets.yaml` to add new products, stores, or targets. The scraper auto-syncs new entries to DuckDB on each run via `sync_targets()`. No code changes needed — the parsers are generic (JSON-LD + CSS work for any product page on all 4 stores).

## Expansion Plan

See [plan-expansion-ipc.md](plan-expansion-ipc.md) for the detailed plan to expand from 1 product to ~175 StatCan CPI food products. The plan covers:

- Schema changes (new columns on `products` and `scrape_targets`)
- Migration from YAML to CSV config (to handle 800+ targets)
- Semi-automated URL discovery script
- Scraper parallelism for 10-15 min runs
- Dashboard improvements (category filters, multi-product views)
