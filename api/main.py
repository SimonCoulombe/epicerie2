"""FastAPI app — JSON API for the grocery price dashboard."""

from datetime import date
from pathlib import Path
from typing import Optional

import duckdb
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from scraper.db import get_connection, init_db, DB_PATH

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Épicerie Price Tracker", version="1.0.0")


@app.on_event("startup")
def startup():
    # Ensure DB + tables exist on API start
    if not DB_PATH.exists():
        init_db()


# ── API Endpoints ────────────────────────────────────────────────────────────


@app.get("/api/products")
def list_products():
    con = get_connection(read_only=True)
    try:
        rows = con.execute("SELECT id, name, slug FROM products ORDER BY name").fetchall()
        return [{"id": r[0], "name": r[1], "slug": r[2]} for r in rows]
    finally:
        con.close()


@app.get("/api/store-chains")
def list_store_chains():
    con = get_connection(read_only=True)
    try:
        rows = con.execute("SELECT id, name FROM store_chains ORDER BY name").fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]
    finally:
        con.close()


@app.get("/api/cities")
def list_cities():
    con = get_connection(read_only=True)
    try:
        rows = con.execute("SELECT id, name, slug FROM cities ORDER BY name").fetchall()
        return [{"id": r[0], "name": r[1], "slug": r[2]} for r in rows]
    finally:
        con.close()


@app.get("/api/stores")
def list_stores():
    con = get_connection(read_only=True)
    try:
        rows = con.execute("""
            SELECT s.id, sc.name AS chain, c.name AS city, s.address, s.slug
            FROM stores s
            JOIN store_chains sc ON s.store_chain_id = sc.id
            JOIN cities c ON s.city_id = c.id
            ORDER BY sc.name, c.name
        """).fetchall()
        return [
            {"id": r[0], "chain": r[1], "city": r[2], "address": r[3], "slug": r[4]}
            for r in rows
        ]
    finally:
        con.close()


@app.get("/api/prices")
def list_prices(
    product: Optional[str] = Query(None, description="Product slug filter"),
    city: Optional[str] = Query(None, description="City slug filter"),
    chain: Optional[str] = Query(None, description="Store chain name filter"),
    store: Optional[str] = Query(None, description="Store slug filter"),
    from_date: Optional[date] = Query(None, alias="from", description="Start date"),
    to_date: Optional[date] = Query(None, alias="to", description="End date"),
):
    con = get_connection(read_only=True)
    try:
        query = """
            SELECT
                pr.date,
                sc.name AS store_chain,
                c.name AS city,
                s.address,
                pr.price,
                p.name AS product_name,
                p.slug AS product_slug
            FROM prices pr
            JOIN scrape_targets st ON pr.scrape_target_id = st.id
            JOIN products p ON st.product_id = p.id
            JOIN stores s ON st.store_id = s.id
            JOIN store_chains sc ON s.store_chain_id = sc.id
            JOIN cities c ON s.city_id = c.id
            WHERE 1=1
        """
        params = []

        if product:
            query += " AND p.slug = ?"
            params.append(product)
        if city:
            query += " AND c.slug = ?"
            params.append(city)
        if chain:
            query += " AND sc.name = ?"
            params.append(chain)
        if store:
            query += " AND s.slug = ?"
            params.append(store)
        if from_date:
            query += " AND pr.date >= ?"
            params.append(from_date)
        if to_date:
            query += " AND pr.date <= ?"
            params.append(to_date)

        query += " ORDER BY pr.date, sc.name"

        rows = con.execute(query, params).fetchall()
        return [
            {
                "date": str(r[0]),
                "store_chain": r[1],
                "city": r[2],
                "address": r[3],
                "price": float(r[4]) if r[4] is not None else None,
                "product_name": r[5],
                "product_slug": r[6],
            }
            for r in rows
        ]
    finally:
        con.close()


# ── Static frontend serving ──────────────────────────────────────────────────

# Serve static assets (JS, CSS)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def serve_index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))
