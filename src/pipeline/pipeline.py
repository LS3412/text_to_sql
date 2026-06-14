"""
Thin pipeline composition object the orchestrator delegates to for the dynamic path.

Wires stages 1-11 in order and returns ``(Card, trace_dict)``. It deliberately knows
NOTHING about Redis, the prebuilt-KPI catalog, audit logging, or A2A — those stay in the
orchestrator, which uses the returned ``trace_dict`` to write the TOOL_CALL / TOOL_RESULT /
A2UI_DISPLAY audit rows exactly as before. The pipeline raises StageError/exceptions on
unrecoverable failure; the orchestrator's existing try/except renders the System Apology card.
"""

from __future__ import annotations

import logging

from src.card_model import Card
from src.pipeline.ast_validator import ASTValidator
from src.pipeline.contracts import PipelineContext
from src.pipeline.embeddings import build_embed_model
from src.pipeline.generator import SQLGenerator
from src.pipeline.pgvector_store import PgVectorStore
from src.pipeline.retriever import HybridSchemaRetriever
from src.pipeline.router import DatabaseRouter, build_out_of_scope_card
from src.pipeline.schema_linker import SchemaLinker
from src.pipeline.schema_validator import SchemaTypeValidator
from src.pipeline.semantic_layer import SemanticLayer
from src.pipeline.stage9_execution import ExecutionEngine
from src.pipeline.stage10_correction import CorrectionLoop
from src.pipeline.stage11_response import build_card
from src.pipeline.transpiler import DialectTranspiler

logger = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, sql_skill, settings, sql_llm=None):
        self.settings = settings
        self.semantic = SemanticLayer.load()

        # Stage 1
        self.router = DatabaseRouter(self.semantic, settings)

        # Stage 3 infra (embeddings + pgvector are optional / degrade gracefully)
        self.embed_model = build_embed_model(settings)
        self.pgvector = PgVectorStore(settings)
        self.retriever = HybridSchemaRetriever(
            sql_skill.catalog, self.embed_model, self.pgvector, self.semantic, settings
        )

        # Stage 4
        self.linker = SchemaLinker(self.semantic, self.embed_model, settings)

        # Stages 5-9 (composed by the correction loop, stage 10)
        self.generator = SQLGenerator(sql_llm, self.semantic, settings)
        self.loop = CorrectionLoop(
            generator=self.generator,
            ast_validator=ASTValidator(sql_skill),
            type_validator=SchemaTypeValidator(self.semantic),
            transpiler=DialectTranspiler(settings),
            execution_engine=ExecutionEngine(sql_skill, settings),
            settings=settings,
        )

    def set_sql_llm(self, llm) -> None:
        """Inject the dedicated low-temperature SQL LLM built at startup."""
        self.generator.llm = llm

    async def run(self, ctx: PipelineContext) -> tuple[Card, dict]:
        # Stage 1 — classify + route
        route = await self.router.route(ctx.question)
        ctx.route = route
        if not route.in_scope:
            card = build_out_of_scope_card(self.settings.router.out_of_scope_message)
            return card, {"routing": "out_of_scope", "reason": route.reason}

        # Stage 3 — hybrid retrieval
        retrieved = await self.retriever.retrieve(
            ctx.question, dialect=route.target_dialect, db_source=route.profile.db_source
        )
        ctx.retrieved = retrieved

        # Stage 4 — schema linking
        links = self.linker.link(ctx.question, retrieved)
        ctx.links = links

        # Stages 5-9 inside the bounded correction loop (stage 10)
        transpiled, result, card_spec = await self.loop.run(ctx, retrieved, route, links)

        # Stage 11 — render the Card
        card = build_card(card_spec, result)

        trace = {
            "routing": "pipeline",
            "profile": route.profile.name,
            "final_sql": transpiled.sql,
            "tables": sorted(retrieved.table_names()),
            "retrieval_mode": retrieved.retrieval_mode,
            "degraded_retrieval": retrieved.degraded,
            "attempts": ctx.attempt + 1,
            "rows": result.rows,
            "row_count": result.row_count,
        }
        return card, trace
