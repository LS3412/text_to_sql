"""
ClickHouse metadata catalog lookup (§3.5).

Queries the `table_metadata_catalog_agg` aggregated target of the catalog
MATERIALIZED VIEW (created/seeded by scripts/init_clickhouse.py) so the LLM only
ever sees the handful of tables relevant to a question — this is what keeps
prompts small when there are thousands of tables. Falls back to the core tables
if ClickHouse is unavailable or the catalog is empty.
"""

import re
import clickhouse_connect
from config.settings import get_settings

_FALLBACK_TABLES = ["stores", "active_tasks", "districts", "users"]


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
            f"hasToken(search_text, '{kw}') OR hasToken(table_name, '{kw}')"
            for kw in keywords
        )
        # The target is an AggregatingMergeTree, so we must merge the groupArray
        # state (groupArrayMerge) and flatten it to one searchable string before
        # matching. The GROUP BY collapses partially-merged parts deterministically.
        query = f"""
            SELECT table_name FROM (
                SELECT
                    table_name,
                    arrayStringConcat(groupArrayMerge(search_tokens_state), ' ') AS search_text
                FROM table_metadata_catalog_agg
                WHERE db_source = 'sql'
                GROUP BY db_source, table_name
            )
            WHERE {term_conditions}
            LIMIT {limit}
        """
        try:
            result = self.client.query(query)
            tables = [row[0] for row in result.result_rows]
            return tables if tables else list(_FALLBACK_TABLES)
        except Exception:
            return list(_FALLBACK_TABLES)
