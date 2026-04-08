# Plan: Epicerie2 — Grocery Price Tracker Platform

## TL;DR

Rewrite the R strawberry scraper in Python (R not installed, Python 3.12 is), use **Playwright** instead of FlareSolverr to bypass Cloudflare (no Docker needed), store data in **DuckDB** (embedded, zero-config), serve a public dashboard at **epicerie.proutgpt.com** using **FastAPI** (JSON API) + **Plotly.js** (client-side charts), schedule with cron, and protect with nginx rate limiting.

---

## Phase 1: System Setup & Python Environment

1. **Create Python venv** at `/home/ubuntu/epicerie2/venv` using Python 3.12
2. **Install Python packages**: `playwright`, `beautifulsoup4`, `lxml`, `duckdb`, `fastapi`, `uvicorn`, `pyyaml`, `httpx`
3. **Install Playwright Chromium** on ARM64: `playwright install --with-deps chromium` — this installs the headless browser + system deps (libglib, libnss, etc.) without Docker
4. **Create `requirements.txt`** pinning all dependencies for reproducibility

**No R, no Docker, no FlareSolverr needed.**

---

## Phase 2: Project Structure

Reorganize `/home/ubuntu/epicerie2/` into:

```
epicerie2/
├── scraper/
│   ├── __init__.py
│   ├── main.py          # CLI entry point — reads config, scrapes, stores
│   ├── browser.py       # Playwright browser helper (launch, fetch page HTML)
│   ├── parsers.py       # Per-store price extraction (port R logic to Python)
│   └── db.py            # DuckDB read/write operations
├── api/
│   ├── __init__.py
│   └── main.py          # FastAPI app — JSON endpoints for dashboard
├── frontend/
│   ├── index.html        # Single-page app shell
│   ├── app.js            # Plotly.js charts + fetch logic (runs in browser)
│   └── style.css
├── config/
│   └── targets.yaml      # All products × stores × cities definitions
├── data/
│   └── epicerie.duckdb   # Database file (gitignored)
├── logs/                  # Cron log output (gitignored)
├── requirements.txt
├── scrape_fraises.R       # Keep original for reference
├── prix_fraises.csv       # Keep original, migrate data to DuckDB
└── README.md              # Updated docs
```

---

## Phase 3: Database Schema (DuckDB)

DuckDB file at `data/epicerie.duckdb`. Normalized schema to support future multi-product, multi-city expansion:

```sql
CREATE SEQUENCE seq_products START 1;
CREATE TABLE products (
    id INTEGER DEFAULT nextval('seq_products') PRIMARY KEY,
    name VARCHAR NOT NULL,            -- 'Fraises 454g'
    slug VARCHAR UNIQUE NOT NULL      -- 'fraises-454g'
);

CREATE SEQUENCE seq_store_chains START 1;
CREATE TABLE store_chains (
    id INTEGER DEFAULT nextval('seq_store_chains') PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE      -- 'Super C', 'Maxi', 'Metro', 'IGA'
);

CREATE SEQUENCE seq_cities START 1;
CREATE TABLE cities (
    id INTEGER DEFAULT nextval('seq_cities') PRIMARY KEY,
    name VARCHAR NOT NULL UNIQUE,     -- 'Lévis', 'Québec', 'Montréal'
    slug VARCHAR UNIQUE NOT NULL      -- 'levis', 'quebec', 'montreal'
);

CREATE SEQUENCE seq_stores START 1;
CREATE TABLE stores (
    id INTEGER DEFAULT nextval('seq_stores') PRIMARY KEY,
    store_chain_id INTEGER REFERENCES store_chains(id),
    city_id INTEGER REFERENCES cities(id),
    address VARCHAR,                  -- '1234 Boul. Guillaume-Couture'
    postal_code VARCHAR,              -- 'G6V 4Z2'
    slug VARCHAR UNIQUE NOT NULL      -- 'superc-levis-couture'
);

CREATE SEQUENCE seq_targets START 1;
CREATE TABLE scrape_targets (
    id INTEGER DEFAULT nextval('seq_targets') PRIMARY KEY,
    product_id INTEGER REFERENCES products(id),
    store_id INTEGER REFERENCES stores(id),
    url VARCHAR NOT NULL,
    use_playwright BOOLEAN DEFAULT TRUE,
    active BOOLEAN DEFAULT TRUE,
    UNIQUE(product_id, store_id)
);

CREATE SEQUENCE seq_prices START 1;
CREATE TABLE prices (
    id INTEGER DEFAULT nextval('seq_prices') PRIMARY KEY,
    scrape_target_id INTEGER REFERENCES scrape_targets(id),
    date DATE NOT NULL,
    price DECIMAL(8,2),               -- NULL = price not found
    scraped_at TIMESTAMP DEFAULT current_timestamp,
    UNIQUE(scrape_target_id, date)    -- one price per target per day, upsert on re-run
);
```

