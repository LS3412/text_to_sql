"""
Stage 7 — Schema & Type Validator.

Walks the sqlglot AST from stage 6 to collect every referenced base table and column and
verifies each EXISTS in the semantic catalog — this is the core defence against the LLM
hallucinating a table/column name (the failure mode the user called out, and the one that
matters most at 50+ tables). A miss raises a retryable StageError whose message names the
offending identifier so the correction loop can feed it back for a targeted fix.

Deliberately conservative to avoid blocking VALID queries:
  * CTE names and table aliases are excluded from the base-table existence check.
  * Unqualified columns are flagged only when they exist in NONE of the referenced tables.
  * Only SUM/AVG over a clearly non-numeric column raises a type_mismatch.
If the AST is None (sqlglot unavailable) it passes through; if the catalog can't resolve a
table it degrades that table's columns to "unchecked" rather than failing.
"""

from __future__ import annotations

import logging

from src.pipeline.contracts import RetrievedSchema, StageError

logger = logging.getLogger(__name__)

try:
    from sqlglot import exp

    HAVE_SQLGLOT = True
except Exception:  # pragma: no cover
    exp = None
    HAVE_SQLGLOT = False

_NUMERIC_HINTS = ("int", "numeric", "decimal", "float", "double", "real", "serial", "money")


class SchemaTypeValidator:
    def __init__(self, semantic):
        self.semantic = semantic

    def validate(self, ast, retrieved: RetrievedSchema) -> None:
        if ast is None or not HAVE_SQLGLOT:
            return  # cannot validate without an AST (documented degradation)

        cte_names = {c.alias_or_name for c in ast.find_all(exp.CTE)} if exp else set()
        base_tables, alias_map = self._collect_tables(ast, cte_names)

        # 1. Every base table must exist in the semantic catalog (or be a CTE).
        for tname in base_tables:
            if tname in cte_names:
                continue
            if self.semantic.get_table(tname) is None:
                raise StageError(
                    "type_validate", "unknown_table",
                    f"table '{tname}' does not exist", retryable=True,
                )

        # Tables we can actually resolve columns against.
        known_tables = [t for t in base_tables if self.semantic.get_table(t) is not None]

        # 2. Columns must resolve to a real column of a referenced table.
        for col in ast.find_all(exp.Column):
            name = col.name
            if not name or name == "*":
                continue
            qualifier = col.table  # may be a table name or an alias
            real_table = alias_map.get(qualifier, qualifier) if qualifier else None

            if real_table and self.semantic.get_table(real_table) is not None:
                if not self.semantic.column_exists(real_table, name):
                    raise StageError(
                        "type_validate", "unknown_column",
                        f"column '{name}' does not exist on table '{real_table}'", retryable=True,
                    )
            elif not qualifier and known_tables:
                # Unqualified: must exist in at least one referenced known table.
                if not any(self.semantic.column_exists(t, name) for t in known_tables):
                    raise StageError(
                        "type_validate", "unknown_column",
                        f"column '{name}' does not exist on any referenced table", retryable=True,
                    )

        # 3. Light aggregate type sanity (SUM/AVG on a non-numeric column).
        self._check_aggregates(ast, alias_map)

    # ------------------------------------------------------------------ #
    def _collect_tables(self, ast, cte_names: set[str]):
        base_tables: set[str] = set()
        alias_map: dict[str, str] = {}
        for tbl in ast.find_all(exp.Table):
            name = tbl.name
            if not name or name in cte_names:
                continue
            base_tables.add(name)
            alias = tbl.alias
            if alias:
                alias_map[alias] = name
        return base_tables, alias_map

    def _check_aggregates(self, ast, alias_map: dict[str, str]) -> None:
        agg_types = tuple(t for t in (getattr(exp, n, None) for n in ("Sum", "Avg")) if t is not None)
        if not agg_types:
            return
        for agg in ast.find_all(*agg_types):
            col = agg.find(exp.Column)
            if col is None:
                continue
            real_table = alias_map.get(col.table, col.table) if col.table else None
            if not real_table:
                continue
            cdef = self.semantic.get_column(real_table, col.name)
            if cdef and cdef.type and not any(h in cdef.type.lower() for h in _NUMERIC_HINTS):
                raise StageError(
                    "type_validate", "type_mismatch",
                    f"cannot {agg.key.upper()} non-numeric column '{real_table}.{col.name}' "
                    f"(type {cdef.type})", retryable=True,
                )
