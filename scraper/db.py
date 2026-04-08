"""DuckDB database operations — schema creation, CSV migration, read/write."""

import csv
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "epicerie.duckdb"
CSV_PATH = Path(__file__).resolve().parent.parent / "prix_fraises.csv"


def get_connection(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def init_db() -> None:
    """Create tables if they don't exist and migrate CSV data."""
    con = get_connection()
    try:
        _create_tables(con)
        _migrate_csv(con)
    finally:
        con.close()


def _create_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE SEQUENCE IF NOT EXISTS seq_products START 1;
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER DEFAULT nextval('seq_products') PRIMARY KEY,
            name VARCHAR NOT NULL,
            slug VARCHAR UNIQUE NOT NULL
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
            slug VARCHAR UNIQUE NOT NULL
        );

        CREATE SEQUENCE IF NOT EXISTS seq_targets START 1;
        CREATE TABLE IF NOT EXISTS scrape_targets (
            id INTEGER DEFAULT nextval('seq_targets') PRIMARY KEY,
            product_id INTEGER REFERENCES products(id),
            store_id INTEGER REFERENCES stores(id),
            url VARCHAR NOT NULL,
            use_playwright BOOLEAN DEFAULT TRUE,
            active BOOLEAN DEFAULT TRUE,
            UNIQUE(product_id, store_id)
        );

        CREATE SEQUENCE IF NOT EXISTS seq_prices START 1;
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER DEFAULT nextval('seq_prices') PRIMARY KEY,
            scrape_target_id INTEGER REFERENCES scrape_targets(id),
            date DATE NOT NULL,
            price DECIMAL(8,2),
            scraped_at TIMESTAMP DEFAULT current_timestamp,
            UNIQUE(scrape_target_id, date)
        );
    """)


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
    """Sync YAML config into DB tables (products, cities, store_chains, stores, scrape_targets)."""
    con = get_connection()
    try:
        _create_tables(con)

        # Insert products (skip existing)
        for p in config.get("products", []):
            con.execute("""
                INSERT INTO products (name, slug) VALUES (?, ?)
                ON CONFLICT (slug) DO NOTHING
            """, [p["name"], p["slug"]])

        # Insert cities (skip existing)
        for c in config.get("cities", []):
            con.execute("""
                INSERT INTO cities (name, slug) VALUES (?, ?)
                ON CONFLICT (slug) DO NOTHING
            """, [c["name"], c["slug"]])

        # Insert store chains (skip existing)
        chain_names = {s["chain"] for s in config.get("stores", [])}
        for name in chain_names:
            con.execute("""
                INSERT INTO store_chains (name) VALUES (?)
                ON CONFLICT (name) DO NOTHING
            """, [name])

        # Insert stores (skip existing)
        for s in config.get("stores", []):
            chain_id = con.execute(
                "SELECT id FROM store_chains WHERE name = ?", [s["chain"]]
            ).fetchone()[0]
            city_id = con.execute(
                "SELECT id FROM cities WHERE slug = ?", [s["city"]]
            ).fetchone()[0]
            con.execute("""
                INSERT INTO stores (store_chain_id, city_id, address, postal_code, slug)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (slug) DO NOTHING
            """, [chain_id, city_id, s.get("address"), s.get("postal_code"), s["slug"]])

        # Insert scrape targets (skip existing, update url/playwright on conflict)
        for t in config.get("targets", []):
            product_id = con.execute(
                "SELECT id FROM products WHERE slug = ?", [t["product"]]
            ).fetchone()[0]
            store_id = con.execute(
                "SELECT id FROM stores WHERE slug = ?", [t["store"]]
            ).fetchone()[0]
            existing = con.execute(
                "SELECT id FROM scrape_targets WHERE product_id = ? AND store_id = ?",
                [product_id, store_id]
            ).fetchone()
            if existing:
                con.execute("""
                    UPDATE scrape_targets SET url = ?, use_playwright = ?
                    WHERE id = ?
                """, [t["url"], t.get("use_playwright", True), existing[0]])
            else:
                con.execute("""
                    INSERT INTO scrape_targets (product_id, store_id, url, use_playwright, active)
                    VALUES (?, ?, ?, ?, TRUE)
                """, [product_id, store_id, t["url"], t.get("use_playwright", True)])

        # Now migrate CSV data (after targets exist)
        _migrate_csv(con)
    finally:
        con.close()


def upsert_price(scrape_target_id: int, row_date: date, price: float | None) -> None:
    """Insert or update a price for a given target and date."""
    con = get_connection()
    try:
        con.execute("""
            INSERT INTO prices (scrape_target_id, date, price, scraped_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (scrape_target_id, date) DO UPDATE SET
                price = excluded.price,
                scraped_at = excluded.scraped_at
        """, [scrape_target_id, row_date, price, datetime.now()])
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
                st.use_playwright
            FROM scrape_targets st
            JOIN products p ON st.product_id = p.id
            JOIN stores s ON st.store_id = s.id
            JOIN store_chains sc ON s.store_chain_id = sc.id
            JOIN cities c ON s.city_id = c.id
            WHERE st.active = TRUE
        """).fetchall()
        columns = ["target_id", "product_slug", "product_name", "chain_name",
                    "city_slug", "store_slug", "url", "use_playwright"]
        return [dict(zip(columns, row)) for row in rows]
    finally:
        con.close()
