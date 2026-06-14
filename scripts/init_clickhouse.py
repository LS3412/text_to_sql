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
from src.pipeline.semantic_layer import SemanticLayer

COLUMNS = ["table_name", "column_name", "description", "db_source", "example_values"]


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

    # Project the semantic dictionary (single source of truth) into the catalog:
    # column-level rows for keyword search + one table-level summary row per table.
    semantic = SemanticLayer.load()
    rows = semantic.to_catalog_rows() + semantic.table_summary_rows()

    # Idempotent reseed
    client.command("TRUNCATE TABLE table_metadata_catalog")
    client.insert("table_metadata_catalog", rows, column_names=COLUMNS)

    count = client.query("SELECT count() FROM table_metadata_catalog").result_rows[0][0]
    print(f"✓ Seeded {count} rows into table_metadata_catalog (from config/semantic/dictionary.yaml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())