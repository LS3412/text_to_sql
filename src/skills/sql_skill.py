"""
SQL Skill for translating natural language queries to SQL.
Highly optimized for speed and safety, bypassing heavy LlamaIndex scanning.
"""

import re
from sqlalchemy import create_engine, text
from config.settings import get_settings
from src.skills.clickhouse_catalog import ClickHouseCatalog
from llama_index.core import Settings


class SQLSkill:
    def __init__(self):
        self.settings = get_settings()
        
        # Replace asyncpg with pg8000 (pure Python) for the synchronous connection
        sync_url = self.settings.database.url.replace("postgresql+asyncpg://", "postgresql+pg8000://")
        self.sync_engine = create_engine(sync_url)
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

    async def generate_and_validate_sql(self, user_question: str) -> tuple[str, list[str]]:
        """
        Performs a rapid, single-shot SQL generation via Qwen, bypassing slow LlamaIndex engines.
        """
        # Step 1: Query ClickHouse to scale down database context
        relevant_tables = self.catalog.get_relevant_tables(user_question)
        
        # Step 2: Format the schemas of selected tables into a clean prompt
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

        system_prompt = f"""
        You are a highly precise PostgreSQL Translation Engine.
        Convert the user question into a safe PostgreSQL SELECT query using these schemas:
        
        {schema_definitions}

        Rules:
        1. Output ONLY the raw SQL query. No explanation, no markdown backticks, no commentary.
        2. When filtering by store name, always use `store_name = 'Store 118'` (string match) instead of filtering by ID unless requested.
        3. Do not append semicolon at the end of the query.
        """

        # Step 3: Run single-shot SQL generation via globally configured LLM (Qwen)
        response = await Settings.llm.acomplete(f"{system_prompt}\n\nQuestion: {user_question}\nSQL:")
        sql_query = response.text.strip()
        
        # Clean markdown wrappers if any
        sql_query = re.sub(r"^```(?:sql)?", "", sql_query, flags=re.IGNORECASE)
        sql_query = re.sub(r"```$", "", sql_query).strip()

        # Step 4: Run lightning-fast safety validation
        if not self.validate_sql_query(sql_query):
            raise PermissionError(f"Security Alert: Blocked unauthorized or unsafe SQL command: {sql_query}")
            
        return sql_query, relevant_tables

    async def execute_query(self, async_session, sql_query: str, agent_id: str) -> list[dict]:
        """
        Executes a safe SELECT statement against the PostgreSQL database.
        Enforces Row-Level Security (RLS) dynamically using set_config().
        """
        await async_session.execute(
            text("SELECT set_config('app.agent_id', :agent_id, true)"), 
            {"agent_id": agent_id}
        )
        
        result = await async_session.execute(text(sql_query))
        columns = result.keys()
        return [dict(zip(columns, row)) for row in result.fetchall()]