**Migration**: Read existing `prix_fraises.csv` (4 rows), seed `products`, `store_chains`, `cities` tables, and insert the historical price rows. Include a `db.py:init_db()` function that creates tables if not exists + migrates CSV.

---

## Phase 4: Scraper Rewrite (Python + Playwright)

### 4a. `config/targets.yaml` — declarative scrape targets

```yaml
products:
  - name: "Fraises 454g"
    slug: "fraises-454g"

cities:
  - name: "Default"
    slug: "default"

stores:
  - chain: "Super C"
    city: "default"
    address: null            # address unknown for now
    postal_code: null
    slug: "superc-default"
  - chain: "Maxi"
    city: "default"
    address: null
    postal_code: null
    slug: "maxi-default"
  - chain: "Metro"
    city: "default"
    address: null
    postal_code: null
    slug: "metro-default"
  - chain: "IGA"
    city: "default"
    address: null
    postal_code: null
    slug: "iga-default"

targets:
  - product: "fraises-454g"
    store: "superc-default"    # references a specific store location
    url: "https://www.superc.ca/allees/fruits-et-legumes/..."
    use_playwright: true
    parser: "superc"
  - product: "fraises-454g"
    store: "maxi-default"
    url: "https://www.maxi.ca/fr/fraises-1-lb/..."
    use_playwright: true
    parser: "maxi"
  # ... etc
```

Adding new products/cities = adding YAML entries. No code changes needed.

### 4b. `scraper/browser.py` — Playwright helper

- Launch Chromium headless, fetch URL, wait for `networkidle`, return HTML
- Reuse a single browser instance across all targets in one run (faster)
- Graceful shutdown on error

### 4c. `scraper/parsers.py` — Price extraction

Port the R logic to Python:
- `price_from_jsonld(html)` — parse `<script type="application/ld+json">` for `offers.price`
- `price_from_css(html, selector)` — parse `.pi--prices` for French/English price formats
- Per-store functions: `parse_superc`, `parse_maxi`, `parse_metro`, `parse_iga`
- Use `beautifulsoup4` + `lxml` for HTML parsing, `json` for JSON-LD

### 4d. `scraper/main.py` — Entry point

