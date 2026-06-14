"""
pgvector data-access for stage 3 (vector rerank) and the offline indexer.

Owns a NEW ``schema_embeddings`` table holding embedded table/column DESCRIPTIONS (not
tenant data), the cosine-distance KNN used to rerank schema candidates, and the DDL used
by scripts/index_schema_embeddings.py.

It uses its OWN dedicated async engine built from ``settings.pipeline.pgvector_dsn`` (or
the app DB url when that is empty), decoupling the schema index from the app's RLS /
read-only role entirely. Every method degrades gracefully: a missing extension, missing
table, or connection error makes ``vector_available`` return False and the retriever falls
back to keyword-only. The query vector is sent as a text-cast ``'[...]'::vector`` literal so
no binary asyncpg vector codec is required.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


def schema_embeddings_ddl(dim: int) -> str:
    return f"""
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE IF NOT EXISTS schema_embeddings (
        id           bigserial PRIMARY KEY,
        object_type  text NOT NULL CHECK (object_type IN ('table','column')),
        table_name   text NOT NULL,
        column_name  text,
        db_source    text NOT NULL DEFAULT 'sql',
        content      text NOT NULL,
        embedding    vector({dim}),
        model        text,
        updated_at   timestamptz NOT NULL DEFAULT now(),
        UNIQUE (object_type, table_name, column_name)
    );
    CREATE INDEX IF NOT EXISTS idx_schema_embeddings_hnsw
        ON schema_embeddings USING hnsw (embedding vector_cosine_ops);
    """


def _to_async_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


class PgVectorStore:
    def __init__(self, settings):
        self.settings = settings
        dsn = getattr(settings.pipeline, "pgvector_dsn", "") or settings.database.url
        self._dsn = _to_async_dsn(dsn)
        self._engine = None
        self._available: Optional[bool] = None

    def _get_engine(self):
        if self._engine is None:
            self._engine = create_async_engine(self._dsn, poolclass=NullPool)
        return self._engine

    async def vector_available(self) -> bool:
        """True only if the vector extension and schema_embeddings table both exist."""
        if self._available is not None:
            return self._available
        try:
            async with self._get_engine().connect() as conn:
                ext = await conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                )
                if ext.first() is None:
                    self._available = False
                    return False
                tbl = await conn.execute(text("SELECT to_regclass('public.schema_embeddings')"))
                self._available = tbl.scalar() is not None
        except Exception as exc:
            logger.warning("pgvector unavailable (%s); vector rerank disabled", exc)
            self._available = False
        return self._available

    async def vector_rerank(
        self, query_vec: list[float], db_source: str = "sql", top_k: int = 5
    ) -> list[tuple[str, float]]:
        """Return [(table_name, similarity), ...] for table-level embeddings, best first."""
        if not query_vec:
            return []
        qlit = _vec_literal(query_vec)
        sql = text(
            f"""
            SELECT table_name, 1 - (embedding <=> '{qlit}'::vector) AS score
            FROM schema_embeddings
            WHERE object_type = 'table' AND db_source = :src
            ORDER BY embedding <=> '{qlit}'::vector ASC
            LIMIT :k
            """
        )
        try:
            async with self._get_engine().connect() as conn:
                res = await conn.execute(sql, {"src": db_source, "k": top_k})
                return [(row[0], float(row[1])) for row in res.fetchall()]
        except Exception as exc:
            logger.warning("vector_rerank failed (%s); treating as no vector signal", exc)
            return []

    # ---- indexer-only helpers (sync engine kept separate in the script) ---- #
    async def ensure_schema(self) -> None:
        dim = self.settings.embedding.dim
        async with self._get_engine().begin() as conn:
            for stmt in schema_embeddings_ddl(dim).strip().split(";"):
                if stmt.strip():
                    await conn.execute(text(stmt))

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
