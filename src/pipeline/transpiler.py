"""
Stage 8 — Dialect Transpiler.

Transpiles the validated source-dialect SQL into the target execution dialect chosen by the
router, and applies the mandatory result-row LIMIT cap on the SAME AST (a single parse — the
LIMIT is NOT re-parsed in stage 9). The profile->dialect mapping is pluggable
(postgres/alloydb -> 'postgres'; clickhouse -> 'clickhouse'); today only the postgres
executor is wired downstream.

Fallbacks: when source == target (today's only path) it re-renders/limits without a risky
cross-dialect rewrite; when sqlglot is unavailable it applies the LIMIT via string surgery
and passes the SQL through unchanged (safe because the canonical source and default target
are both postgres).
"""

from __future__ import annotations

import logging
import re

from src.pipeline.contracts import GeneratedSQL, RouteDecision, StageError, TranspiledSQL

logger = logging.getLogger(__name__)

try:
    import sqlglot
    from sqlglot import exp

    HAVE_SQLGLOT = True
except Exception:  # pragma: no cover
    sqlglot = None
    exp = None
    HAVE_SQLGLOT = False


class DialectTranspiler:
    def __init__(self, settings):
        self.settings = settings
        self.max_rows = settings.app.max_result_rows

    def transpile(self, gen: GeneratedSQL, ast, route: RouteDecision) -> TranspiledSQL:
        source = route.source_dialect
        target = route.target_dialect
        profile_name = route.profile.name

        if not HAVE_SQLGLOT or ast is None:
            return TranspiledSQL(
                sql=self._regex_limit(gen.sql), target_dialect=target, profile_name=profile_name
            )

        try:
            capped = self._apply_limit_ast(ast)
            out_sql = capped.sql(dialect=target)
            return TranspiledSQL(sql=out_sql, target_dialect=target, profile_name=profile_name)
        except Exception as exc:  # UnsupportedError / ParseError / render error
            raise StageError(
                "transpile", "transpile",
                f"could not transpile {source}->{target}: {exc}", sql=gen.sql, retryable=True,
            )

    # ------------------------------------------------------------------ #
    def _apply_limit_ast(self, ast):
        """Add a LIMIT if missing; clamp an existing numeric LIMIT above the cap."""
        limit_node = ast.args.get("limit") if hasattr(ast, "args") else None
        if limit_node is None:
            try:
                return ast.limit(self.max_rows)
            except Exception:
                return ast  # some root types may not support .limit(); leave as-is
        # Clamp an oversized literal LIMIT.
        try:
            current = int(limit_node.expression.this)
            if current > self.max_rows:
                return ast.limit(self.max_rows)
        except Exception:
            pass
        return ast

    def _regex_limit(self, sql: str) -> str:
        cleaned = sql.strip().rstrip(";")
        if re.search(r"\blimit\b", cleaned, flags=re.IGNORECASE):
            return cleaned
        return f"{cleaned} LIMIT {self.max_rows}"
