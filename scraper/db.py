"""DuckDB database operations — schema creation, CSV migration, read/write."""

import csv
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "epicerie.duckdb"
CSV_PATH = Path(__file__).resolve().parent.parent / "prix_fraises.csv"
PRODUCTS_CSV = Path(__file__).resolve().parent.parent / "config" / "products.csv"
TARGETS_CSV = Path(__file__).resolve().parent.parent / "config" / "targets.csv"


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def init_db() -> None:
    """Create tables if they don't exist, run schema migrations, and migrate CSV data."""
    con = get_connection()
    try:
        _create_tables(con)
        _migrate_schema(con)
        _migrate_csv(con)
    finally:
        con.close()


def _create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_products START 1;
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER DEFAULT nextval('seq_products') PRIMARY KEY,
            name VARCHAR NOT NULL,
            slug VARCHAR UNIQUE NOT NULL,
            category VARCHAR,
            cpi_name VARCHAR,
            unit VARCHAR
        );

        CREATE SEQUENCE IF NOT EXISTS seq_store_chains START 1;
        CREATE TABLE IF NOT EXISTS store_chains (
            id INTEGER DEFAULT nextval('seq_store_chains') PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE
        );

        CREATE SEQUENCE IF NOT EXISTS seq_cities START 1;
        CREATE TABLE IF NOT EXISTS cities (
            id INTEGER DEFAULT nextval('seq_cities') PRIMARY KEY,
            name VARCHAR NOT NULL UNIQUE,
            slug VARCHAR UNIQUE NOT NULL
        );

        CREATE SEQUENCE IF NOT EXISTS seq_stores START 1;
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER DEFAULT nextval('seq_stores') PRIMARY KEY,
            store_chain_id INTEGER REFERENCES store_chains(id),
            city_id INTEGER REFERENCES cities(id),
            address VARCHAR,
            postal_code VARCHAR,
            slug VARCHAR UNIQUE NOT NULL,
            chain_store_id VARCHAR,
            store_name VARCHAR
        );

        CREATE SEQUENCE IF NOT EXISTS seq_targets START 1;
        CREATE TABLE IF NOT EXISTS scrape_targets (
            id INTEGER DEFAULT nextval('seq_targets') PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            store_id INTEGER REFERENCES stores(id),
            url VARCHAR NOT NULL,
            use_playwright BOOLEAN DEFAULT TRUE,
            active BOOLEAN DEFAULT TRUE,
            parser VARCHAR DEFAULT 'auto',
            last_success DATE,
            fail_count INTEGER DEFAULT 0,
            product_title VARCHAR,
            UNIQUE(product_id, store_id)
        );

        CREATE SEQUENCE IF NOT EXISTS seq_prices START 1;
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER DEFAULT nextval('seq_prices') PRIMARY KEY,
            scrape_target_id INTEGER REFERENCES scrape_targets(id),
            date DATE NOT NULL,
            price DECIMAL(8,2),
            price_unit VARCHAR DEFAULT 'each',
            price_per_kg DECIMAL(8,2),
            scraped_at TIMESTAMP DEFAULT current_timestamp,
            UNIQUE(scrape_target_id, date)
        );
    """)


def _migrate_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Add new columns to existing tables (idempotent for existing DBs)."""
    migrations = [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS category VARCHAR",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS cpi_name VARCHAR",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit VARCHAR",
        "ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS parser VARCHAR DEFAULT 'auto'",
        "ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS last_success DATE",
        "ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS fail_count INTEGER DEFAULT 0",
        "ALTER TABLE prices ADD COLUMN IF NOT EXISTS price_unit VARCHAR DEFAULT 'each'",
        "ALTER TABLE prices ADD COLUMN IF NOT EXISTS price_per_kg DECIMAL(8,2)",
        "ALTER TABLE scrape_targets ADD COLUMN IF NOT EXISTS product_title VARCHAR",
        "ALTER TABLE stores ADD COLUMN IF NOT EXISTS chain_store_id VARCHAR",
        "ALTER TABLE stores ADD COLUMN IF NOT EXISTS store_name VARCHAR",
    ]
    for sql in migrations:
        try:
            con.execute(sql)
        except duckdb.CatalogException:
            pass  # column already exists


