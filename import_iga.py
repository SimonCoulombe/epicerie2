#!/usr/bin/env python3
"""Read IGA price JSON from stdin and upsert into DuckDB.
Called by the Pi: python3 scrape_iga.py | ssh oracle 'cd ~/epicerie2 && venv/bin/python import_iga.py'
"""

import json
import sys
from datetime import date

sys.path.insert(0, "/home/ubuntu/epicerie2")
from scraper.db import get_connection, upsert_price, update_target_status


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Failed to parse JSON from stdin: {e}", file=sys.stderr)
        sys.exit(1)

    if not data:
        print("No data received — nothing to import.", file=sys.stderr)
        sys.exit(0)

    con = get_connection(read_only=True)
    target_map = {}
    rows = con.execute("""
        SELECT p.slug, s.slug, st.id
        FROM scrape_targets st
        JOIN products p ON st.product_id = p.id
        JOIN stores s ON st.store_id = s.id
    """).fetchall()
    con.close()
    for product_slug, store_slug, target_id in rows:
        target_map[(product_slug, store_slug)] = target_id

    ok = 0
    for entry in data:
        key = (entry["product_slug"], entry["store_slug"])
        target_id = target_map.get(key)
        if target_id is None:
            print(f"No target for {key}", file=sys.stderr)
            continue

        row_date = date.fromisoformat(entry["date"])
        upsert_price(
            target_id, row_date,
            entry["price"],
            price_unit=entry.get("unit", "each"),
            price_per_kg=entry.get("price_per_kg"),
        )
        update_target_status(target_id, success=True)
        kg_str = f", {entry['price_per_kg']:.2f}$/kg" if entry.get("price_per_kg") else ""
        print(f"  [IGA] {entry['product_slug']}: {entry['price']:.2f} $ ({entry.get('unit', 'each')}{kg_str})")
        ok += 1

    print(f"Imported {ok}/{len(data)} IGA prices for {data[0]['date'] if data else '?'}.")


if __name__ == "__main__":
    main()
