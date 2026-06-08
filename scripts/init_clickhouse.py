#!/usr/bin/env python3
"""
Create and seed the ClickHouse metadata catalog as a MATERIALIZED VIEW (§3.5).

Design (raw -> materialized view -> aggregated target):

    table_metadata_catalog_raw   MergeTree, one row per (table, column).
                                 This is the ingest point — to onboard thousands
                                 of tables you just INSERT more rows here.
        │  (MV fires on every insert)
        ▼
    table_metadata_catalog_mv    MATERIALIZED VIEW that, per (db_source, table),
                                 rolls every column's name+description+examples
                                 into a single searchable token blob.
        │
        ▼
    table_metadata_catalog_agg   AggregatingMergeTree target holding the
                                 groupArray *state*. The catalog lookup reads
                                 this with groupArrayMerge (see clickhouse_catalog.py).

Because it's a real MV, adding tables = inserting into the raw table and the
aggregated catalog updates itself — no rebuild step. The order below matters:
the MV must exist BEFORE the raw insert, or the target stays empty.

Run once after `docker-compose up`:
    python scripts/init_clickhouse.py
"""

import sys

sys.path.insert(0, ".")

import clickhouse_connect

from config.settings import get_settings

COLUMNS = ["table_name", "column_name", "description", "db_source", "example_values"]

# (table_name, column_name, description, db_source, example_values)
# Descriptions are intentionally keyword-rich ("overdue", "due date", "district",
# "priority", "completion", "late") so the hasToken() lookup surfaces the right
# tables for the spec's target questions.
CATALOG_ROWS = [
    # districts
    ("districts", "district_id", "Unique identifier of the regional district", "sql", "1, 2, 3"),
    ("districts", "district_name", "Name of the district or region grouping stores", "sql", "North District, South District"),
    ("districts", "region", "Geographic region or cluster the district belongs to", "sql", "West Region, East Region"),
    # stores
    ("stores", "store_id", "Unique identifier of the physical store", "sql", "1, 2, 3"),
    ("stores", "store_name", "The name of the retail store", "sql", "Store 118, Store 202"),
    ("stores", "district_id", "ID of the regional district the store belongs to", "sql", "1, 2, 3"),
    ("stores", "completion_rate", "Overall audit task completion percentage of the store", "sql", "95.50, 82.10"),
    # users
    ("users", "user_id", "Unique identifier of the field user or manager", "sql", "1, 2, 3"),
    ("users", "user_name", "Display name of the store or district manager user", "sql", "Alice Stone, Bob Rivera"),
    ("users", "role", "User role such as Store Manager or District Manager", "sql", "Store Manager, District Manager"),
    ("users", "district_id", "ID of the district this user manages or belongs to", "sql", "1, 2"),
    # active_tasks
    ("active_tasks", "task_id", "Unique task identifier", "sql", "1, 2, 3"),
    ("active_tasks", "store_id", "ID of the store this audit task belongs to", "sql", "1, 2"),
    ("active_tasks", "task_name", "The name or description of the audit task", "sql", "Audit Inventory, Store Walk"),
    ("active_tasks", "status", "Current completion status of the audit task", "sql", "Pending, In Progress, Completed"),
    ("active_tasks", "priority", "Task priority level for prioritisation", "sql", "Low, Medium, High"),
    ("active_tasks", "project_type", "Kind of project or task category", "sql", "Store Walk, Inventory, Reset"),
    ("active_tasks", "assigned_user_id", "ID of the user the task is assigned to", "sql", "1, 2"),
    ("active_tasks", "created_at", "Timestamp when the task was created", "sql", "2026-06-01 09:00:00"),
    ("active_tasks", "due_date", "Deadline by which the task is due; drives overdue and at-risk detection", "sql", "2026-06-08 17:00:00"),
    ("active_tasks", "completed_at", "Timestamp the task was actually completed; used to detect late completion", "sql", "2026-06-07 12:00:00"),
]


def main() -> int:
    settings = get_settings()
    client = clickhouse_connect.get_client(
        host=settings.clickhouse.host,
        port=settings.clickhouse.port,
        username=settings.clickhouse.user,
        password=settings.clickhouse.password,
    )

    # 1. Idempotent teardown (drop the MV before its source/target). Also drop the
    #    legacy flat catalog table from the previous (non-MV) design.
    for stmt in (
        "DROP VIEW IF EXISTS table_metadata_catalog_mv",
        "DROP TABLE IF EXISTS table_metadata_catalog_agg",
        "DROP TABLE IF EXISTS table_metadata_catalog_raw",
        "DROP TABLE IF EXISTS table_metadata_catalog",
    ):
        client.command(stmt)

    # 2. Raw ingest table — one row per column.
    client.command(
        """
        CREATE TABLE table_metadata_catalog_raw (
            table_name     String,
            column_name    String,
            description    String,
            db_source      String,
            example_values String
        ) ENGINE = MergeTree()
        ORDER BY (db_source, table_name, column_name)
        """
    )

    # 3. Aggregated target — one row per (db_source, table_name) holding the
    #    groupArray *state* of per-column searchable token strings.
    client.command(
        """
        CREATE TABLE table_metadata_catalog_agg (
            db_source           String,
            table_name          String,
            search_tokens_state AggregateFunction(groupArray, String)
        ) ENGINE = AggregatingMergeTree()
        ORDER BY (db_source, table_name)
        """
    )

    # 4. Materialized view: on every insert into _raw, roll each column into a
    #    lower-cased "table column description examples" token string and feed the
    #    groupArrayState into the aggregated target.
    client.command(
        """
        CREATE MATERIALIZED VIEW table_metadata_catalog_mv
        TO table_metadata_catalog_agg AS
        SELECT
            db_source,
            table_name,
            groupArrayState(
                lower(concatWithSeparator(' ', table_name, column_name, description, example_values))
            ) AS search_tokens_state
        FROM table_metadata_catalog_raw
        GROUP BY db_source, table_name
        """
    )

    # 5. Seed the raw table (MV now exists, so the target is populated automatically).
    client.insert("table_metadata_catalog_raw", CATALOG_ROWS, column_names=COLUMNS)

    raw_count = client.query("SELECT count() FROM table_metadata_catalog_raw").result_rows[0][0]
    table_count = client.query(
        "SELECT uniqExact(table_name) FROM table_metadata_catalog_agg"
    ).result_rows[0][0]
    print(
        f"✓ Seeded {raw_count} column rows; materialized view aggregated "
        f"{table_count} tables into table_metadata_catalog_agg"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
