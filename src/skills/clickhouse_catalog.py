"""
ClickHouse metadata catalog lookup (§3.5).

Queries the `table_metadata_catalog` table (created/seeded by
scripts/init_clickhouse.py) so the LLM only ever sees the handful of tables
relevant to a question. Falls back to the core tables if ClickHouse is
unavailable or the catalog is empty.
"""

import re
import clickhouse_connect
from config.settings import get_settings

_FALLBACK_TABLES = ["stores", "active_tasks"]


class ClickHouseCatalog:
    def __init__(self):
        # Dynamically load config values from .env
        self.settings = get_settings()
        self.client = clickhouse_connect.get_client(
            host=self.settings.clickhouse.host,
            port=self.settings.clickhouse.port,
            username=self.settings.clickhouse.user,
            password=self.settings.clickhouse.password,
        )

    def get_relevant_tables(self, user_question: str, limit: int = 4) -> list[str]:
        # Sanitize keywords to alphanumeric tokens — hasToken() rejects tokens
        # containing separators, and this also prevents query injection.
        raw = (re.sub(r"[^a-z0-9]", "", word.lower()) for word in user_question.split())
        keywords = [kw for kw in raw if len(kw) > 3]
        if not keywords:
            return list(_FALLBACK_TABLES)

        term_conditions = " OR ".join(
            f"hasToken(lower(description), '{kw}') OR hasToken(lower(table_name), '{kw}')"
            for kw in keywords
        )
        query = f"""
            SELECT DISTINCT table_name
            FROM table_metadata_catalog
            WHERE db_source = 'sql' AND ({term_conditions})
            LIMIT {limit}
        """
        try:
            result = self.client.query(query)
            tables = [row[0] for row in result.result_rows]
            return tables if tables else list(_FALLBACK_TABLES)
        except Exception:
            return list(_FALLBACK_TABLES)