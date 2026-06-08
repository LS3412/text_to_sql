"""
In-process agent runner for evaluation.

Exercises the real pipeline (catalog -> NLSQLTableQueryEngine generation -> validate
-> RLS execution -> Card synthesis) without needing the HTTP server, and returns the
intermediate SQL, the rows, and the final Card so both SQL-level and Card-level
metrics can be scored from one run.

Requires the same infra as the app: Postgres (schema reflection + execution),
ClickHouse (catalog), and the configured LLM (Ollama by default). Construction will
raise if any are unreachable — the test suite turns that into a skip.
"""

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from llama_index.core import Settings, SQLDatabase
from llama_index.core.embeddings import MockEmbedding

from config.settings import get_settings
from config.llm import build_llm
from src.orchestrator import A2AOrchestrator, serialize_decimal

_REFLECT_TABLES = ["stores", "active_tasks", "districts", "users"]


class AgentRunner:
    def __init__(self):
        self.settings = get_settings()
        # Same global LLM + embedding setup as src/main.py.
        Settings.llm = build_llm(self.settings)
        Settings.embed_model = MockEmbedding(embed_dim=1536)

        # Build the shared SQLDatabase once (sync readonly engine), like startup.
        sync_url = self.settings.database.readonly_url.replace("+asyncpg", "+psycopg2")
        sync_engine = create_engine(sync_url, poolclass=NullPool)
        sql_database = SQLDatabase(sync_engine, include_tables=_REFLECT_TABLES)

        self.orchestrator = A2AOrchestrator()  # constructs SQLSkill + ClickHouse catalog
        self.orchestrator.sql_skill.sql_database = sql_database

    async def run(self, question: str, tenant_id: str = None, agent_id: str = None) -> dict:
        tenant_id = tenant_id or self.settings.eval.tenant_id
        agent_id = agent_id or self.settings.eval.agent_id

        sql, tables = await self.orchestrator.sql_skill.generate_and_validate_sql(question)
        rows = await self.orchestrator.sql_skill.execute_query(sql, agent_id, tenant_id)
        rows = serialize_decimal(rows)
        card = await self.orchestrator._synthesize_card(question, rows)
        return {"sql": sql, "tables": tables, "rows": rows, "card": card}


_runner: AgentRunner | None = None


def get_runner() -> AgentRunner:
    """Lazily build and cache the runner (raises if infra is unreachable)."""
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner
