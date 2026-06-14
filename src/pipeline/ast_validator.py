"""
Stage 6 — AST Parser & Syntax Validator.

Parses the generated SQL with sqlglot in the source dialect and enforces read-only:
only a single SELECT (or WITH ... SELECT / UNION) is allowed; DML/DDL is blocked by AST
node-type inspection (robust where the regex guard is fragile, e.g. CTE-wrapped writes or
keywords inside string literals). The existing fast regex validator
(SQLSkill.validate_sql_query) runs FIRST as a cheap pre-filter and a hard security gate.

Soft dependency: if sqlglot is not installed, this degrades to regex-only validation and
returns ``ast=None``; stage 7 then skips AST-based type checking. A SYSTEM-level warning is
logged once so the degradation is visible.
"""

from __future__ import annotations

import logging

from src.pipeline.contracts import GeneratedSQL, StageError

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp

    HAVE_SQLGLOT = True
except Exception:  # pragma: no cover - exercised only when sqlglot is absent
    sqlglot = None
    exp = None
    HAVE_SQLGLOT = False
    logger.warning("sqlglot not installed — AST validation/transpile degrade to regex-only")


def _forbidden_node_types():
    names = [
        "Insert", "Update", "Delete", "Drop", "Create", "Alter",
        "TruncateTable", "Merge", "Command",
    ]
    return tuple(t for t in (getattr(exp, n, None) for n in names) if t is not None)


class ASTValidator:
    def __init__(self, sql_skill):
        self.sql_skill = sql_skill

    def validate(self, gen: GeneratedSQL):
        """Return the parsed AST (or None when sqlglot is unavailable). Raises StageError."""
        sql = gen.sql

        # Cheap, hard security pre-filter (blocks DML/DDL keywords + ';'-stacking).
        if not self.sql_skill.validate_sql_query(sql):
            raise StageError(
                "ast_validate", "readonly_violation",
                "query rejected by the safety validator (non-SELECT, DML/DDL, or stacked statement)",
                sql=sql, retryable=False,
            )

        if not HAVE_SQLGLOT:
            return None

        dialect = gen.source_dialect
        try:
            statements = sqlglot.parse(sql, dialect=dialect, error_level=sqlglot.ErrorLevel.RAISE)
        except Exception as exc:  # sqlglot.errors.ParseError and friends
            raise StageError("ast_validate", "syntax", f"SQL parse error: {exc}", sql=sql, retryable=True)

        non_empty = [s for s in statements if s is not None]
        if len(non_empty) != 1:
            raise StageError(
                "ast_validate", "multi_statement",
                f"expected exactly one statement, found {len(non_empty)}", sql=sql, retryable=False,
            )

        ast = non_empty[0]

        forbidden = _forbidden_node_types()
        if forbidden and ast.find(*forbidden) is not None:
            raise StageError(
                "ast_validate", "readonly_violation",
                "query contains a write/DDL operation", sql=sql, retryable=False,
            )

        if not self._is_read_only_root(ast):
            raise StageError(
                "ast_validate", "readonly_violation",
                "top-level statement is not a SELECT/UNION/CTE-over-SELECT", sql=sql, retryable=False,
            )

        return ast

    @staticmethod
    def _is_read_only_root(ast) -> bool:
        read_types = tuple(
            t for t in (getattr(exp, n, None) for n in ("Select", "Union", "Subquery", "With"))
            if t is not None
        )
        if isinstance(ast, read_types):
            return True
        # Some sqlglot versions attach a CTE via the `with` arg on a Select root.
        return getattr(ast, "args", {}).get("this") is not None and isinstance(
            ast, getattr(exp, "Query", tuple())
        )
