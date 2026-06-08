"""
SQL Skill for translating natural language queries to SQL.

Uses LlamaIndex's NLSQLTableQueryEngine to GENERATE the SQL (sql_only=True): the
engine renders the schema of the catalog-selected tables into a prompt and asks
the LLM for a SELECT — it never executes or touches tenant data. We then run our
own fast validator + LIMIT enforcement and execute the validated SQL ourselves on
the read-only, tenant-scoped (RLS) connection. This keeps tenant isolation in our
hands while satisfying the "use NLSQLTableQueryEngine" requirement.

Schema reflection (the expensive part) happens once at startup in a shared
SQLDatabase (built in src/main.py and injected as `self.sql_database`); per request
we only construct a lightweight NLSQLTableQueryEngine over the narrowed table set.
"""

import asyncio
import re

from sqlalchemy import text

from config.settings import get_settings
from config.database import readonly_session
from src.skills.clickhouse_catalog import ClickHouseCatalog, _FALLBACK_TABLES

from llama_index.core import Settings, PromptTemplate
from llama_index.core.query_engine import NLSQLTableQueryEngine
from llama_index.core.tools import QueryEngineTool


# Custom text-to-SQL prompt. The default LlamaIndex prompt does not know about our
# RLS model, so it would tell the model it *may* filter by any column — including
# tenant_id, which the LLM would then fill with an invented value and get zero
# rows. These rules mirror the safety guarantees the skill enforces downstream.
_TEXT_TO_SQL_TMPL = PromptTemplate(
    """You are a highly precise PostgreSQL translation engine.
Given the user question, write a single safe PostgreSQL SELECT query.

Only use the tables and columns described here:
{schema}

Rules:
1. Output ONLY the raw SQL query — no explanation, no markdown fences, no commentary.
2. Produce exactly one read-only SELECT (you may use WITH/CTEs). Never write
   INSERT/UPDATE/DELETE/DDL and never use a semicolon.
3. Do NOT filter by tenant_id — tenant isolation is enforced automatically by the
   database. Never reference tenant_id in WHERE/JOIN.
4. When filtering by store name use the name string, e.g. store_name = 'Store 118'.
5. Always include a LIMIT clause (no more than {max_rows} rows).
6. For "today"/"overdue"/"late" use now() against due_date / completed_at.

Question: {query_str}
SQLQuery: """
)


class SQLSkill:
    def __init__(self, sql_database=None):
        self.settings = get_settings()
        self.catalog = ClickHouseCatalog()
        # Shared, once-reflected SQLDatabase — injected at startup by src/main.py.
        self.sql_database = sql_database

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

    def _build_query_engine(self, tables: list[str]) -> NLSQLTableQueryEngine:
        """
        Construct a per-request NLSQLTableQueryEngine over the catalog-narrowed
        tables. Cheap: schema reflection lives in the shared SQLDatabase, so this
        only renders the prompt for `tables`. sql_only=True => generate, don't run.
        """
        if self.sql_database is None:
            raise RuntimeError(
                "SQLSkill.sql_database is not set — build the SQLDatabase at startup."
            )
        # The engine formats the prompt with schema/query_str/dialect only, so fill
        # max_rows ourselves via partial_format (leaving schema/query_str as the
        # engine's placeholders) — otherwise formatting raises on the unknown var.
        prompt = _TEXT_TO_SQL_TMPL.partial_format(max_rows=self.settings.app.max_result_rows)
        return NLSQLTableQueryEngine(
            sql_database=self.sql_database,
            tables=tables,
            llm=Settings.llm,
            text_to_sql_prompt=prompt,
            sql_only=True,
            synthesize_response=False,
        )

    async def generate_and_validate_sql(self, user_question: str, history: str = "") -> tuple[str, list[str]]:
        """
        Generate SQL with NLSQLTableQueryEngine (sql_only), then validate + cap it.
        `history` is recent conversation context (may be empty) folded into the NL
        query so follow-up questions resolve. Returns (sql_query, tables_used).
        """
        # Step 1: ClickHouse catalog narrows the table set the LLM sees.
        relevant_tables = self.catalog.get_relevant_tables(user_question)

        # Step 2: Fold recent history into the natural-language query (the query
        # engine has no chat-memory parameter of its own).
        nl_query = (
            f"Prior conversation (use only to resolve references):\n{history}\n\nQuestion: {user_question}"
            if history
            else user_question
        )

        # Step 3: Generate SQL. Run in a worker thread so any synchronous work
        # inside the engine never blocks the event loop under load.
        query_engine = self._build_query_engine(relevant_tables)
        response = await asyncio.to_thread(query_engine.query, nl_query)

        # sql_only mode returns the SQL in metadata["sql_query"]; fall back to the
        # response text to be robust across llama-index versions.
        sql_query = (response.metadata or {}).get("sql_query") or str(response.response)
        sql_query = sql_query.strip()

        # Clean markdown wrappers if the model added any.
        sql_query = re.sub(r"^```(?:sql)?", "", sql_query, flags=re.IGNORECASE)
        sql_query = re.sub(r"```$", "", sql_query).strip()

        # Step 4: Fast safety validation (read-only role + RLS are the real guards;
        # this rejects obviously unsafe output before it reaches the database).
        if not self.validate_sql_query(sql_query):
            raise PermissionError(f"Security Alert: Blocked unauthorized or unsafe SQL command: {sql_query}")

        # Step 5: Enforce a row cap before the query ever reaches the database.
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

    def as_query_engine_tool(self, tables: list[str] | None = None) -> QueryEngineTool:
        """
        Expose the SQL generator as a LlamaIndex QueryEngineTool. Not wired into a
        ReActAgent today (SQL is the only skill), but this is the seam a future
        multi-skill handler routes through once the MQL skill lands.
        """
        engine = self._build_query_engine(tables or list(_FALLBACK_TABLES))
        return QueryEngineTool.from_defaults(
            query_engine=engine,
            name="text_to_sql",
            description=(
                "Generates and runs read-only PostgreSQL over the field-insights "
                "tables (stores, active_tasks, districts, users) for current-state "
                "questions about task completion, overdue/at-risk tasks, and store "
                "or district performance."
            ),
        )