# Mapping from CSV store names to chain/slug used in the YAML config
_CSV_STORE_MAP = {
    "Super C": ("Super C", "superc-default"),
    "Maxi": ("Maxi", "maxi-default"),
    "Metro": ("Metro", "metro-default"),
    "IGA": ("IGA", "iga-default"),
}


def _migrate_csv(con: duckdb.DuckDBPyConnection) -> None:
    """Import prix_fraises.csv rows that aren't already in the DB."""
    if not CSV_PATH.exists():
        return

    # Check if we already migrated (any price rows exist)
    existing_count = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    if existing_count > 0:
        return

    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            store_name = row["store"]
            if store_name not in _CSV_STORE_MAP:
                continue
            chain_name, store_slug = _CSV_STORE_MAP[store_name]

            price_val = row.get("price", "")
            price = Decimal(price_val) if price_val and price_val != "NA" else None
            row_date = row["date"]

            # Get scrape_target_id via store slug + fraises product
            target_id = con.execute("""
                SELECT st.id FROM scrape_targets st
                JOIN stores s ON st.store_id = s.id
                JOIN products p ON st.product_id = p.id
                WHERE s.slug = ? AND p.slug = 'fraises-454g'
            """, [store_slug]).fetchone()

            if target_id is None:
                continue

            con.execute("""
                INSERT INTO prices (scrape_target_id, date, price, scraped_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (scrape_target_id, date) DO UPDATE SET price = excluded.price
            """, [target_id[0], row_date, float(price) if price else None, datetime.now()])


