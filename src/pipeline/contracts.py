"""
Unified data contracts shared by EVERY pipeline stage.

This is the single canonical module that all of stages 1-11 import. It deliberately
holds ONLY data structures (dataclasses + one Exception type) and has no runtime
dependencies on the rest of the package, so it can be imported anywhere without risk
of circular imports.

Design notes (resolving the three reconciliations called out in the plan):
  * StageError is an *Exception subclass* — stages ``raise`` it, the correction loop
    ``catch``es it. There is no separate result-wrapper type.
  * Generation emits SQL and presentation as TWO distinct objects (GeneratedSQL +
    CardSpec) produced by ONE LLM call. The correction loop only re-generates to fix
    the SQL; stage 11 consumes the CardSpec.
  * There is ONE retrieval type (RetrievedSchema) — no parallel "RetrievalBundle".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# Database profile (the registry of instances lives in profiles.py)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DBProfile:
    """A pluggable database target.

    ``dialect``     — sqlglot dialect name used by the transpiler (stage 8).
    ``db_source``   — the catalog ``db_source`` filter used by retrieval (stage 3).
    ``executor_key``— key into the executor registry (stage 9); ``None`` means the
                      profile is a dialect/catalog target only and is not executable
                      yet (e.g. ClickHouse, per the locked decision).
    """

    name: str
    dialect: str
    db_source: str
    executor_key: Optional[str]
    is_default: bool = False


# --------------------------------------------------------------------------- #
# Stage 1 — routing
# --------------------------------------------------------------------------- #
@dataclass
class RouteDecision:
    in_scope: bool
    profile: DBProfile
    source_dialect: str
    target_dialect: str
    reason: str = ""
    confidence: float = 1.0
    ambiguous: bool = False


# --------------------------------------------------------------------------- #
# Stages 3 — retrieved schema (the single retrieval representation)
# --------------------------------------------------------------------------- #
@dataclass
class RetrievedColumn:
    table: str
    column: str
    description: str = ""
    data_type: Optional[str] = None
    example_values: list[str] = field(default_factory=list)


@dataclass
class RetrievedTable:
    table: str
    description: str = ""
    columns: list[RetrievedColumn] = field(default_factory=list)
    db_source: str = "sql"


# Retrieval modes, most→least capable. ``degraded`` is True for anything below the first.
RETRIEVAL_KEYWORD_VECTOR = "keyword_vector"
RETRIEVAL_KEYWORD_ONLY = "keyword_only"
RETRIEVAL_VECTOR_ONLY = "vector_only"
RETRIEVAL_ALL_TABLES_COMPACT = "all_tables_compact"
RETRIEVAL_CORE_FLOOR = "core_floor"


@dataclass
class RetrievedSchema:
    tables: list[RetrievedTable] = field(default_factory=list)
    dialect: str = "postgres"
    retrieval_mode: str = RETRIEVAL_KEYWORD_VECTOR
    degraded: bool = False

    def is_empty(self) -> bool:
        return not self.tables

    def table_names(self) -> set[str]:
        return {t.table for t in self.tables}

    def column_names(self, table: str) -> set[str]:
        for t in self.tables:
            if t.table == table:
                return {c.column for c in t.columns}
        return set()

    def to_prompt_block(self) -> str:
        """Full per-table schema text for the generation prompt.

        Renders columns when known; otherwise falls back to the table description so
        the block is never empty for a known table.
        """
        if not self.tables:
            return ""
        blocks: list[str] = []
        for t in self.tables:
            lines = [f"Table: {t.table}"]
            if t.description:
                lines.append(f"  Description: {t.description}")
            if t.columns:
                lines.append("  Columns:")
                for c in t.columns:
                    parts = [f"    - {c.column}"]
                    meta = []
                    if c.data_type:
                        meta.append(c.data_type)
                    if c.description:
                        meta.append(c.description)
                    if c.example_values:
                        meta.append("e.g. " + ", ".join(str(v) for v in c.example_values[:3]))
                    if meta:
                        parts.append(" (" + "; ".join(meta) + ")")
                    lines.append("".join(parts))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def compact_table_list(self) -> str:
        """One line per table (``name: description``) — the no-match fallback block."""
        return "\n".join(
            f"- {t.table}: {t.description}" if t.description else f"- {t.table}"
            for t in self.tables
        )


# --------------------------------------------------------------------------- #
# Stage 4 — schema linking
# --------------------------------------------------------------------------- #
LINK_SYNONYM = "synonym"
LINK_VALUE_MAP = "value_map"
LINK_EXACT = "exact"
LINK_FUZZY = "fuzzy"
LINK_EMBEDDING = "embedding"
LINK_UNRESOLVED = "unresolved"


@dataclass
class EntityLink:
    phrase: str
    table: Optional[str] = None
    column: Optional[str] = None
    literal: Optional[str] = None
    confidence: float = 0.0
    source: str = LINK_UNRESOLVED


@dataclass
class SchemaLinks:
    links: list[EntityLink] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.links

    def to_prompt_block(self) -> str:
        if not self.links:
            return ""
        lines = ["Resolved entities (use these mappings):"]
        for ln in self.links:
            target = ".".join(p for p in (ln.table, ln.column) if p)
            if ln.literal is not None:
                lines.append(f"  - \"{ln.phrase}\" -> {target} = '{ln.literal}'")
            elif target:
                lines.append(f"  - \"{ln.phrase}\" -> {target}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Stage 5 — generation outputs (SQL and presentation are SEPARATE objects)
# --------------------------------------------------------------------------- #
@dataclass
class GeneratedSQL:
    sql: str
    source_dialect: str = "postgres"


# Card kinds the strict Card validator (src/card_model.py) accepts.
VALID_CARD_KINDS = (
    "summary", "metric", "list", "ranking",
    "comparison", "trend", "alert", "confirmation", "text", "auto",
)


@dataclass
class CardSpec:
    title: str = ""
    body_template: str = ""
    card_kind: str = "auto"
    metric_label: str = "Value"
    metric_unit: str = ""
    suggested_actions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Stage 8/9 — transpiled SQL + execution result
# --------------------------------------------------------------------------- #
@dataclass
class TranspiledSQL:
    sql: str
    target_dialect: str
    profile_name: str


@dataclass
class ExecutionResult:
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


# --------------------------------------------------------------------------- #
# Uniform error type — raised by stages, caught by the correction loop
# --------------------------------------------------------------------------- #
# kind ∈ {syntax, multi_statement, readonly_violation, unknown_table,
#         unknown_column, type_mismatch, transpile, execute, generation, empty,
#         unsupported_target}
class StageError(Exception):
    def __init__(
        self,
        stage: str,
        kind: str,
        message: str,
        sql: Optional[str] = None,
        retryable: bool = True,
    ):
        super().__init__(f"[{stage}:{kind}] {message}")
        self.stage = stage
        self.kind = kind
        self.message = message
        self.sql = sql
        self.retryable = retryable


# --------------------------------------------------------------------------- #
# The context object threaded through every stage
# --------------------------------------------------------------------------- #
@dataclass
class PipelineContext:
    question: str
    tenant_id: str
    agent_id: str
    session_id: str
    user_id: str
    trace_id: str

    # Accumulated stage outputs (filled in as the pipeline progresses).
    route: Optional[RouteDecision] = None
    retrieved: Optional[RetrievedSchema] = None
    links: Optional[SchemaLinks] = None
    attempt: int = 0
