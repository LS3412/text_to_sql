#!/usr/bin/env python3
"""
Create and seed the ClickHouse `table_metadata_catalog` (§3.5).

Run once after `docker-compose up`:
    python scripts/init_clickhouse.py

Without this, ClickHouseCatalog.get_relevant_tables() silently falls back to the
hard-coded core tables on every call.
"""

import sys

sys.path.insert(0, ".")

import clickhouse_connect

from config.settings import get_settings

COLUMNS = ["table_name", "column_name", "description", "db_source", "example_values"]

# (table_name, column_name, description, db_source, example_values)
CATALOG_ROWS = [
    ("stores", "store_id", "Unique identifier of the physical store", "sql", "1, 2, 3"),
    ("stores", "store_name", "The name of the retail store", "sql", "Store 118, Store 202"),
    ("stores", "district_id", "ID of the regional district", "sql", "1, 2, 3"),
    ("stores", "completion_rate", "Overall audit task completion percentage of the store", "sql", "95.50, 82.10"),
    ("active_tasks", "task_id", "Unique task identifier", "sql", "1, 2, 3"),
    ("active_tasks", "store_id", "ID of the store this audit task belongs to", "sql", "1, 2"),
    ("active_tasks", "task_name", "The name or description of the audit task", "sql", "Audit Inventory, Restock Items"),
    ("active_tasks", "status", "Current completion status of the audit task", "sql", "Pending, In Progress, Completed"),
]


def main() -> int:
    settings = get_settings()
    client = clickhouse_connect.get_client(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
    )

    client.command(
        """
        CREATE TABLE IF NOT EXISTS table_metadata_catalog (
            table_name     String,
            column_name    String,
            description    String,
            db_source      String,
            example_values String
        ) ENGINE = MergeTree()
        ORDER BY (db_source, table_name, column_name)
        """
    )

    # Idempotent reseed
    client.command("TRUNCATE TABLE table_metadata_catalog")
    client.insert("table_metadata_catalog", CATALOG_ROWS, column_names=COLUMNS)

    count = client.query("SELECT count() FROM table_metadata_catalog").result_rows[0][0]
    print(f"✓ Seeded {count} rows into table_metadata_catalog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
