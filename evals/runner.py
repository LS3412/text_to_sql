"""
In-process agent runner for evaluation.
Exercises the real pipeline (catalog -> single-call SQL generation -> validate
-> RLS execution -> Card data-binding) without needing the HTTP server, and returns the
intermediate SQL, the rows, and the final Card so both SQL-level and Card-level
metrics can be scored from one run.
"""

from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from llama_index.core import Settings, SQLDatabase
from llama_index.core.embeddings import MockEmbedding

from config.settings import get_settings
from config.llm import build_llm, build_sql_llm
from config.database import DatabaseManager
from src.orchestrator import A2AOrchestrator, serialize_decimal  # noqa: F401  (re-exported)
from src.pipeline.contracts import PipelineContext

# Reflect only existing schema tables
_REFLECT_TABLES = ["stores", "active_tasks"]


class AgentRunner:
    def __init__(self):
        self.settings = get_settings()
        
        # Same global LLM + embedding setup as src/main.py.
        Settings.llm = build_llm(self.settings)
        Settings.embed_model = MockEmbedding(embed_dim=1536)
        
        # Build the shared SQLDatabase once (sync readonly engine), like startup.
        # Swap out binary psycopg2 with pure-Python pg8000 for Windows
        sync_url = self.settings.database.readonly_url.replace("+asyncpg", "+pg8000")
        sync_engine = create_engine(sync_url, poolclass=NullPool)
        sql_database = SQLDatabase(sync_engine, include_tables=_REFLECT_TABLES)
        
        self.orchestrator = A2AOrchestrator()  # constructs SQLSkill + ClickHouse catalog + pipeline
        self.orchestrator.sql_skill.sql_database = sql_database
        # Inject the dedicated low-temperature SQL LLM (same as src/main.py startup).
        self.orchestrator.pipeline.set_sql_llm(build_sql_llm(self.settings))

    async def run(self, question: str, tenant_id: str = None, agent_id: str = None) -> dict:
        tenant_id = tenant_id or self.settings.eval.tenant_id
        agent_id = agent_id or self.settings.eval.agent_id

        # Exercise the full 11-stage pipeline (router -> retrieval -> linking ->
        # generate/validate/transpile/execute correction loop -> response).
        ctx = PipelineContext(
            question=question,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id="eval",
            user_id="eval",
            trace_id="eval",
        )
        card, trace = await self.orchestrator.pipeline.run(ctx)
        return {
            "sql": trace.get("final_sql"),
            "tables": trace.get("tables", []),
            "rows": trace.get("rows", []),
            "card": card,
        }


_runner: AgentRunner | None = None


def get_runner() -> AgentRunner:
    """Lazily build and cache the runner (raises if infra is unreachable)."""
    global _runner
    if _runner is None:
        _runner = AgentRunner()
    return _runner
