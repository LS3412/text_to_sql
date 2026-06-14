"""
Stage 9 — Execution Engine.

A pluggable executor registry keyed by the routed profile's ``executor_key``. Only the
Postgres executor is wired (the locked decision: all execution stays on Postgres/AlloyDB).
The Postgres executor opens its OWN short-lived ``readonly_session()`` and runs the SQL as
the ``a2a_readonly`` role under tenant RLS (set via the two GUCs in SQLSkill.execute_query) —
this fixes the previous behaviour of executing generated SQL on the read/write owner session.
A fresh session per call means a failed attempt never poisons the next correction attempt.

The ClickHouse profile has no executor (``executor_key=None``) and yields a non-retryable
StageError so the orchestrator renders a graceful card rather than crashing.
"""

from __future__ import annotations

import logging

from config.database import readonly_session
from src.pipeline.contracts import ExecutionResult, PipelineContext, RouteDecision, StageError, TranspiledSQL
from src.pipeline.stage11_response import serialize_decimal

logger = logging.getLogger(__name__)


class PostgresExecutor:
    def __init__(self, sql_skill, settings):
        self.sql_skill = sql_skill
        self.max_rows = settings.app.max_result_rows

    async def run(self, transpiled: TranspiledSQL, ctx: PipelineContext) -> ExecutionResult:
        try:
            async with readonly_session() as ro:
                rows = await self.sql_skill.execute_query(
                    async_session=ro,
                    sql_query=transpiled.sql,
                    agent_id=ctx.agent_id,
                    tenant_id=ctx.tenant_id,
                )
        except Exception as exc:  # asyncpg / SQLAlchemy runtime error — fixable by re-prompting
            raise StageError(
                "execute", "execute", f"query execution failed: {exc}",
                sql=transpiled.sql, retryable=True,
            )
        rows = serialize_decimal(rows)
        return ExecutionResult(rows=rows, row_count=len(rows), truncated=len(rows) >= self.max_rows)


class ExecutionEngine:
    def __init__(self, sql_skill, settings):
        pg = PostgresExecutor(sql_skill, settings)
        self.registry = {"postgres": pg, "alloydb": pg}

    async def run(self, transpiled: TranspiledSQL, route: RouteDecision, ctx: PipelineContext) -> ExecutionResult:
        key = route.profile.executor_key
        if not key or key not in self.registry:
            raise StageError(
                "execute", "unsupported_target",
                f"no execution engine is wired for profile '{route.profile.name}'",
                sql=transpiled.sql, retryable=False,
            )
        return await self.registry[key].run(transpiled, ctx)
