#!/usr/bin/env python3
"""
Build/refresh the pgvector ``schema_embeddings`` index used by stage-3 vector rerank.

Projects the semantic dictionary (config/semantic/dictionary.yaml) into embedded
table/column DESCRIPTION vectors on the pgvector-enabled Postgres named by
``PIPELINE_PGVECTOR_DSN`` (or the app DB url when unset). Idempotent: upserts by
(object_type, table_name, column_name) and records the embedding model used so dimension
drift is detectable. Uses the pure-Python pg8000 driver (already a project dependency) so
no extra binary/system package is required in the no-admin/portable environment.

Prerequisites:
    * pip install llama-index-embeddings-ollama   (RAG embedding model)
    * a pgvector-enabled Postgres reachable at PIPELINE_PGVECTOR_DSN
    * ollama pull nomic-embed-text

Run AFTER scripts/init_clickhouse.py and whenever the dictionary changes:
    python scripts/index_schema_embeddings.py
"""

import sys

sys.path.insert(0, ".")

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from config.settings import get_settings  # noqa: E402
from src.pipeline.embeddings import build_embed_model, embed_text  # noqa: E402
from src.pipeline.pgvector_store import schema_embeddings_ddl  # noqa: E402
from src.pipeline.semantic_layer import SemanticLayer  # noqa: E402


def _sync_dsn(settings) -> str:
    dsn = settings.pipeline.pgvector_dsn or settings.database.url
    if dsn.startswith("postgresql+pg8000://"):
        return dsn
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn.replace("postgresql+asyncpg://", "postgresql+pg8000://", 1)
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+pg8000://", 1)
    return dsn


def _vec_literal(vec) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


_INSERT = text(
    """
    INSERT INTO schema_embeddings
        (object_type, table_name, column_name, db_source, content, embedding, model)
    VALUES (:object_type, :table_name, :column_name, :db_source, :content,
            CAST(:embedding AS vector), :model)
    ON CONFLICT (object_type, table_name, column_name) DO UPDATE
       SET content = EXCLUDED.content,
           embedding = EXCLUDED.embedding,
           db_source = EXCLUDED.db_source,
           model = EXCLUDED.model,
           updated_at = now()
    """
)


def main() -> int:
    settings = get_settings()
    semantic = SemanticLayer.load()

    embed_model = build_embed_model(settings)
    if embed_model is None:
        print("✗ Embedding model unavailable (install llama-index-embeddings-ollama and run "
              "`ollama pull nomic-embed-text`). Aborting.")
        return 1

    engine = create_engine(_sync_dsn(settings), poolclass=NullPool)
    model_name = settings.embedding.model
    dim = settings.embedding.dim

    with engine.begin() as conn:
        for stmt in schema_embeddings_ddl(dim).strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))

    inserted = 0
    with engine.begin() as conn:
        for doc in semantic.iter_embedding_docs():
            vec = embed_text(embed_model, doc["content"])
            if not vec:
                print(f"  ! skipped {doc['table_name']}.{doc.get('column_name')} (no embedding)")
                continue
            conn.execute(_INSERT, {
                "object_type": doc["object_type"],
                "table_name": doc["table_name"],
                "column_name": doc.get("column_name"),
                "db_source": doc["db_source"],
                "content": doc["content"],
                "embedding": _vec_literal(vec),
                "model": model_name,
            })
            inserted += 1

    print(f"✓ Indexed {inserted} schema docs into schema_embeddings (model={model_name}, dim={dim})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
