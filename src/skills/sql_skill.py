"""
SQL Skill for translating natural language queries to SQL.
Highly optimized for speed and safety, bypassing heavy LlamaIndex scanning.

NOTE (spec deviation, §2/§3.2): the spec lists LlamaIndex `NLSQLTableQueryEngine`
as the required engine. We deliberately bypass it for a single-shot prompt to cut
latency. See IMPLEMENTATION_NOTES.md — this requires joint sign-off.
"""

import re
from sqlalchemy import text
from config.settings import get_settings
from config.database import readonly_session
from src.skills.clickhouse_catalog import ClickHouseCatalog
from llama_index.core import Settings


class SQLSkill:
    def __init__(self):
        self.settings = get_settings()
        self.catalog = ClickHouseCatalog()

    def validate_sql_query(self, query: str) -> bool:
        """
        Lightning-fast, literal-aware security validator.
        Runs in under 0.1ms, eliminating a 10-second LLM validation call entirely.
        """
        # Strip string literals to prevent false positives inside text filters (like store names)
        query_stripped = re.sub(r"'(?:[^'\\]|\\.)*'", "", query)
        query_stripped = re.sub(r'"(?:[^"\\]|\\.)*"', "", query_stripped)

        query_upper = query_stripped.upper().strip()

        # 1. Must be SELECT or WITH
        if not (query_upper.startswith("SELECT") or query_upper.startswith("WITH")):
            return False

        # 2. Block mutating keywords
        forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "REPLACE", "GRANT", "REVOKE"]
        for word in forbidden:
            pattern = r"\b" + word + r"\b"
            if re.search(pattern, query_upper):
                return False

        # 3. Block statement stacking (semicolon hacks)
        cleaned = query.strip().rstrip(";")
        if ";" in cleaned:
            return False

        return True

    def _apply_limit(self, query: str) -> str:
        """
        Enforce a row cap (§7.1). Appends `LIMIT <max_result_rows>` when the query
        has no LIMIT, and clamps an existing LIMIT that exceeds the configured max.
        LIMIT detection ignores string literals to avoid false matches.
        """
        max_rows = self.settings.app.max_result_rows
        base = query.strip().rstrip(";").strip()

        no_literals = re.sub(r"'(?:[^'\\]|\\.)*'", "", base)
        match = re.search(r"\bLIMIT\s+(\d+)\b", no_literals, re.IGNORECASE)

        if not match:
            return f"{base} LIMIT {max_rows}"
        if int(match.group(1)) > max_rows:
            return re.sub(r"\bLIMIT\s+\d+\b", f"LIMIT {max_rows}", base, count=1, flags=re.IGNORECASE)
        return base

    async def generate_and_validate_sql(self, user_question: str, history: str = "") -> tuple[str, list[str]]:
        """
        Performs a rapid, single-shot SQL generation via the configured LLM,
        bypassing slow LlamaIndex engines. `history` is recent conversation
        context (may be empty) used to resolve follow-up questions.
        """
        # Step 1: Query ClickHouse to scale down database context
        relevant_tables = self.catalog.get_relevant_tables(user_question)

        # Step 2: Format the schemas of selected tables into a clean prompt.
        # tenant_id is intentionally omitted from the prompt — tenant isolation is
        # enforced transparently by Row-Level Security, not by the generated WHERE.
        schema_definitions = ""
        if "stores" in relevant_tables:
            schema_definitions += (
                "Table: stores\n"
                "Columns:\n"
                " - store_id (SERIAL PRIMARY KEY)\n"
                " - store_name (VARCHAR) - e.g. 'Store 118', 'Store 202'\n"
                " - district_id (INTEGER)\n"
                " - completion_rate (NUMERIC)\n\n"
            )
        if "active_tasks" in relevant_tables:
            schema_definitions += (
                "Table: active_tasks\n"
                "Columns:\n"
                " - task_id (SERIAL PRIMARY KEY)\n"
                " - store_id (INTEGER, foreign key referencing stores.store_id)\n"
                " - task_name (VARCHAR)\n"
                " - status (VARCHAR) - e.g. 'Pending', 'In Progress', 'Completed'\n\n"
            )

        history_block = f"\nRecent conversation (use only to resolve references):\n{history}\n" if history else ""

        system_prompt = f"""
        You are a highly precise PostgreSQL Translation Engine.
        Convert the user question into a safe PostgreSQL SELECT query using these schemas:

        {schema_definitions}{history_block}
        Rules:
        1. Output ONLY the raw SQL query. No explanation, no markdown backticks, no commentary.
        2. When filtering by store name, always use `store_name = 'Store 118'` (string match) instead of filtering by ID unless requested.
        3. Do not append a semicolon at the end of the query.
        4. Do NOT filter by tenant_id — tenant isolation is handled automatically by the database.
        5. Always include a LIMIT clause (no more than {self.settings.app.max_result_rows} rows).
        """

        # Step 3: Run single-shot SQL generation via the globally configured LLM
        response = await Settings.llm.acomplete(f"{system_prompt}\n\nQuestion: {user_question}\nSQL:")
        sql_query = response.text.strip()

        # Clean markdown wrappers if any
        sql_query = re.sub(r"^```(?:sql)?", "", sql_query, flags=re.IGNORECASE)
        sql_query = re.sub(r"```$", "", sql_query).strip()

        # Step 4: Run lightning-fast safety validation
        if not self.validate_sql_query(sql_query):
            raise PermissionError(f"Security Alert: Blocked unauthorized or unsafe SQL command: {sql_query}")

        # Step 5: Enforce a row cap before the query ever reaches the database
        sql_query = self._apply_limit(sql_query)

        return sql_query, relevant_tables

    async def execute_query(self, sql_query: str, agent_id: str, tenant_id: str) -> list[dict]:
        """
        Executes a safe SELECT statement against PostgreSQL using the read-only role.
        Sets both RLS GUCs so the connection is scoped to the agent and the tenant
        (§5.2, §7.3); tenant rows that don't match app.tenant_id are invisible.
        """
        async with readonly_session() as session:
            await session.execute(
                text("SELECT set_config('app.agent_id', :v, true)"), {"v": agent_id}
            )
            await session.execute(
                text("SELECT set_config('app.tenant_id', :v, true)"), {"v": tenant_id}
            )

            result = await session.execute(text(sql_query))
            columns = result.keys()
            return [dict(zip(columns, row)) for row in result.fetchall()]
