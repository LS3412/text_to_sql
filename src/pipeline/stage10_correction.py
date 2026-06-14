"""
Stage 10 — Auto-Correction Loop.

The master control flow that sequences generate(5) -> ast_validate(6) -> type_validate(7)
-> transpile(8) -> execute(9). On a RETRYABLE StageError it re-prompts the generator with the
error + previous SQL so the LLM can self-correct; on a non-retryable error (security reject,
unsupported target) it gives up immediately. The loop is strictly bounded by
``settings.pipeline.max_correction_retries`` (default 2 -> at most 3 attempts) and an optional
wall-clock budget — there is no unbounded ``while`` — after which it re-raises a terminal
StageError that the orchestrator turns into the graceful "System Apology" card.
"""

from __future__ import annotations

import logging
import time

from src.pipeline.contracts import (
    CardSpec,
    ExecutionResult,
    PipelineContext,
    RetrievedSchema,
    RouteDecision,
    SchemaLinks,
    StageError,
    TranspiledSQL,
)

logger = logging.getLogger(__name__)


class CorrectionLoop:
    def __init__(self, generator, ast_validator, type_validator, transpiler, execution_engine, settings):
        self.generator = generator
        self.ast_validator = ast_validator
        self.type_validator = type_validator
        self.transpiler = transpiler
        self.execution_engine = execution_engine
        self.settings = settings

    async def run(
        self,
        ctx: PipelineContext,
        retrieved: RetrievedSchema,
        route: RouteDecision,
        links: SchemaLinks | None,
    ) -> tuple[TranspiledSQL, ExecutionResult, CardSpec]:
        ps = self.settings.pipeline
        max_retries = ps.max_correction_retries if ps.enable_correction_loop else 0
        budget = getattr(ps, "correction_budget_seconds", 0) or 0
        start = time.monotonic()
        prior_error: StageError | None = None

        for attempt in range(max_retries + 1):
            ctx.attempt = attempt
            try:
                gen, card_spec = await self.generator.generate(ctx, retrieved, route, links, prior_error)
                ast = self.ast_validator.validate(gen)
                self.type_validator.validate(ast, retrieved)
                transpiled = self.transpiler.transpile(gen, ast, route)
                result = await self.execution_engine.run(transpiled, route, ctx)
                if attempt:
                    logger.info("Correction succeeded on attempt %d", attempt + 1)
                return transpiled, result, card_spec
            except StageError as err:
                if not err.retryable or attempt >= max_retries:
                    raise
                if budget and (time.monotonic() - start) > budget:
                    logger.warning("Correction budget (%ss) exceeded; giving up", budget)
                    raise
                logger.info("Attempt %d failed at %s:%s — retrying", attempt + 1, err.stage, err.kind)
                prior_error = err

        # Unreachable (the loop always returns or raises), but keep the type-checker happy.
        raise StageError("correction", "generation", "exhausted correction attempts", retryable=False)
