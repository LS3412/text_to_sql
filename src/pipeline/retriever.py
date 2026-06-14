"""
Stage 3 — Hybrid Schema Retrieval (RAG).

ClickHouse ``hasToken`` keyword prefilter (existing ClickHouseCatalog) selects candidate
table names; pgvector cosine rerank reorders/recalls them by embedded-description
similarity; the chosen tables are hydrated into a RetrievedSchema from the SEMANTIC LAYER
(the authoritative source of column descriptions/types/examples).

Retrieval and selection operate on DESCRIPTIONS, never raw identifiers, and only the
top-K tables ever reach the prompt — this is what lets the prompt stay small at 50+ tables.

Degrade chain (most→least capable):
  1. keyword + vector            (best)
  2. keyword only                (vector/embeddings/pgvector unavailable)
  3. vector only                 (no keyword signal but vector available)
  4. all-tables compact list     (no signal at all — one line per table so the LLM can pick)
  5. core floor                  (stores/active_tasks) — last resort
"""

from __future__ import annotations

import logging
import re

from src.pipeline.contracts import (
    RETRIEVAL_ALL_TABLES_COMPACT,
    RETRIEVAL_CORE_FLOOR,
    RETRIEVAL_KEYWORD_ONLY,
    RETRIEVAL_KEYWORD_VECTOR,
    RETRIEVAL_VECTOR_ONLY,
    RetrievedSchema,
    RetrievedTable,
)
from src.pipeline.embeddings import aembed_query
from src.skills.clickhouse_catalog import _FALLBACK_TABLES

logger = logging.getLogger(__name__)
_WORD_RE = re.compile(r"[a-z0-9]+")


class HybridSchemaRetriever:
    def __init__(self, catalog, embed_model, pgvector_store, semantic, settings):
        self.catalog = catalog
        self.embed_model = embed_model
        self.pgvector = pgvector_store
        self.semantic = semantic
        self.settings = settings

    async def retrieve(
        self, question: str, dialect: str = "postgres", db_source: str = "sql",
        top_k_tables: int | None = None,
    ) -> RetrievedSchema:
        top_k = top_k_tables or self.settings.retrieval.top_k_tables
        keyword = self._keyword_prefilter(question, db_source)
        vector = await self._vector_rerank(question, db_source, top_k)

        if keyword and vector:
            names = self._merge(vector, keyword)[:top_k]
            mode, degraded = RETRIEVAL_KEYWORD_VECTOR, False
        elif keyword:
            names, mode, degraded = keyword[:top_k], RETRIEVAL_KEYWORD_ONLY, True
        elif vector:
            names, mode, degraded = vector[:top_k], RETRIEVAL_VECTOR_ONLY, True
        else:
            return self._all_tables_compact(db_source, dialect)

        tables = self.semantic.build_retrieved_tables(names, db_source)
        if not tables:
            return self._all_tables_compact(db_source, dialect)
        return RetrievedSchema(tables=tables, dialect=dialect, retrieval_mode=mode, degraded=degraded)

    # ------------------------------------------------------------------ #
    def _keyword_prefilter(self, question: str, db_source: str) -> list[str]:
        try:
            cands = self.catalog.get_relevant_tables(
                question, limit=self.settings.retrieval.keyword_prefilter_k
            )
        except Exception as exc:
            logger.warning("Keyword prefilter failed (%s)", exc)
            cands = []
        # ClickHouseCatalog returns the core fallback set on a miss OR when it is down.
        # Treat "exactly the core set + zero vocabulary overlap" as NO real keyword signal.
        if set(cands) == set(_FALLBACK_TABLES) and self._vocab_overlap(question, db_source) == 0:
            return []
        return list(cands)

    async def _vector_rerank(self, question: str, db_source: str, top_k: int) -> list[str]:
        if not self.settings.retrieval.vector_enabled or self.embed_model is None or self.pgvector is None:
            return []
        try:
            if not await self.pgvector.vector_available():
                return []
            qvec = await aembed_query(self.embed_model, question)
            if not qvec:
                return []
            ranked = await self.pgvector.vector_rerank(qvec, db_source, max(top_k, 5))
            return [name for name, _score in ranked]
        except Exception as exc:
            logger.warning("Vector rerank failed (%s); degrading", exc)
            return []

    @staticmethod
    def _merge(primary: list[str], secondary: list[str]) -> list[str]:
        seen: list[str] = []
        for name in [*primary, *secondary]:
            if name not in seen:
                seen.append(name)
        return seen

    def _vocab_overlap(self, question: str, db_source: str) -> int:
        vocab = self.semantic.vocabulary(db_source)
        words = set(_WORD_RE.findall(question.lower()))
        count = len(words & vocab)
        q_lower = question.lower()
        for token in vocab:
            if " " in token and token in q_lower:
                count += 1
        return count

    def _all_tables_compact(self, db_source: str, dialect: str) -> RetrievedSchema:
        cap = self.settings.retrieval.max_compact_tables
        tdefs = self.semantic.tables_for_source(db_source)[:cap]
        tables = [
            RetrievedTable(table=t.name, description=t.description, db_source=t.db_source, columns=[])
            for t in tdefs
        ]
        if tables:
            return RetrievedSchema(
                tables=tables, dialect=dialect,
                retrieval_mode=RETRIEVAL_ALL_TABLES_COMPACT, degraded=True,
            )
        # Core floor — stores/active_tasks fully hydrated.
        floor = self.semantic.build_retrieved_tables(list(_FALLBACK_TABLES), db_source)
        return RetrievedSchema(
            tables=floor, dialect=dialect, retrieval_mode=RETRIEVAL_CORE_FLOOR, degraded=True
        )