def sync_targets(config: dict) -> None:
    """Sync config into DB tables.

    - Products come from config/products.csv (with category, cpi_name, unit).
    - Scrape targets come from config/targets.csv.
    - Stores, cities, and store_chains come from the YAML config dict.
    """
    con = get_connection()
    try:
        _create_tables(con)
        _migrate_schema(con)

        # ── Products from CSV ──────────────────────────────────────────
        if PRODUCTS_CSV.exists():
            with open(PRODUCTS_CSV, newline="", encoding="utf-8") as f:
                for p in csv.DictReader(f):
                    existing = con.execute(
                        "SELECT id FROM products WHERE slug = ?", [p["slug"]]
                    ).fetchone()
                    if existing:
                        con.execute("""
                            UPDATE products
                            SET name = ?, category = ?, cpi_name = ?, unit = ?
                            WHERE slug = ?
                        """, [p["name"], p.get("category"), p.get("cpi_name"),
                              p.get("unit"), p["slug"]])
                    else:
                        con.execute("""
                            INSERT INTO products (name, slug, category, cpi_name, unit)
                            VALUES (?, ?, ?, ?, ?)
                        """, [p["name"], p["slug"], p.get("category"),
                              p.get("cpi_name"), p.get("unit")])

        # ── Cities from YAML ──────────────────────────────────────────
        for c in config.get("cities", []):
            con.execute("""
                INSERT INTO cities (name, slug) VALUES (?, ?)
                ON CONFLICT (slug) DO NOTHING
            """, [c["name"], c["slug"]])

        # ── Store chains from YAML ────────────────────────────────────
        chain_names = {s["chain"] for s in config.get("stores", [])}
        for name in chain_names:
            con.execute("""
                INSERT INTO store_chains (name) VALUES (?)
                ON CONFLICT (name) DO NOTHING
            """, [name])

        # ── Stores from YAML ─────────────────────────────────────────
        for s in config.get("stores", []):
            chain_id = con.execute(
                "SELECT id FROM store_chains WHERE name = ?", [s["chain"]]
            ).fetchone()[0]
            city_id = con.execute(
                "SELECT id FROM cities WHERE slug = ?", [s["city"]]
            ).fetchone()[0]
            existing = con.execute(
                "SELECT id FROM stores WHERE slug = ?", [s["slug"]]
            ).fetchone()
            if existing:
                # DuckDB can't UPDATE FK-referenced rows; skip if already exists
                # City/chain fixes must be done via direct SQL or DB rebuild
                pass
            else:
                con.execute("""
                    INSERT INTO stores (store_chain_id, city_id, address, postal_code,
                                        slug, chain_store_id, store_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, [chain_id, city_id, s.get("address"), s.get("postal_code"),
                      s["slug"], s.get("chain_store_id"), s.get("store_name")])

        # ── Scrape targets from CSV ───────────────────────────────────
        if TARGETS_CSV.exists():
            with open(TARGETS_CSV, newline="", encoding="utf-8") as f:
                for t in csv.DictReader(f):
                    product_row = con.execute(
                        "SELECT id FROM products WHERE slug = ?", [t["product_slug"]]
                    ).fetchone()
                    store_row = con.execute(
                        "SELECT id FROM stores WHERE slug = ?", [t["store_slug"]]
                    ).fetchone()
                    if product_row is None or store_row is None:
                        continue
                    product_id, store_id = product_row[0], store_row[0]
                    use_pw = t.get("use_playwright", "true").lower() == "true"
                    parser = t.get("parser", "auto")

                    existing = con.execute(
                        "SELECT id FROM scrape_targets WHERE product_id = ? AND store_id = ?",
                        [product_id, store_id]
                    ).fetchone()
                    if existing:
                        con.execute("""
                            UPDATE scrape_targets SET url = ?, use_playwright = ?, parser = ?
                            WHERE id = ?
                        """, [t["url"], use_pw, parser, existing[0]])
                    else:
                        con.execute("""
                            INSERT INTO scrape_targets (product_id, store_id, url, use_playwright, active, parser)
                            VALUES (?, ?, ?, ?, TRUE, ?)
                        """, [product_id, store_id, t["url"], use_pw, parser])

        # Now migrate CSV data (after targets exist)
        _migrate_csv(con)
    finally:
        con.close()


def upsert_price(scrape_target_id: int, row_date: date, price: float | None,
                 price_unit: str = "each", price_per_kg: float | None = None) -> None:
    """Insert or update a price for a given target and date."""
    con = get_connection()
    try:
        con.execute("""
            INSERT INTO prices (scrape_target_id, date, price, price_unit, price_per_kg, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (scrape_target_id, date) DO UPDATE SET
                price = excluded.price,
                price_unit = excluded.price_unit,
                price_per_kg = excluded.price_per_kg,
                scraped_at = excluded.scraped_at
        """, [scrape_target_id, row_date, price, price_unit, price_per_kg, datetime.now()])
    finally:
        con.close()


def get_active_targets() -> list[dict]:
    """Return active scrape targets with all joined info."""
    con = get_connection(read_only=True)
    try:
        rows = con.execute("""
            SELECT
                st.id AS target_id,
                p.slug AS product_slug,
                p.name AS product_name,
                sc.name AS chain_name,
                c.slug AS city_slug,
                s.slug AS store_slug,
                st.url,
                st.use_playwright,
                st.parser,
                s.chain_store_id
            FROM scrape_targets st
            JOIN products p ON st.product_id = p.id
            JOIN stores s ON st.store_id = s.id
            JOIN store_chains sc ON s.store_chain_id = sc.id
            JOIN cities c ON s.city_id = c.id
            WHERE st.active = TRUE
        """).fetchall()
        columns = ["target_id", "product_slug", "product_name", "chain_name",
                    "city_slug", "store_slug", "url", "use_playwright", "parser",
                    "chain_store_id"]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        con.close()


def update_target_status(target_id: int, success: bool,
                         product_title: str | None = None) -> None:
    """Update last_success/fail_count (and optionally product_title) after a scrape."""
    con = get_connection()
    try:
        if success:
            if product_title:
                con.execute("""
                    UPDATE scrape_targets
                    SET last_success = current_date, fail_count = 0, product_title = ?
                    WHERE id = ?
                """, [product_title, target_id])
            else:
                con.execute("""
                    UPDATE scrape_targets
                    SET last_success = current_date, fail_count = 0
                    WHERE id = ?
                """, [target_id])
        else:
            con.execute("""
                UPDATE scrape_targets
                SET fail_count = COALESCE(fail_count, 0) + 1
                WHERE id = ?
            """, [target_id])
    finally:
        con.close()
