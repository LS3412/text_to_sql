"""
src.pipeline — the modular 11-stage text-to-SQL pipeline.

Stage map:
  1  router.py             Query Classifier & Database Router
  2  semantic_layer.py     Semantic Layer (Data Dictionary, single source of truth)
  3  retriever.py          Schema Retrieval (hybrid RAG: ClickHouse keyword + pgvector)
  4  schema_linker.py      Schema Linking (entity resolution)
  5  generator.py          SQL Generation Engine (LLM)
  6  ast_validator.py      AST Parser & Syntax Validator (sqlglot)
  7  schema_validator.py   Schema & Type Validator
  8  transpiler.py         Dialect Transpiler (sqlglot; pluggable profiles)
  9  stage9_execution.py   Execution Engine (pluggable; Postgres wired)
  10 stage10_correction.py Auto-Correction Loop (bounded retries)
  11 stage11_response.py   Natural Language Response Generator (rows -> Card)

The orchestrator composes these via ``Pipeline`` and threads a ``PipelineContext``.
"""

from src.pipeline.contracts import (
    CardSpec,
    ExecutionResult,
    GeneratedSQL,
    PipelineContext,
    RetrievedSchema,
    RouteDecision,
    SchemaLinks,
    StageError,
)
from src.pipeline.pipeline import Pipeline

__all__ = [
    "Pipeline",
    "PipelineContext",
    "RouteDecision",
    "RetrievedSchema",
    "SchemaLinks",
    "GeneratedSQL",
    "CardSpec",
    "ExecutionResult",
    "StageError",
]