- Load `targets.yaml`
- Sync targets to DuckDB (insert new products/stores/cities if needed)
- Launch Playwright browser once
- Loop through active targets, fetch HTML, extract price
- Upsert rows into `prices` table (replace today's row if re-run)
- Log results to stdout (cron captures to log file)
- For IGA (no Cloudflare): use `httpx` directly instead of Playwright (faster)

---

## Phase 5: Dashboard

### 5a. FastAPI Backend (`api/main.py`) — port 8000

Endpoints:
- `GET /api/products` → `[{id, name, slug}]`
- `GET /api/store-chains` → `[{id, name}]`
- `GET /api/cities` → `[{id, name, slug}]`
- `GET /api/stores` → `[{id, chain, city, address, slug}]`
- `GET /api/prices?product=fraises-454g&city=default&chain=Super+C&from=2026-01-01&to=2026-04-08` → `[{date, store_chain, city, address, price}]`
  - All filters optional; can filter by chain, city, specific store slug, or any combination

All queries hit DuckDB read-only. FastAPI serves the frontend static files from `/frontend/` at the root path.

### 5b. Static Frontend (`frontend/`)

- **index.html**: Clean single-page app — product multi-select, city multi-select, date range picker
- **app.js**: 
  - On load: fetch `/api/products`, `/api/cities`, `/api/store-chains` to populate dropdowns
  - On submit: fetch `/api/prices` with selected filters
  - Render **Plotly.js** line chart (x=date, y=price, one trace per store chain)
  - All chart rendering happens client-side (zero server compute for visualization)
- **style.css**: Simple responsive layout, works on mobile
- Use CDN-hosted Plotly.js (no build step needed)

### 5c. Key UI features
- Default view: all stores, latest product, last 30 days
- Multi-product comparison mode
- Responsive table below chart showing raw data
- French UI labels (target audience is Quebec)

---

## Phase 6: Deployment & Nginx

### 6a. DNS
- Add A record: `epicerie.proutgpt.com` → VM's public IP (user must do this in DNS provider)

### 6b. nginx config (`/etc/nginx/sites-available/epicerie`)

```nginx
# Rate limiting zones (add to http block in nginx.conf)
limit_req_zone $binary_remote_addr zone=epicerie_req:10m rate=10r/s;
limit_conn_zone $binary_remote_addr zone=epicerie_conn:10m;

server {
    listen 80;
    server_name epicerie.proutgpt.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name epicerie.proutgpt.com;

    ssl_certificate /etc/letsencrypt/live/epicerie.proutgpt.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/epicerie.proutgpt.com/privkey.pem;

    # Anti-abuse: rate limit + connection limit
    limit_req zone=epicerie_req burst=30 nodelay;
    limit_conn epicerie_conn 20;

    # API → FastAPI
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Static frontend
    location / {
        root /home/ubuntu/epicerie2/frontend;
        try_files $uri $uri/ /index.html;
    }
}
```

### 6c. SSL via certbot
```bash
sudo certbot --nginx -d epicerie.proutgpt.com
```

### 6d. systemd service (`epicerie-api.service`)
- Runs uvicorn on port 8000: `/home/ubuntu/epicerie2/venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000`
- User=ubuntu, WorkingDirectory=/home/ubuntu/epicerie2
- Restart=on-failure

---

## Phase 7: Cron Scheduling

```cron
# Scrape grocery prices daily at 8:00 AM Eastern
0 8 * * * cd /home/ubuntu/epicerie2 && /home/ubuntu/epicerie2/venv/bin/python -m scraper.main >> /home/ubuntu/epicerie2/logs/scrape.log 2>&1
```

Set timezone to `America/Toronto` if not already set (`sudo timedatectl set-timezone America/Toronto`).

---

## Relevant Files

**Existing (to keep/reference):**
- `/home/ubuntu/epicerie2/scrape_fraises.R` — original R scraper, port parsing logic from `price_from_jsonld()` and `price_from_css()` functions and STORES list
- `/home/ubuntu/epicerie2/prix_fraises.csv` — existing data to migrate (4 rows, 2026-04-08)
- `/etc/nginx/sites-enabled/proutgpt-api` — existing nginx config, reference for SSL + proxy pattern
- `/etc/systemd/system/proutgpt.service` — reference for systemd service pattern

**New files to create:**
- `/home/ubuntu/epicerie2/requirements.txt`
- `/home/ubuntu/epicerie2/scraper/__init__.py`, `main.py`, `browser.py`, `parsers.py`, `db.py`
- `/home/ubuntu/epicerie2/api/__init__.py`, `main.py`
- `/home/ubuntu/epicerie2/frontend/index.html`, `app.js`, `style.css`
- `/home/ubuntu/epicerie2/config/targets.yaml`
- `/etc/nginx/sites-available/epicerie` (nginx config)
- `/etc/systemd/system/epicerie-api.service` (systemd unit)

---

## Verification

1. **Scraper works**: Run `python -m scraper.main` manually, confirm prices are inserted into DuckDB — query `SELECT * FROM prices` with `duckdb` CLI or Python
2. **DuckDB has migrated data**: Verify the 4 existing CSV rows appear in the `prices` table
3. **API responds**: `curl http://127.0.0.1:8000/api/products` returns JSON list
4. **API prices endpoint**: `curl "http://127.0.0.1:8000/api/prices?product=fraises-454g"` returns price data
5. **Frontend loads**: Open `http://127.0.0.1:8000/` in browser, verify dropdowns populate and chart renders
6. **nginx proxying**: After DNS propagation, `curl https://epicerie.proutgpt.com/api/products` returns data
7. **Rate limiting works**: Rapid-fire requests return 429 after burst is exhausted: `for i in $(seq 1 50); do curl -s -o /dev/null -w "%{http_code}\n" https://epicerie.proutgpt.com/api/products; done`
8. **Cron runs**: Check `/home/ubuntu/epicerie2/logs/scrape.log` after scheduled time, or test with `crontab -l` and a near-future time
9. **Re-run idempotency**: Run scraper twice in one day, confirm no duplicate rows in `prices` table

---

## Decisions

- **Python over R**: Python 3.12 already installed; avoids installing R + renv on ARM64
- **Playwright over FlareSolverr**: Direct browser control, no separate service or Docker needed. Official ARM64 Chromium builds available.
- **DuckDB over PostGIS**: Embedded (single file), zero-config, excellent for analytical queries on small-medium datasets. No Docker or server process needed. If geospatial queries are ever needed, DuckDB has a `spatial` extension.
- **FastAPI + Plotly.js over Shiny/Streamlit**: Chart rendering happens client-side (offloads compute to user's browser as requested). FastAPI is lightweight and async.
- **nginx rate limiting for abuse protection**: `limit_req` (10 req/s with burst of 30) + `limit_conn` (max 20 concurrent per IP). No auth needed.
- **Cron over systemd timer**: Simpler, one-liner, standard approach for daily jobs.

---

## Further Considerations

1. **DNS setup required**: User must create an A record for `epicerie.proutgpt.com` pointing to the VM's public IP before SSL/nginx will work. Alternatively, can start with `api.proutgpt.com/epicerie` subpath if DNS change is delayed.
2. **Playwright memory usage**: Chromium headless uses ~200-400MB RAM per launch. With 24GB available, this is fine. The browser is launched once per scrape run, not kept resident.
3. **Future: adding products/cities**: Just add entries to `config/targets.yaml` with the product URL. The scraper auto-syncs new entries to DuckDB. May need new parser functions if store page layouts differ for non-berry products.
